#!/usr/bin/env python3
"""
OPSİYONEL AI zenginleştirme adımı.

data/duyurular.json içindeki her yeni (henüz "details" alanı olmayan) kayıt
için TEK bir AI çağrısında iki şeyi birden yapar:
  1. Sınıflandırma: bu bir hibe/destek duyurusu mu, yoksa haber/sonuç ilanı/
     başka bir şey mi? ("tur" alanı)
  2. Eğer hibe duyurusuysa: kimler başvurabilir, hibe miktarı, son başvuru
     tarihi, desteklenen aktiviteler alanlarını metinden çıkarır.
     Değilse bu alanlar null bırakılır (gereksiz ayrıntı çıkarılmaz).

İKİ SAĞLAYICI DESTEKLENİR — hangisinin anahtarı tanımlıysa o kullanılır:
  - ANTHROPIC_API_KEY tanımlıysa   -> Claude (claude-sonnet-4-6) kullanılır (öncelikli)
  - yoksa GEMINI_API_KEY tanımlıysa -> Google Gemini (gemini-2.5-flash) kullanılır
  - ikisi de tanımlı değilse        -> script sessizce çıkar, sistemin geri kalanını etkilemez

Token tasarrufu için üç katman var:
  A) scrape.py zaten her kaydı ücretsiz anahtar kelime taramasından geçirip
     "olasi_tur" etiketler (hibe_olabilir / sonuc_olabilir / belirsiz).
  B) Bu script, "sonuc_olabilir" etiketli kayıtları AI'ya HİÇ GÖNDERMEZ —
     doğrudan tur="sonuc_ilani_tahmini" olarak işaretler (ai_ile_dogrulandi=false).
     Bu, en belirgin "sonuçlandı/kazananlar açıklandı" gibi başlıklarda AI
     çağrısını tamamen atlar.
  C) "details" alanı zaten olan kayıtlar (daha önce işlenmiş) tekrar
     gönderilmez — sonraki her çalıştırmada sadece YENİ kayıtlar işlenir.

Kullanım:
    export ANTHROPIC_API_KEY=sk-ant-...    # ya da
    export GEMINI_API_KEY=AIza...
    python enrich.py                # bekleyen tüm kayıtları işler
    python enrich.py --limit 20     # maliyeti sınırlamak için ilk 20 kayıt
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
DATA_FILE = ROOT / "data" / "duyurular.json"
HEADERS_WEB = {"User-Agent": "Mozilla/5.0 (compatible; HibeTakipBot/1.0)"}

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Not: Google, gemini-2.5-flash modelini Ekim 2026'da kullanımdan kaldıracağını
# duyurdu. O tarihten sonra buradaki model adını güncel bir Gemini Flash
# modeliyle değiştirmek gerekebilir (ai.google.dev/api/generate-content'ten kontrol edin).
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """Sana bir Türkiye kamu/STK duyurusunun web sayfası metni verilecek.
Aşağıdaki şemada SADECE JSON döndür, başka hiçbir şey yazma (açıklama, markdown işareti vb. ekleme):

{
  "tur": "hibe_duyurusu" | "haber" | "sonuc_ilani" | "diger",
  "kimler_basvurabilir": "kısa açıklama veya null",
  "hibe_miktari": "tutar/oran bilgisi (ör. '%75 hibe, üst limit 500.000 TL') veya null",
  "son_basvuru_tarihi": "YYYY-MM-DD veya null",
  "desteklenen_aktiviteler": "hangi faaliyetler/harcamalar destekleniyor, kısa liste veya null",
  "ozet": "1-2 cümlelik tarafsız özet"
}

Kurallar:
- "tur" alanını dikkatle belirle: yeni bir hibe/destek/çağrı ilanıysa "hibe_duyurusu";
  genel bir haber/etkinlik duyurusuysa "haber"; başvuru sonuçları/kazananlar/asıl-yedek
  liste açıklanıyorsa "sonuc_ilani"; hiçbiri değilse "diger".
- tur "hibe_duyurusu" DEĞİLSE diğer alanları (kimler_basvurabilir, hibe_miktari,
  son_basvuru_tarihi, desteklenen_aktiviteler) null bırak, sadece ozet'i doldur.
- Emin olmadığın alanları null bırak, metinde olmayan bilgiyi uydurma."""


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
            "max_tokens": 500,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": page_text}],
        },
        timeout=30,
    )
    resp.raise_for_status()
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
                "maxOutputTokens": 500,
                "responseMimeType": "application/json",
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def get_provider():
    """Hangi anahtar tanımlıysa onu döndürür. Anthropic tanımlıysa o önceliklidir."""
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
    """AI bütçesi sınırlıysa en olası hibe duyurularını önce işle."""
    order = {"hibe_olabilir": 0, "belirsiz": 1, "sonuc_olabilir": 2}
    return order.get(item.get("olasi_tur"), 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="AI'ya gönderilecek maksimum kayıt sayısı")
    args = parser.parse_args()

    provider, api_key = get_provider()
    if not provider:
        print("ANTHROPIC_API_KEY ya da GEMINI_API_KEY tanımlı değil — AI zenginleştirme atlanıyor (opsiyonel, sorun değil).")
        sys.exit(0)
    print(f"Kullanılan AI sağlayıcı: {provider}")

    if not DATA_FILE.exists():
        print("data/duyurular.json bulunamadı, önce scrape.py çalıştırılmalı.")
        sys.exit(1)

    store = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    items = store.get("items", {})

    pending = [k for k, v in items.items() if "details" not in v]
    pending.sort(key=lambda k: priority(items[k]))

    skipped_free = 0
    to_call_ai = []
    for key in pending:
        item = items[key]
        if item.get("olasi_tur") == "sonuc_olabilir":
            # Ücretsiz katman: başlık kalıbı çok net "sonuç ilanı" diyor, AI'ya sormaya gerek yok.
            item["details"] = {
                "tur": "sonuc_ilani_tahmini",
                "kimler_basvurabilir": None,
                "hibe_miktari": None,
                "son_basvuru_tarihi": None,
                "desteklenen_aktiviteler": None,
                "ozet": None,
                "ai_ile_dogrulandi": False,
            }
            skipped_free += 1
        else:
            to_call_ai.append(key)

    if args.limit:
        to_call_ai = to_call_ai[: args.limit]

    print(f"Ücretsiz filtre ile atlanan (muhtemel sonuç ilanı): {skipped_free}")
    print(f"AI'ya gönderilecek kayıt: {len(to_call_ai)}")

    processed = 0
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
        except Exception as e:
            print(f"  AI hata: {e} -> {item['title'][:60]}")
        time.sleep(0.4)

    store["items"] = items
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTamamlandı. Ücretsiz: {skipped_free} | AI ile işlenen: {processed}")


if __name__ == "__main__":
    main()
