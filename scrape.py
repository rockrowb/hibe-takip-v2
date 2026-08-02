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
import ssl
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
ROOT = Path(__file__).parent
RAW_FILE = ROOT / "data" / "raw.json"
SOURCES_FILE = ROOT / "sources.json"
TIMEOUT = 20
MIN_TITLE_LEN = 12
MIN_LINKS_TO_ACCEPT_PAGE = 4
RETRYABLE_STATUS = {500, 502, 503, 504}


class LegacySSLAdapter(HTTPAdapter):
    """Bazı eski/özel yapılandırılmış sunucular (ör. bazı .gov.tr siteleri)
    modern OpenSSL varsayılanlarıyla 'handshake failure' hatası veriyor.
    Bu adaptör güvenlik seviyesini bilinçli olarak biraz düşürüp (SECLEVEL=1)
    bu tip eski sunucularla da bağlantı kurabilmeyi sağlar. Sadece normal
    bağlantı SSLError ile başarısız olduğunda, ikinci deneme olarak
    kullanılır."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


_legacy_session = requests.Session()
_legacy_session.mount("https://", LegacySSLAdapter())

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


def parse_all_dates(text):
    """Bloktaki TÜM tarihleri (tekrarsız, sırayla) döndürür — ör. bir kaynak
    listelemesinde 'Başlangıç: ... Bitiş: ...' gibi iki tarih birden
    geçiyorsa ikisini de yakalar. Bu, ka.gov.tr gibi sitelerde AI'ya hiç
    gitmeden doğrudan tarih bilgisi elde etmek için kullanılır."""
    found = []
    for d, mo, y in re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", text):
        iso = f"{y}-{mo}-{d}"
        if iso not in found:
            found.append(iso)
    return found


def fetch(url):
    """Normal istek dener; SSL hatası alırsa (bazı eski .gov.tr sunucuları)
    esnetilmiş bir SSL bağlamıyla bir kez daha dener; 500/502/503/504 gibi
    geçici sunucu hatalarında kısa bir bekleme sonrası bir kez daha dener."""
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code in RETRYABLE_STATUS and attempt == 0:
                time.sleep(2.0)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser"), r.status_code, len(r.text)
        except requests.exceptions.SSLError as e:
            last_exc = e
            try:
                r = _legacy_session.get(url, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                return BeautifulSoup(r.text, "html.parser"), r.status_code, len(r.text)
            except Exception as e2:
                last_exc = e2
                break
        except requests.exceptions.HTTPError as e:
            last_exc = e
            if attempt == 0 and e.response is not None and e.response.status_code in RETRYABLE_STATUS:
                time.sleep(2.0)
                continue
            raise
    raise last_exc


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
    soup, status, html_len = fetch(url)
    candidates = soup.find_all("a", href=True)
    entries = []
    for a in candidates:
        if not looks_like_announcement(a, link_contains):
            continue
        title = a.get_text(strip=True)
        href = urljoin(url, a["href"])
        block = a.find_parent(["div", "li", "article"]) or a
        block_text = block.get_text(" ", strip=True)
        date_iso = parse_date(block_text)
        # Ham, AI'sız tarih yakalama: bloktaki TÜM tarihler (ör. ka.gov.tr'de
        # "Teklif Teslimi Başlangıç/Bitiş Tarihi" gibi iki tarih birden
        # geçebiliyor). En sonuncusu genelde son başvuru/bitiş tarihidir —
        # panel bunu AI çalışmasa bile ön-tahmin olarak kullanabilir.
        all_dates = parse_all_dates(block_text)
        entries.append({"title": title, "url": href, "date": date_iso, "on_tarihler": all_dates})
    diag = f"HTTP {status}, {html_len} byte HTML, {len(candidates)} link, {len(entries)} olası duyuru"
    return entries, diag


def scrape_source(source):
    base = source["homepage"].rstrip("/")
    link_contains = source.get("link_contains") or []
    best = []
    tried = []
    last_diag = None
    for path in source["paths"]:
        url = base + path if path.startswith("/") else base + "/" + path
        tried.append(url)
        try:
            entries, diag = scrape_page(url, link_contains)
            last_diag = f"{url} -> {diag}"
        except requests.exceptions.Timeout:
            last_diag = f"{url} -> ZAMAN AŞIMI ({TIMEOUT}sn içinde yanıt gelmedi)"
            print(f"  [{source['id']}] {last_diag}")
            continue
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            last_diag = f"{url} -> HTTP {code} (muhtemelen erişim engellendi/bot koruması)"
            print(f"  [{source['id']}] {last_diag}")
            continue
        except Exception as e:
            last_diag = f"{url} -> hata: {e}"
            print(f"  [{source['id']}] {last_diag}")
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
    return best, last_diag


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
            entries, diag = scrape_source(source)
            source_status[source["id"]] = {
                "ok": len(entries) > 0, "checked": today, "found": len(entries),
                "diag": diag,  # HTTP durum kodu + kaç link/duyuru bulundu — "0 sonuç"un GERÇEK sebebini gösterir
            }
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
                if entry.get("on_tarihler"):
                    items[key]["on_tarihler"] = entry["on_tarihler"]
        added_total += added
        print(f"  -> yeni: {added}")
        time.sleep(1.0)  # kaynaklar arasında kısa bekleme — art arda çok hızlı istek atıp
                          # bot-koruması tetiklemekten kaçınmak için

    store["items"] = items
    store["source_status"] = source_status
    store["last_updated"] = today
    RAW_FILE.parent.mkdir(exist_ok=True)
    RAW_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[raw.json] Toplam kayıt: {len(items)} | Bu çalıştırmada yeni: {added_total}")


if __name__ == "__main__":
    main()
