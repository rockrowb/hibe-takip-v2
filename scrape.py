#!/usr/bin/env python3
"""
KATMAN 1/3 — HAM VERİ TOPLAMA

Türkiye hibe/destek programı duyurularını sources.json'daki kaynaklardan tarar
ve SADECE data/raw.json'a yazar. Hiçbir sınıflandırma/AI işlemi burada
YAPILMAZ — bu bilinçli bir tasarım tercihi:

  data/raw.json         <- bu script yazar (ham: başlık/tarih/link)
  data/classified.json  <- classify.py yazar (+ ücretsiz olasi_tur etiketi)
  data/duyurular.json   <- enrich.py yazar (+ AI ile çıkarılan detaylar)

Neden ayrı katmanlar: sınıflandırma kuralları ya da AI mantığı değiştiğinde
(ör. yeni anahtar kelime eklendi, yeni AI alanı eklendi) SADECE ilgili script
yeniden çalıştırılır — siteleri tekrar taramaya (yavaş, kotaya bağlı) gerek
kalmaz. data/raw.json bir kere toplanan hiçbir kaydı SİLMEZ, sadece üzerine
ekler/günceller; bu yüzden bir kaynağın path'i bozulsa/değişse bile önceden
toplanmış kayıtlar kalıcı olarak saklanır.

Her kaynak için:
  1. sources.json'da tanımlı olası "path"ler sırayla denenir (ör. /duyurular,
     /destekler, /haberler...). İçinde yeterli sayıda link bulunan ilk sayfa
     kullanılır (basit bir "otomatik keşif" mekanizması).
  2. Sayfadaki linkler arasından "duyuru gibi görünenler" seçilir.
  3. Link'in bulunduğu blokta bir tarih aranır.

Not: Bazı siteler (özellikle JavaScript ile içerik yükleyen tek-sayfa
uygulamaları) bu basit HTTP+HTML yaklaşımıyla taranamaz — sayfa boş gelir.
Bu durumda "0 sonuç" alınır; bu, o kaynağın ileride farklı bir yöntem
(headless tarayıcı vb.) gerektirdiğinin işaretidir.

Kullanım:
    python scrape.py                # tüm kaynakları tarar
    python scrape.py --only kosgeb  # sadece belirli bir kaynağı tarar (test için)
"""
import argparse
import re
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}
ROOT = Path(__file__).parent
RAW_FILE = ROOT / "data" / "raw.json"
SOURCES_FILE = ROOT / "sources.json"
TIMEOUT = 20
MIN_TITLE_LEN = 12
MIN_LINKS_TO_ACCEPT_PAGE = 4

NOISE_WORDS = {
    "anasayfa", "iletişim", "hakkımızda", "kurumsal", "giriş", "kayıt ol",
    "gizlilik", "çerez", "sıkça sorulan", "site haritası", "erişilebilirlik",
    "facebook", "twitter", "instagram", "linkedin", "youtube", "e-bülten",
    "devamını oku", "read more", "tıklayınız", "detaylı bilgi", "paylaş",
}

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
    if any(w in title.lower() for w in NOISE_WORDS) and len(title) < 20:
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
        entries.append({"title": title, "url": href, "date": date_iso})
    return entries


def scrape_source(source):
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
        entries = [e for e in entries if urlparse(e["url"]).netloc == urlparse(base).netloc]
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
    if RAW_FILE.exists():
        return json.loads(RAW_FILE.read_text(encoding="utf-8"))
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
                # ÖNEMLİ: kayıt SİLİNMEZ/değiştirilmez, sadece başlık/tarih tazelenir.
                items[key]["title"] = entry["title"] or items[key]["title"]
                if entry.get("date"):
                    items[key]["date"] = entry["date"]
        added_total += added
        print(f"  -> yeni: {added}")

    store["items"] = items
    store["source_status"] = source_status
    store["last_updated"] = today
    RAW_FILE.parent.mkdir(exist_ok=True)
    RAW_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[raw.json] Toplam kayıt: {len(items)} | Bu çalıştırmada yeni: {added_total}")


if __name__ == "__main__":
    main()
