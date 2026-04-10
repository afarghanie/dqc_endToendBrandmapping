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
import json
from openai import OpenAI
import re
import jellyfish

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

        logger.info(f"Pengekstrakan selesai: {len(ngram_stats)} raw kandidat diperoleh.")
        return ngram_stats

    def format_samples(self, samples: list) -> str:
        return " || ".join([
            f"ID: {s.get('ItemId', '')} | Name: {s.get('ListingName', '')} | Link: {s.get('ListingLink', '')}"
            for s in samples
        ])

class SemanticBrandMatcher:
    """Mesin Pengecek Sinonim Menggunakan Phonetic dan Skeleton Fuzz."""
    def __init__(self, master_brands: List[str]):
        # Simpan form asli
        self.master_brands = [str(b).strip() for b in master_brands]
        # Simpan form skeleton
        self.master_skeletons = [self._get_skeleton(b) for b in self.master_brands]
        # Simpan form fonetik
        self.master_phonetics = [self._get_phonetic(b) for b in self.master_brands]
        logger.info(f"SemanticBrandMatcher initialized dengan {len(self.master_brands)} Master Brand.")

    def _get_skeleton(self, text: str) -> str:
        # Buang semua yang bukan alfabet atau angka, jadikan string tancap tanpa spasi
        return re.sub(r'[^a-z0-9]', '', str(text).lower())

    def _get_phonetic(self, text: str) -> str:
        # Membuang spasi dkk lalu ambil kode phonetic sound (Metaphone Code)
        skel = self._get_skeleton(text)
        return jellyfish.metaphone(skel)

    def match(self, candidate: str) -> Tuple[str, str, float]:
        """
        Mencari padanan synonym semantic. 
        Return format: (Status, Master Brand Name, Score)
        """
        cand_lower = str(candidate).lower()
        cand_skel = self._get_skeleton(candidate)
        cand_phonetic = self._get_phonetic(candidate)
        
        # -- Lapis 1: Exact Match (Sangat Akurat 100%)
        for master in self.master_brands:
            if cand_lower == master.lower():
                return "Existing Brand", master, 100.0

        best_skeleton_score = 0.0
        best_master_match = ""

        # -- Lapis 2 & 3: Iterasi Master untuk Phonetic dan Skeleton Fuzz
        for idx, master in enumerate(self.master_brands):
            m_skel = self.master_skeletons[idx]
            m_phonetic = self.master_phonetics[idx]

            # Lapis 2: Rule Phonetic Identik => Lolos mutlak
            if cand_phonetic != "" and cand_phonetic == m_phonetic:
                return "Auto-Matched (Synonym)", master, 99.0  # Skor semu batas kepercayaaan tertinggi
            
            # Cari Skeleton Fuzz terbaik (Lapis 3)
            sim_score = fuzz.ratio(cand_skel, m_skel)
            if sim_score > best_skeleton_score:
                best_skeleton_score = sim_score
                best_master_match = master

        # -- Lapis 3: Evaluasi Skeleton Fuzz (Batas: 95.0%)
        # Angka ini harus ditekan ekstrim, karena teks tak ada spasi, 95% = wajib nyaris sama total hurufnya.
        if best_skeleton_score >= 95.0:
             return "Auto-Matched (Synonym)", best_master_match, best_skeleton_score

        # Gagal Lapis 1, 2, dan 3 => Ditolak
        return "New Brand Discovery", "", best_skeleton_score

