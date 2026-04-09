import re
import pandas as pd
from collections import defaultdict

# Konfigurasi Input Output
INPUT_FILE = "ch_ui_export_2026-04-08T10-27-37.xlsx"
OUTPUT_FILE = "ngram_brand_discovery_result.xlsx"
MIN_OCCURRENCES = 2

# Daftar Stopwords Khusus E-Commerce (Bisa Ditambah Sendiri)
STOPWORDS = {
    "kopi", "susu", "roast", "beans", "premium", "blend",
    "bubuk", "biji", "espresso", "robusta", "arabica", "arabika",
    "murah", "asli", "pria", "dewasa", "stamina", "gula", "aren",
    "cair", "liter", "house", "tanpa", "ampas", "pahit",
    "promo", "sale", "diskon", "1kg", "kg", "gram", "gr",
    "kilo", "kemasan", "box", "sachet", "isi", "bpom", "halal",
    "super", "kualitas", "excellent", "minuman", "rasa", "diet",
    "khas", "nusantara", "original", "ori", "by", "for", "and", "dan"
}

def clean_text(text):
    if pd.isna(text):
        return ""
    # Ubah ke huruf kecil, hapus karakter spesial
    text = str(text).lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    # Hapus spasi ganda
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_ngrams(text, n):
    words = text.split()
    # Filter stopwords standar di e-commerce
    filtered_words = [w for w in words if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]
    
    ngrams = []
    # Khusus untuk e-commerce, nama brand biasanya ada di paling depan.
    # Kita cukup ambil N-gram yang berasal dari kombinasi kata ke-1 sampai ke-3
    limit = min(len(filtered_words), 4) # Ambil di 4 kata terdepan yang tersisa
    for i in range(limit - n + 1):
        ngrams.append(" ".join(filtered_words[i:i+n]))
    return ngrams

def run_discovery():
    print(f"Loading data from {INPUT_FILE}...")
    try:
        df = pd.read_excel(INPUT_FILE)
    except FileNotFoundError:
        print(f"Error: File {INPUT_FILE} tidak ditemukan.")
        return

    # Pastikan nama kolom sesuai
    if "ListingName" not in df.columns or "Item_GMV" not in df.columns:
        print("Error: Kolom 'ListingName' atau 'Item_GMV' tidak ditemukan.")
        return

    print("Memproses N-gram (Unigram dan Bigram)...")
    
    ngram_stats = defaultdict(lambda: {"count": 0, "total_gmv": 0.0})

    for index, row in df.iterrows():
        listing_name = row["ListingName"]
        gmv = float(row["Item_GMV"]) if pd.notna(row["Item_GMV"]) else 0.0

        cleaned_name = clean_text(listing_name)
        
        # Ekstrak bigram (kombinasi 2 kata terdepan) - paling sering menjadi pattern brand
        bigrams = get_ngrams(cleaned_name, 2)
        for bg in bigrams:
            ngram_stats[bg]["count"] += 1
            ngram_stats[bg]["total_gmv"] += gmv

        # Ekstrak unigram (1 kata) sebagai backup jika brand hanya 1 suku kata
        unigrams = get_ngrams(cleaned_name, 1)
        for ug in unigrams:
            ngram_stats[ug]["count"] += 1
            ngram_stats[ug]["total_gmv"] += gmv

    # Format Output
    print("Mengelompokkan dan Mengurutkan hasil...")
    results = []
    for ngram, stats in ngram_stats.items():
        if stats["count"] >= MIN_OCCURRENCES:
            results.append({
                "N-Gram Candidate": ngram.title(),
                "Occurrences": stats["count"],
                "Total GMV": stats["total_gmv"]
            })

    # Konversi ke DataFrame
    res_df = pd.DataFrame(results)
    
    # Urutkan berdasarkan Total GMV tertinggi
    res_df = res_df.sort_values(by="Total GMV", ascending=False).reset_index(drop=True)

    # Simpan ke Excel
    res_df.to_excel(OUTPUT_FILE, index=False)
    print(f"\nSelesai! Berhasil mengidentifikasi {len(res_df)} kandidat nama brand potensial.")
    print(f"Silakan periksa file: {OUTPUT_FILE}")
    
    # Tampilkan Top 10 di Terminal
    print("\n--- TOP 10 KANDIDAT BRAND POTENSIAL (Berdasarkan GMV) ---")
    print(res_df.head(10).to_markdown())

if __name__ == "__main__":
    run_discovery()
