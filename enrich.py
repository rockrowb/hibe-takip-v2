#!/usr/bin/env python3
"""
KATMAN 3/3 — OPSİYONEL AI ZENGİNLEŞTİRME

data/classified.json'ı OKUR (data/raw.json değil — sınıflandırma katmanının
üzerine inşa edilir), data/duyurular.json'a YAZAR. Panel (index.html) sadece
data/duyurular.json'ı okur.

Her yeni (henüz "details" alanı olmayan) kayıt için TEK bir AI çağrısında:
  1. Sınıflandırma: bu GERÇEKTEN AÇIK/GÜNCEL bir hibe-destek çağrısı mı, yoksa
     haber/sonuç ilanı/genel bilgilendirme mi? ("tur" alanı)
  2. Eğer açık bir hibe çağrısıysa: kimler başvurabilir, hibe miktarı, son
     başvuru tarihleri (birden fazla dönem/aşama varsa hepsi), desteklenen
     temel hizmetler/aktiviteler, ve ilgili tema etiketleri (tekstil, yazılım,
     tarım vb.) metinden çıkarılır.

VERİ KATMANLARI VE NEDEN AYRI:
  data/raw.json         <- scrape.py (ham, asla silinmez/üzerine yazılmaz)
  data/classified.json  <- classify.py (ücretsiz olasi_tur etiketi, her
                            çalıştırmada raw.json'dan yeniden üretilebilir)
  data/duyurular.json   <- BU SCRIPT (+ AI detayları). Önceden AI ile
                            işlenmiş "details" alanları HER ZAMAN korunur —
                            classify.py/scrape.py'da bir değişiklik olsa bile
                            daha önce parası ödenmiş AI sonuçları kaybolmaz.

GÜVENCE 1 — Yeni kayıt yoksa AI'a KESİNLİKLE gidilmez:
  Script en başta "details" alanı olmayan kayıt var mı diye bakar. Hiç yoksa
  API sağlayıcısını sorgulamadan, hiçbir ağ isteği atmadan sys.exit(0) ile
  çıkar.

GÜVENCE 2 — Kota/limit dolarsa o ana kadarki ilerleme kaybolmaz:
  - Her kayıt işlendikten hemen sonra data/duyurular.json diske yazılır.
  - API "kota/limit doldu" tipi bir hata döndürürse (HTTP 429, ya da
    "insufficient_quota" / "RESOURCE_EXHAUSTED" / "rate_limit" içeren
    mesajlar) döngü kalan kayıtlara dokunmadan durur, kalanlar bir sonraki
    çalıştırmada otomatik kuyruğa girer.

İKİ SAĞLAYICI DESTEKLENİR:
  - ANTHROPIC_API_KEY tanımlıysa    -> Claude (claude-sonnet-4-6) (öncelikli)
  - yoksa GEMINI_API_KEY tanımlıysa -> Google Gemini (gemini-2.5-flash)
  - ikisi de tanımlı değilse        -> script sessizce çıkar

Token tasarrufu katmanları:
  A) classify.py her kaydı ücretsiz anahtar kelime taramasından geçirip
     olasi_tur etiketler (hibe_olabilir / sonuc_olabilir / haber_olabilir / belirsiz).
  B) "sonuc_olabilir" VE "haber_olabilir" etiketli kayıtlar AI'ya HİÇ
     GÖNDERİLMEZ, ücretsiz olarak işaretlenir.
  C) "details" alanı zaten olan kayıtlar tekrar gönderilmez.

Kullanım:
    export ANTHROPIC_API_KEY=sk-ant-...    # ya da
    export GEMINI_API_KEY=AIza...
    python enrich.py                # bekleyen tüm kayıtları işler
    python enrich.py --limit 20     # tek çalıştırmada işlenecek üst sınır
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CLASSIFIED_FILE = ROOT / "data" / "classified.json"
DATA_FILE = ROOT / "data" / "duyurular.json"
HEADERS_WEB = {"User-Agent": "Mozilla/5.0 (compatible; HibeTakipBot/1.0)"}

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Not: Google, gemini-2.5-flash modelini Ekim 2026'da kullanımdan kaldıracağını
# duyurdu. O tarihten sonra model adını güncel bir Gemini Flash modeliyle
# değiştirmek gerekebilir (ai.google.dev/api/generate-content'ten kontrol edin).
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-2.5-flash"

QUOTA_ERROR_SIGNS = [
    "429", "insufficient_quota", "resource_exhausted", "rate_limit",
    "quota", "too many requests", "billing",
]

# Panelde filtre olarak sunulacak sabit tema listesi. AI bu listeden 0-4 tane
# uygun olanı seçer; hiçbiri uymuyorsa boş bırakır (uydurma yeni etiket üretmez).
TEMA_LISTESI = [
    "tarım", "hayvancılık", "gıda", "tekstil", "turizm", "enerji", "çevre",
    "yazılım", "donanım", "inovasyon", "ar-ge", "girişimcilik", "kadın girişimciliği",
    "gençlik", "istihdam", "ihracat", "dijital dönüşüm", "yapay zeka", "sağlık",
    "eğitim", "kültür-sanat", "sosyal girişimcilik", "kentsel dönüşüm",
    "afet yönetimi", "ulaştırma", "savunma sanayii", "biyoteknoloji", "oyun",
    "e-ticaret", "sivil toplum",
]

SYSTEM_PROMPT = f"""Sana bir Türkiye kamu/STK duyurusunun web sayfası metni verilecek.
Aşağıdaki şemada SADECE JSON döndür, başka hiçbir şey yazma (açıklama, markdown işareti vb. ekleme):

