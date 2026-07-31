#!/usr/bin/env python3
"""
Türkiye hibe/destek programı duyurularını sources.json'daki kaynaklardan tarar
ve data/duyurular.json'a kaydeder.

Her kaynak için:
  1. sources.json'da tanımlı olası "path"ler sırayla denenir (ör. /duyurular,
     /destekler, /haberler...). İçinde yeterli sayıda link bulunan ilk sayfa
     kullanılır (basit bir "otomatik keşif" mekanizması, çünkü 30+ farklı
     sitenin URL yapısını tek tek elle doğrulamak mümkün değil).
  2. Sayfadaki linkler arasından "duyuru gibi görünenler" (yeterince uzun
     başlık metni olan, menü/altbilgi olmayan) seçilir.
  3. Link'in bulunduğu blokta bir tarih aranır (DD.MM.YYYY veya "05 Tem 2026"
     gibi formatlar).

Bu yaklaşım %100 hatasız değildir — her sitenin HTML yapısı farklı olduğu için
bazı kaynaklarda gürültülü (alakasız link) veya eksik sonuç alınabilir. İlk
çalıştırmadan sonra data/duyurular.json'daki sonuçlara bakıp sources.json
içindeki path/link_contains alanlarını ilgili kaynak için inceltmek gerekir.

Kullanım:
    python scrape.py                # tüm kaynakları tarar
    python scrape.py --only kosgeb  # sadece belirli bir kaynağı tarar (test için)
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HibeTakipBot/1.0; +https://github.com/)"}
ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "duyurular.json"
SOURCES_FILE = ROOT / "sources.json"
TIMEOUT = 15
MIN_TITLE_LEN = 12
MIN_LINKS_TO_ACCEPT_PAGE = 4

# Menü/altbilgi/erişilebilirlik linklerinde sık geçen, duyuru başlığı OLMAYAN kelimeler.
NOISE_WORDS = {
    "anasayfa", "iletişim", "hakkımızda", "kurumsal", "giriş", "kayıt ol",
    "gizlilik", "çerez", "sıkça sorulan", "site haritası", "erişilebilirlik",
    "facebook", "twitter", "instagram", "linkedin", "youtube", "e-bülten",
    "devamını oku", "read more", "tıklayınız", "detaylı bilgi", "paylaş",
}

HIBE_KEYWORDS = [
    "hibe", "destek", "çağrı", "cagri", "başvuru", "basvuru", "fon",
    "program", "teklif çağrısı", "grant", "proje çağrısı", "burs",
]
SONUC_KEYWORDS = [
    "sonuçlandı", "sonuclandi", "kazananlar", "ilan edildi",
    "değerlendirme tamamlandı", "degerlendirme tamamlandi",
    "başarılı bulunan", "basarili bulunan", "asil liste", "asıl liste",
    "yedek liste", "kabul edilen projeler",
]


def guess_type(title):
    """Ücretsiz, kod-tabanlı ön tahmin. AI'ya gitmeden önce sıralama/öncelik
    amaçlı kullanılır — hiçbir kaydı silmez, sadece 'olası_tur' etiketler."""
    t = title.lower()
    has_hibe = any(k in t for k in HIBE_KEYWORDS)
    has_sonuc = any(k in t for k in SONUC_KEYWORDS)
    if has_sonuc:
        return "sonuc_olabilir"
    if has_hibe:
        return "hibe_olabilir"
    return "belirsiz"


TR_MONTHS = {
    "oca": "01", "şub": "02", "sub": "02", "mar": "03", "nis": "04",
    "may": "05", "haz": "06", "tem": "07", "ağu": "08", "agu": "08",
    "eyl": "09", "eki": "10", "kas": "11", "ara": "12",
}
EN_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10",
    "nov": "11", "dec": "12",
}


def parse_date(text):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    m = re.search(r"(\d{1,2})\s+([A-Za-zŞşĞğÜüÖöÇçİı]{3,})\s+(\d{4})", text)
    if m:
        d, mon, y = m.groups()
        mon_key = mon.lower()[:3].replace("i̇", "i")
        mnum = TR_MONTHS.get(mon_key) or EN_MONTHS.get(mon_key)
        if mnum:
            return f"{y}-{mnum}-{int(d):02d}"
    return None


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def looks_like_announcement(a, link_contains):
    title = a.get_text(strip=True)
    href = a.get("href", "")
    if not title or len(title) < MIN_TITLE_LEN:
        return False
    if title.lower() in NOISE_WORDS:
        return False
    if any(w in title.lower() for w in NOISE_WORDS):
        # Kısa menü metinleriyle tam eşleşme dışında, çok kısa başlıkları da ele
        if len(title) < 20:
            return False
    if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
        return False
    if link_contains:
        if not any(token in href for token in link_contains):
            return False
    return True


def scrape_page(url, link_contains):
    soup = fetch(url)
    candidates = soup.find_all("a", href=True)
    entries = []
    for a in candidates:
        if not looks_like_announcement(a, link_contains):
            continue
        title = a.get_text(strip=True)
        href = urljoin(url, a["href"])
        block = a.find_parent(["div", "li", "article"]) or a
        date_iso = parse_date(block.get_text(" ", strip=True))
        entries.append({
            "title": title,
            "url": href,
            "date": date_iso,
            "olasi_tur": guess_type(title),
        })
    return entries


def scrape_source(source):
    """Bir kaynağın olası path'lerini sırayla dener, yeterli link bulan ilkini kullanır."""
    base = source["homepage"].rstrip("/")
    link_contains = source.get("link_contains") or []
    best = []
    tried = []
    for path in source["paths"]:
        url = base + path if path.startswith("/") else base + "/" + path
        tried.append(url)
        try:
            entries = scrape_page(url, link_contains)
        except Exception as e:
            print(f"  [{source['id']}] {url} -> hata: {e}")
            continue
        # aynı domain içindeki linkleri tercih et
        entries = [e for e in entries if urlparse(e["url"]).netloc == urlparse(base).netloc]
        # tekilleştir
        seen = set()
        uniq = []
        for e in entries:
            if e["url"] not in seen:
                seen.add(e["url"])
                uniq.append(e)
        if len(uniq) >= MIN_LINKS_TO_ACCEPT_PAGE:
            print(f"  [{source['id']}] OK: {url} -> {len(uniq)} olası duyuru")
            best = uniq
            break
        elif len(uniq) > len(best):
            best = uniq
        time.sleep(0.3)
    if not best:
        print(f"  [{source['id']}] UYARI: denenen hiçbir sayfada yeterli içerik bulunamadı: {tried}")
    for e in best:
        e["source"] = source["name"]
        e["source_id"] = source["id"]
    return best