class LLMValidator:
    """Filter akhir menggunakan OpenRouter LLM untuk membuang term generik/daerah yang tersisa."""
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key if self.api_key else "dummy_key",
        )
        self.model = os.getenv("LLM_MODEL", "openai/gpt-5-nano")
        
    def filter_brands(self, candidates: List[str]) -> Set[str]:
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY tidak disetel di .env. Melewati proses LLM, asumsi semua terverifikasi.")
            return set(candidates)
            
        logger.info(f"Mengirim {len(candidates)} kandidat ke API LLM OpenRouter ({self.model})...")
        
        prompt = f"""Tugasmu adalah menganalisis daftar kandidat frasa/kalimat dari e-commerce produk kopi:
HAPUS semua frasa yang berisi/mengandung kata-kata umum, deskripsi kemasan, kualitas barang, merk palsu/receh, jenis minuman generik, dan nama daerah geografis asli (sepeerti temanggung, aceh, sidikalang, gayo, dsb) YANG BUKAN bagian hak paten brand.
Kembalikan HANYA JSON array dari string yang BENAR-BENAR merupakan Merek (Brand Name) tulen. Jangan pakai formatting markdown.

Daftar Kandidat:
{json.dumps(candidates)}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a Brand Name extraction system. Only output a raw JSON array of valid brand names."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                timeout=120.0 # Batal otomatis jika nyangkut lebih dari 120 detik
            )
            raw_response = response.choices[0].message.content.strip()
            
            # Sanitasi JSON jikalau AI terobsesi mengirim format markdown markdown
            if raw_response.startswith("```json"):
                raw_response = raw_response[7:-3].strip()
            elif raw_response.startswith("```"):
                raw_response = raw_response[3:-3].strip()
                
            valid_brands = json.loads(raw_response)
            
            # Jika JSON tidak array
            if not isinstance(valid_brands, list):
                logger.error("LLM tidak mengembalikan JSON array. Mengabaikan validasi...")
                return set(candidates)
                
            return set([str(b).strip().lower() for b in valid_brands])
        except Exception as e:
            logger.error(f"Error pada OpenRouter API: {e}")
            return set(candidates)

class PipelineManager:
    """Manajer pengontrol eksekusi utama."""
    def __init__(self):
        self.db = DatabaseConnector()
        self.text_processor = TextProcessor()
        self.semantic_matcher = None

    def run(self, output_file: str = "advanced_brand_discovery_result.xlsx"):
        logger.info("=== Memulai Advanced Brand Auto-Discovery Pipeline ===")
        
        # 1. Tarik Data
        self.db.connect()
        master_brands = self.db.fetch_master_brands()
        self.semantic_matcher = SemanticBrandMatcher(master_brands)
        
        unbranded_items = self.db.fetch_unbranded_items()
        self.db.close()
        
        # 2. Extract TF-IDF
        ngram_stats = self.text_processor.discover_ngrams(unbranded_items)
        
        # 3. Validasi Semantic & Phonetic per Kandidat
        logger.info("Validasi Kandidat menggunakan Semantic Phonetic Matcher terhadap Master Brands...")
        candidates = []
        for cand_name, data in ngram_stats.items():
            if data["count"] < MIN_OCCURRENCES_TO_EVALUATE:
                continue
                
            status, exact_brand, score = self.semantic_matcher.match(cand_name)
            
            candidate = {
                "Candidate Name": cand_name.title(),
                "Occurrences": data["count"],
                "Total GMV": data["total_gmv"],
                "Status": status,
                "Suggested Exact Brand": exact_brand,
                "Match Score (%)": round(score, 2),
                "Example Items": self.text_processor.format_samples(data["samples"])
            }
            
            # Jika di match sempurna 100%, status rubah
            if score == 100.0:
                candidate["Status"] = "Existing Brand"
            
            candidates.append(candidate)
                
        # 4. Validasi LLM Eksekusi Terakhir
        new_brands_index = [i for i, c in enumerate(candidates) if c["Status"] == "New Brand Discovery"]
        logger.info(f"Ditemukan {len(new_brands_index)} kandidat dengan status 'New Brand Discovery'.")

        if new_brands_index:
            llm_validator = LLMValidator()
            new_brands_names = [candidates[i]["Candidate Name"] for i in new_brands_index]
            
            verified_brands = set()
            BATCH_SIZE = 2000 # Batch menembak OpenRouter (Dinaikkan dari 100 ke 2000 untuk efisiensi kecepatan)
            for i in range(0, len(new_brands_names), BATCH_SIZE):
                batch = new_brands_names[i : i+BATCH_SIZE]
                accepted_batch = llm_validator.filter_brands(batch)
                verified_brands.update(accepted_batch)
                
            # Update status original candidates
            for idx in new_brands_index:
                cand_name = candidates[idx]["Candidate Name"].lower()
                
                # Cek apakah dia diloloskan (ada di verified brands)
                # Gunakan semantic skel tipis in case JSON reformat string by LLM
                cand_skel = re.sub(r'[^a-z0-9]', '', cand_name)
                match_found = False
                for v in verified_brands:
                    v_skel = re.sub(r'[^a-z0-9]', '', v)
                    if cand_skel == v_skel or cand_skel in v_skel or v_skel in cand_skel:
                        match_found = True
                        break
                
                if match_found:
                    candidates[idx]["Status"] = "LLM Verified Brand"
                else:
                    candidates[idx]["Status"] = "Rejected by LLM (Generic/Region)"
                
        # 5. Finalizing Excel
        result_df = pd.DataFrame(candidates)
        
        # Urutkan berdasarkan total kontribusi GMV tertinggi
        result_df = result_df.sort_values(by="Total GMV", ascending=False).reset_index(drop=True)

        logger.info(f"Menyimpan hasil pipeline ke {output_file}...")
        result_df.to_excel(output_file, index=False)
        logger.info("Pipeline Berhasil Diselesaikan! 🚀")

if __name__ == "__main__":
    pipeline = PipelineManager()
    pipeline.run()