{{
  "tur": "hibe_duyurusu" | "haber" | "sonuc_ilani" | "diger",
  "basvuruya_acik": true | false | null,
  "kimler_basvurabilir": "kısa açıklama veya null",
  "hibe_miktari": "tutar/oran bilgisi (ör. '%75 hibe, üst limit 500.000 TL') veya null",
  "son_basvuru_tarihleri": ["YYYY-MM-DD", "..."] veya null (birden fazla aşama/dönem varsa hepsini listele, tek tarihse tek elemanlı liste),
  "desteklenen_aktiviteler": "hangi temel hizmetler/faaliyetler/harcamalar destekleniyor, kısa liste veya null",
  "temalar": ["..."] (aşağıdaki listeden 0-4 tane uygun olanı seç, listede yoksa boş bırak),
  "ozet": "1-2 cümlelik tarafsız özet"
}}

Tema listesi (sadece bunlardan seç): {", ".join(TEMA_LISTESI)}

"tur" alanını SIKI şekilde belirle — sadece "hibe_duyurusu" seçmek için metin
AÇIKÇA yeni başvurulara açık, güncel bir hibe/destek/fon çağrısı olmalı
(başvuru koşulları, son tarih veya başvuru şekli gibi somut bilgiler içermeli).
- Sadece bir kurumdan/programdan genel bahseden, geçmişte açılmış bir çağrıyı
  hatırlatan ama şu an başvuru almayan, ziyaret/imza töreni/toplantı/video gibi
  genel haberler, ya da net bir çağrı içermeyen metinleri "hibe_duyurusu" SAYMA
  — bunlar "haber" ya da "diger" olsun.
- Kurumsal/idari içerikler (anket merkezi, bütçe uygulama sonuçları, faaliyet
  raporu, stratejik plan, insan kaynakları/personel ilanı, ihale ilanı,
  yönetim kurulu/genel kurul kararları, KVKK/gizlilik metinleri, denetim
  raporu gibi) KESİNLİKLE "diger" olsun, bunları asla "hibe_duyurusu" sayma.
- Başvuru sonuçları/kazananlar/asıl-yedek liste/yarışma sonucu açıklanıyorsa "sonuc_ilani".
- "basvuruya_acik": metinde başvuru tarihinin geçmiş/gelecek olduğu netse true/false yap,
  emin değilsen null bırak.
