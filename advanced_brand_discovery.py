import os
import re
import logging
from collections import defaultdict
from typing import List, Dict, Tuple, Set

import pandas as pd
import clickhouse_connect
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from rapidfuzz import process, fuzz

# Konfigurasi Logging Standar
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Konfigurasi Threshold
FUZZY_MATCH_THRESHOLD = 83.0
MAX_DF_THRESHOLD = 0.005      # Buang N-Gram yang muncul di lebih dari 0.5% listing (Stopword otomatis agresif)
NGRAM_RANGE = (1, 2)         # Menggunakan Unigram & Bigram sekaligus
MIN_OCCURRENCES_TO_EVALUATE = 2

# Filter Domain Kopi & E-commerce
CUSTOM_STOPWORDS = frozenset([
    'kopi', 'coffee', 'coffe', 'cofe', 'koffe', 'koffie', 'bubuk', 'instan', 'instant', 
    'tanpa', 'custom', 'gula', 'aren', 'biji', 'bean', 'beans', 'roast', 'roasted',
    'gayo', 'aceh', 'kintamani', 'arabica', 'arabika', 'robusta', 'blend', 'premium',
    'murah', 'asli', 'murni', 'liter', 'kemasan', 'sertifikat', 'bpom', 'halal', 
    'pria', 'wanita', 'stamina', 'dewasa', 'pahit', 'manis', 'rasa', 'terlaris',
    'promo', 'gratis', 'ongkir', 'kilo', 'gram', 'sachet', 'ampas', 'hijau', 'green', 'paket', 'kemudi',
    'jawa','barat', 'spray', 'dried', 'racik', 'merk', 'sunda', 'collagen', 'grade', 'powder','eceran', 'creamy', 'espresso', 'latte', 'khas', 'official'
])

class DatabaseConnector:
    """Menangani koneksi ke ClickHouse."""
    def __init__(self):
        load_dotenv()
        self.host = os.getenv("CLICKHOUSE_HOST")
        self.port = int(os.getenv("CLICKHOUSE_PORT", 8123))
        self.db = os.getenv("CLICKHOUSE_DB")
        self.user = os.getenv("CLICKHOUSE_USER")
        self.password = os.getenv("CLICKHOUSE_PASSWORD")
        self.client = None

    def connect(self):
        logger.info(f"Connecting to ClickHouse at {self.host}...")
        self.client = clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            database=self.db,
            username=self.user,
            password=self.password
        )
        logger.info("Successfully connected to ClickHouse.")

    def fetch_unbranded_items(self) -> pd.DataFrame:
        """Mengambil Listing Name dari item berstatus 'No Brand'."""
        query = """
        SELECT ItemId, ListingLink, ListingName, DailySalesValue 
        FROM default.dm_Kopi_rpr_copy_al
        WHERE Brand = 'No Brand'
        """
        logger.info("Mendownload unbranded items dari database...")
        result = self.client.query_df(query)
        logger.info(f"Mendapatkan {len(result)} baris unbranded items.")
        return result

    def fetch_master_brands(self) -> Set[str]:
        """Mengambil Master Brand yang sudah ada."""
        query = """
        SELECT DISTINCT Brand 
        FROM default.dm_Kopi_rpr_copy_al
        WHERE Brand != 'No Brand' AND Brand != ''
        """
        logger.info("Mendownload Master Brands...")
        result = self.client.query_df(query)
        brands = set(result['Brand'].dropna().astype(str).str.title().tolist())
        logger.info(f"Mendapatkan {len(brands)} master brands.")
        return brands

    def close(self):
        # clickhouse-connect relies on requests/urllib3 sessions, explicit close isn't strictly necessary,
        # but keep method for interface completeness.
        pass

