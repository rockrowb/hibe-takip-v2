#!/usr/bin/env python3
"""
KATMAN 2/3 — ÜCRETSİZ SINIFLANDIRMA

data/raw.json'ı okur, hiçbir ağ isteği ATMADAN (tamamen çevrimdışı, ücretsiz,
hızlı) her kaydın başlığına bakarak bir ön tahmin ("olasi_tur") üretir ve
data/classified.json'a yazar:

    hibe_olabilir   -> başlıkta hibe/destek/çağrı/başvuru gibi kelimeler var
    sonuc_olabilir  -> başlıkta "sonuçlandı", "kazananlar açıklandı" gibi kalıplar var
    haber_olabilir  -> başlıkta genel haber/basın bülteni kalıpları var (video,
                        ziyaret, imza töreni, toplantı vb.) ama hibe kelimesi yok
    belirsiz        -> hiçbiri net değil

Bu etiket HİÇBİR KAYDI SİLMEZ ya da GİZLEMEZ — sadece (a) panelin varsayılan
sekme yerleşimine ve (b) enrich.py'ın hangi kayıtları AI'ya göndermeden
ücretsiz eleyeceğine karar vermek için kullanılır.

Kurallar/anahtar kelimeler değiştiğinde SADECE bu script yeniden çalıştırılır
— siteleri tekrar taramaya gerek yoktur, çünkü girdi (raw.json) zaten diskte
duruyor. Bu yüzden data/raw.json'u SİLMEK YERİNE, sınıflandırma mantığını
düzeltip bu scripti tekrar çalıştırmak yeterlidir.

Kullanım:
    python classify.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
RAW_FILE = ROOT / "data" / "raw.json"
CLASSIFIED_FILE = ROOT / "data" / "classified.json"

HIBE_KEYWORDS = [
    "hibe", "destek", "çağrı", "cagri", "başvuru", "basvuru", "fon",
    "program", "teklif çağrısı", "grant", "proje çağrısı", "burs",
    "yarışma başvur", "competition apply",
]

SONUC_KEYWORDS = [
    "sonuçlandı", "sonuclandi", "kazananlar", "ilan edildi",
    "değerlendirme tamamlandı", "degerlendirme tamamlandi",
    "başarılı bulunan", "basarili bulunan", "asil liste", "asıl liste",
    "yedek liste", "kabul edilen projeler",
    "sonuçları açıklandı", "sonucu açıklandı", "sonuçlarını açıkladı",
    "sonuçları belli oldu", "kazananları belli oldu", "dereceye giren",
    "ödül töreni", "ödül aldı", "birinci oldu",
]

# Genel haber/basın bülteni kalıpları — bir hibe çağrısı DEĞİL, ama "sonuç"
# da değil (ör. ziyaret, imza töreni, eğitim, video/rapor paylaşımı).
HABER_KEYWORDS = [
    "ziyaret etti", "ziyaret gerçekleştir", "imza töreni", "imzalandı",
    "açılışını gerçekleştir", "açılışı yapıldı", "toplantısı gerçekleştirildi",
    "toplantı düzenlendi", "video", "simülasyon raporu", "sunum sonuçları",
    "eğitim verildi", "eğitimi düzenlendi", "ile buluştu", "kutlandı",
    "tebrik", "davet edildi", "katıldı", "katılım sağladı", "ağırladı",
    "işbirliği protokolü", "protokol imzalandı", "bilgilendirme toplantısı",
    "farkındalık", "anma etkinliği", "fuarına katıldı",
]


def guess_type(title):
    t = title.lower()
    has_sonuc = any(k in t for k in SONUC_KEYWORDS)
    has_hibe = any(k in t for k in HIBE_KEYWORDS)
    has_haber = any(k in t for k in HABER_KEYWORDS)

    if has_sonuc:
        return "sonuc_olabilir"
    if has_haber and not has_hibe:
        return "haber_olabilir"
    if has_hibe:
        return "hibe_olabilir"
    return "belirsiz"


def main():
    if not RAW_FILE.exists():
        print("data/raw.json bulunamadı, önce scrape.py çalıştırılmalı.")
        return

    raw = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    raw_items = raw.get("items", {})

    classified = {}
    counts = {"hibe_olabilir": 0, "sonuc_olabilir": 0, "haber_olabilir": 0, "belirsiz": 0}
    for url, item in raw_items.items():
        tur = guess_type(item.get("title", ""))
        counts[tur] += 1
        classified[url] = {**item, "olasi_tur": tur}

    out = {
        "last_updated": raw.get("last_updated"),
        "source_status": raw.get("source_status", {}),
        "items": classified,
    }
    CLASSIFIED_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[classified.json] Toplam: {len(classified)} | " +
          " | ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