- tur "hibe_duyurusu" DEĞİLSE kimler_basvurabilir, hibe_miktari, son_basvuru_tarihleri,
  desteklenen_aktiviteler, temalar alanlarını null/boş bırak, sadece ozet'i doldur.
- Emin olmadığın alanları null bırak, metinde olmayan bilgiyi ASLA uydurma."""


def fetch_text(url, max_chars=6000):
    try:
        r = requests.get(url, headers=HEADERS_WEB, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return None, str(e)
    ctype = r.headers.get("Content-Type", "")
    if "pdf" in ctype.lower() or url.lower().endswith(".pdf"):
        return None, "PDF - şimdilik atlanıyor"
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return text[:max_chars], None


class QuotaExceeded(Exception):
    pass


def _check_quota_error(exc, response_text=""):
    blob = f"{exc} {response_text}".lower()
    if any(sign in blob for sign in QUOTA_ERROR_SIGNS):
        raise QuotaExceeded(str(exc))


def call_claude(api_key, page_text):
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 600,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": page_text}],
        },
        timeout=30,
    )
    if resp.status_code == 429:
        raise QuotaExceeded(f"HTTP 429: {resp.text[:200]}")
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        _check_quota_error(e, resp.text)
        raise
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def call_gemini(api_key, page_text):
    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    resp = requests.post(
        url,
        params={"key": api_key},
        headers={"content-type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": page_text}]}],
            "generationConfig": {
                "maxOutputTokens": 600,
                "responseMimeType": "application/json",
            },
        },
        timeout=30,
    )
    if resp.status_code == 429:
        raise QuotaExceeded(f"HTTP 429: {resp.text[:200]}")
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        _check_quota_error(e, resp.text)
        raise
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def get_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini", os.environ["GEMINI_API_KEY"]
    return None, None


def call_ai(provider, api_key, page_text):
    if provider == "anthropic":
        return call_claude(api_key, page_text)
    elif provider == "gemini":
        return call_gemini(api_key, page_text)
    raise ValueError(f"Bilinmeyen sağlayıcı: {provider}")


def priority(item):
    order = {"hibe_olabilir": 0, "belirsiz": 1, "sonuc_olabilir": 2, "haber_olabilir": 2}
    return order.get(item.get("olasi_tur"), 1)


FREE_SKIP_DETAILS = {
    # SADECE çok net "sonuç ilanı" kalıpları ücretsiz elenir (ör. "kazananlar
    # açıklandı"). "haber_olabilir" (kurumsal/basın bülteni gibi görünenler)
    # ARTIK AI'a gönderiliyor — AI'ın sınıflandırmadaki rolünü güçlendirmek
    # için bilinçli tercih: anahtar kelime kalıpları yanılabilir (ör. "Anket
    # Merkezi" gerçekten alakasızdır ama farklı bir başlık yanlış pozitif
    # olabilir), AI son sözü söylesin.
    "sonuc_olabilir": "sonuc_ilani_tahmini",
}


def empty_details(tur_tahmini):
    return {
        "tur": tur_tahmini,
        "basvuruya_acik": False,
        "kimler_basvurabilir": None,
        "hibe_miktari": None,
        "son_basvuru_tarihleri": None,
        "desteklenen_aktiviteler": None,
        "temalar": [],
        "ozet": None,
        "ai_ile_dogrulandi": False,
    }


def save(store):
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Tek çalıştırmada işlenecek üst sınır")
    args = parser.parse_args()

    if not CLASSIFIED_FILE.exists():
        print("data/classified.json bulunamadı, önce scrape.py ve classify.py çalıştırılmalı.")
        sys.exit(1)

    classified = json.loads(CLASSIFIED_FILE.read_text(encoding="utf-8"))
    classified_items = classified.get("items", {})

    # Önceki AI sonuçlarını (data/duyurular.json) yükle — "details" alanları KORUNUR.
    if DATA_FILE.exists():
        existing = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        existing_items = existing.get("items", {})
    else:
        existing_items = {}

    # classified.json'daki her kayıt duyurular.json'a taşınır; daha önce
    # "details" işlenmişse o korunur, işlenmemişse pending kalır.
    items = {}
    for url, c_item in classified_items.items():
        merged = dict(c_item)
        if url in existing_items and "details" in existing_items[url]:
            merged["details"] = existing_items[url]["details"]
        items[url] = merged

    # --- GÜVENCE 1: yeni kayıt yoksa AI'a hiç gidilmez ---
    # Not: "duplicate_of" işaretli kayıtlar (classify.py'ın tekilleştirmesi)
    # AI kuyruğuna hiç girmez — zaten başka bir kayıtla aynı, gereksiz
    # maliyet/kota harcamamak için burada elenir.
    pending = [k for k, v in items.items() if "details" not in v and not v.get("duplicate_of")]
    if not pending:
        print("Yeni/bekleyen kayıt yok — AI'a hiç gidilmedi, hiçbir çağrı yapılmadı.")
        store = {"last_updated": classified.get("last_updated"),
                  "source_status": classified.get("source_status", {}), "items": items}
        save(store)
        sys.exit(0)

    provider, api_key = get_provider()
    if not provider:
        print(f"{len(pending)} bekleyen kayıt var ama ANTHROPIC_API_KEY/GEMINI_API_KEY tanımlı değil — AI atlanıyor.")
        store = {"last_updated": classified.get("last_updated"),
                  "source_status": classified.get("source_status", {}), "items": items}
        save(store)
        sys.exit(0)
    print(f"Kullanılan AI sağlayıcı: {provider} | Bekleyen kayıt: {len(pending)}")

    pending.sort(key=lambda k: priority(items[k]))

    skipped_free = 0
    to_call_ai = []
    for key in pending:
        item = items[key]
        tur_tahmini = FREE_SKIP_DETAILS.get(item.get("olasi_tur"))
        if tur_tahmini:
            item["details"] = empty_details(tur_tahmini)
            skipped_free += 1
        else:
            to_call_ai.append(key)

    if args.limit:
        to_call_ai = to_call_ai[: args.limit]

    print(f"Ücretsiz filtre ile atlanan (muhtemel sonuç ilanı / haber): {skipped_free}")
    print(f"AI'ya gönderilecek kayıt: {len(to_call_ai)}")

    store = {"last_updated": classified.get("last_updated"),
              "source_status": classified.get("source_status", {}), "items": items}
    save(store)

    processed = 0
    try:
        for key in to_call_ai:
            item = items[key]
            text, err = fetch_text(item["url"])
            if err:
                print(f"  atlandı ({err}): {item['title'][:60]}")
                continue
            try:
                details = call_ai(provider, api_key, text)
                details["ai_ile_dogrulandi"] = True
                details["ai_saglayici"] = provider
                item["details"] = details
                processed += 1
                print(f"  OK [{details.get('tur')}]: {item['title'][:60]}")
            except QuotaExceeded as e:
                print(f"\nAPI kotası/token limiti doldu ({e}).")
                print(f"Bu çalıştırmada {processed} kayıt işlendi, kalanlar bir sonraki çalıştırmada devam edecek.")
                break
            except Exception as e:
                print(f"  AI hata (bu kayıt atlandı, devam ediliyor): {e} -> {item['title'][:60]}")

            store["items"] = items
            save(store)
            time.sleep(0.4)
    finally:
        store["items"] = items
        save(store)

    remaining = sum(1 for k in to_call_ai if "details" not in items[k])
    print(f"\nTamamlandı. Ücretsiz: {skipped_free} | AI ile işlenen: {processed}"
          + (f" | Bir sonraki çalıştırmaya kalan: {remaining}" if remaining else ""))


if __name__ == "__main__":
    main()