class TextProcessor:
    """Mengolah teks dan mengekstrak N-Gram menggunakan TF-IDF (sebagai Auto Stopwords)."""
    @staticmethod
    def clean_text(text: str) -> str:
        if pd.isna(text):
            return ""
        text = str(text).lower()
        # Biarkan huruf dan angka saja
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def discover_ngrams(self, df: pd.DataFrame) -> List[Dict]:
        """
        N-Gram Auto-Discovery dengan bantuan TF-IDF Document Frequency
        sebagai filter dinamis penghancur Generic Words.
        """
        logger.info("Memulai proses Auto-Discovery TF-IDF N-Grams...")
        
        # 1. Bersihkan semua title
        df['cleaned_name'] = df['ListingName'].apply(self.clean_text)
        corpus = df['cleaned_name'].tolist()

        # 2. Gunakan TF-IDF untuk membuang term frequency yang terlalu agresif / massal (Auto Stopwords)
        # max_df=MAX_DF_THRESHOLD artinya membuang term yang muncul > % dokumen
        # min_df=2 artinya membuang term yang muncul hanya sekali di dataset
        vectorizer = TfidfVectorizer(
            ngram_range=NGRAM_RANGE,
            max_df=MAX_DF_THRESHOLD,
            min_df=2, 
            stop_words=list(CUSTOM_STOPWORDS),
            token_pattern=r'(?u)\b[a-zA-Z]{3,}\b' # Hanya mengambil huruf (tanpa angka) yg panjang minimum 3 karakter
        )
        
        logger.info("Memfitting TfidfVectorizer pada Corpus...")
        vectorizer.fit(corpus)
        valid_vocabulary = set(vectorizer.vocabulary_.keys())
        logger.info(f"Ditemukan {len(valid_vocabulary)} kombinasi valid N-grams (setelah diiris max_df).")

        # 3. Aggregasi Jumlah dan GMV untuk N-gram yang selamat dari seleksi TF-IDF
        ngram_stats = defaultdict(lambda: {"count": 0, "total_gmv": 0.0, "samples": []})

        logger.info("Menghitung frekuensi & DailySalesValue untuk valid N-grams...")
        # Lakukan manual traversal untuk perhitungan akurat di top 4 words saja
        for _, row in df.iterrows():
            cleaned_text = row['cleaned_name']
            gmv = float(row['DailySalesValue']) if pd.notna(row['DailySalesValue']) else 0.0
            
            # Persiapkan dict sample
            sample_data = {
                "ItemId": row.get('ItemId', ''),
                "ListingName": row.get('ListingName', ''),
                "ListingLink": row.get('ListingLink', '')
            }
            
            words = cleaned_text.split()
            # Ambil N-Gram dari maximum 4 kata terdepan
            limit = min(len(words), 4)

            # Unigrams
            for i in range(limit):
                ug = words[i]
                if ug in valid_vocabulary:
                    ngram_stats[ug]["count"] += 1
                    ngram_stats[ug]["total_gmv"] += gmv
                    if len(ngram_stats[ug]["samples"]) < 3:
                        ngram_stats[ug]["samples"].append(sample_data)
            
            # Bigrams
            for i in range(limit - 1):
                bg = f"{words[i]} {words[i+1]}"
                if bg in valid_vocabulary:
                    ngram_stats[bg]["count"] += 1
                    ngram_stats[bg]["total_gmv"] += gmv
                    if len(ngram_stats[bg]["samples"]) < 3:
                        ngram_stats[bg]["samples"].append(sample_data)

        # 4. Format hasil
        results = []
        for ngram, stats in ngram_stats.items():
            if stats["count"] >= MIN_OCCURRENCES_TO_EVALUATE:
                
                samples_str = " || ".join([
                    f"ID: {s['ItemId']} | Name: {s['ListingName']} | Link: {s['ListingLink']}"
                    for s in stats["samples"]
                ])
                
                results.append({
                    "Candidate Name": ngram.title(),
                    "Occurrences": stats["count"],
                    "Total GMV": stats["total_gmv"],
                    "Example Items": samples_str
                })
        
        logger.info(f"Pengekstrakan selesai: {len(results)} kandidat diperoleh.")
        return results

class FuzzyMatcher:
    """Validator menggunakan algoritma Levenshtein (RapidFuzz)."""
    def __init__(self, master_brands: Set[str]):
        self.master_brands = list(master_brands)
        logger.info(f"FuzzyMatcher initialized dengan {len(self.master_brands)} Master Brand.")

    def evaluate_candidate(self, candidate_name: str) -> Tuple[str, str, float]:
        """Bandingkan kandidat ke Master Brand."""
        if not self.master_brands:
            return "New Brand Discovery", "", 0.0

        match = process.extractOne(
            query=candidate_name,
            choices=self.master_brands,
            scorer=fuzz.WRatio
        )
        
        if match:
            best_match_str, best_match_score, _ = match
            if best_match_score >= FUZZY_MATCH_THRESHOLD:
                return "Auto-Matched (Typo)", best_match_str, best_match_score

        return "New Brand Discovery", "", match[1] if match else 0.0

class PipelineManager:
    """Manajer pengontrol eksekusi utama."""
    def __init__(self):
        self.db_connector = DatabaseConnector()
        self.text_processor = TextProcessor()
        self.matcher = None

    def run(self, output_file: str = "advanced_brand_discovery_result.xlsx"):
        logger.info("=== Memulai Advanced Brand Auto-Discovery Pipeline ===")
        
        # 1. Start Connection
        self.db_connector.connect()
        master_brands = self.db_connector.fetch_master_brands()
        self.matcher = FuzzyMatcher(master_brands)
        df_unbranded = self.db_connector.fetch_unbranded_items()
        self.db_connector.close()
        
        if df_unbranded.empty:
            logger.info("Tidak ada data 'No Brand' yang perlu dioleh. Pipeline berhenti.")
            return

        # 2. Process NLP
        candidates = self.text_processor.discover_ngrams(df_unbranded)
        
        # 3. Fuzzy Matching Validations
        logger.info("Validasi Kandidat menggunakan RapidFuzz terhadap Master Brands...")
        for candidate in candidates:
            status, matched_str, score = self.matcher.evaluate_candidate(candidate["Candidate Name"])
            candidate["Status"] = status
            candidate["Suggested Exact Brand"] = matched_str
            candidate["Match Score (%)"] = round(score, 2)
            
            # Jika di match sempurna 100%, status rubah
            if score == 100.0:
                candidate["Status"] = "Existing Brand"
                
        # 4. Finalizing
        result_df = pd.DataFrame(candidates)
        
        # Urutkan berdasarkan total kontribusi GMV tertinggi
        result_df = result_df.sort_values(by="Total GMV", ascending=False).reset_index(drop=True)

        logger.info(f"Menyimpan hasil pipeline ke {output_file}...")
        result_df.to_excel(output_file, index=False)
        logger.info("Pipeline Berhasil Diselesaikan! 🚀")

if __name__ == "__main__":
    pipeline = PipelineManager()
    pipeline.run()