def load_existing():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"last_updated": None, "items": {}, "source_status": {}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Sadece bu source id'sini tara (test için)")
    args = parser.parse_args()

    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
        if not sources:
            print(f"'{args.only}' id'li kaynak sources.json içinde bulunamadı.")
            sys.exit(1)

    store = load_existing()
    items = store.get("items", {})
    source_status = store.get("source_status", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    added_total = 0
    for source in sources:
        print(f"Taranıyor: {source['name']}")
        try:
            entries = scrape_source(source)
            source_status[source["id"]] = {"ok": True, "checked": today, "found": len(entries)}
        except Exception as e:
            print(f"  [{source['id']}] KAYNAK ERİŞİLEMEDİ: {e}")
            source_status[source["id"]] = {"ok": False, "checked": today, "error": str(e)}
            entries = []

        added = 0
        for entry in entries:
            key = entry["url"]
            if key not in items:
                entry["first_seen"] = today
                items[key] = entry
                added += 1
            else:
                items[key]["title"] = entry["title"] or items[key]["title"]
                if entry.get("date"):
                    items[key]["date"] = entry["date"]
                items[key]["olasi_tur"] = entry.get("olasi_tur", items[key].get("olasi_tur"))
        added_total += added
        print(f"  -> yeni: {added}")

    store["items"] = items
    store["source_status"] = source_status
    store["last_updated"] = today
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nToplam kayıt: {len(items)} | Bu çalıştırmada yeni: {added_total}")


if __name__ == "__main__":
    main()
