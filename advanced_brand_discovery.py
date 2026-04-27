import os
import re
import logging
from collections import defaultdict
from typing import List, Dict, Tuple, Set

import numpy as np
import pandas as pd
import clickhouse_connect
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from rapidfuzz import fuzz
import json
from openai import OpenAI
import jellyfish
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# Konfigurasi Logging Standar
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# KONFIGURASI THRESHOLD
# =============================================================================
MAX_DF_THRESHOLD       = 0.005   # Buang N-Gram yang muncul di lebih dari 0.5% listing
NGRAM_RANGE            = (1, 2)  # Unigram & Bigram
MIN_OCCURRENCES_TO_EVALUATE = 2

# [FIX A] Panjang skeleton minimum agar kandidat boleh masuk Phonetic & Fuzz.
# Kata dengan skeleton < batas ini langsung ke LLM — terlalu pendek untuk
# di-match secara sinyal suara/tulang tanpa risiko collision tinggi.
MIN_SKELETON_FOR_MATCHING = 5

# [FIX B] Panjang kode Metaphone minimum agar dianggap tidak ambigu.
# Kode pendek seperti "SN", "KT" bisa tumbukan banyak kata berbeda.
MIN_PHONETIC_CODE_LENGTH = 4

# [FIX B] Rasio panjang skeleton kandidat vs master brand yang masih wajar.
# Di luar rentang ini, phonetic match diabaikan meski kode identik.
PHONETIC_LENGTH_RATIO_MIN = 0.60   # kandidat boleh 40% lebih pendek dari master
PHONETIC_LENGTH_RATIO_MAX = 1.67   # kandidat boleh 67% lebih panjang dari master

# Skeleton Fuzz threshold (tidak diubah dari versi asli)
SKELETON_FUZZ_THRESHOLD = 95.0

# =============================================================================
# CUSTOM STOPWORDS — hanya kata e-commerce generic, BUKAN domain spesifik.
# Domain-specific stopwords (kopi, skincare, dst) dihandle dinamis oleh LLM.
# =============================================================================
CUSTOM_STOPWORDS = frozenset([
    # Kata promosi / logistik e-commerce
    'murah', 'promo', 'gratis', 'ongkir', 'terlaris', 'premium', 'asli', 'murni',
    'original', 'official', 'resmi', 'ready', 'stock', 'eceran', 'paket', 'bundle',
    'custom', 'merk', 'brand',
    # Satuan / kemasan
    'gram', 'kilo', 'liter', 'ml', 'sachet', 'kemasan', 'botol', 'kaleng', 'bungkus',
    'pcs', 'pack', 'dus', 'karton',
    # Sertifikasi
    'halal', 'bpom', 'sertifikat', 'certified', 'collagen', 'kolagen', 'beans', 'bean',
    # Atribut produk generic
    'instan', 'instant', 'powder', 'bubuk', 'cair', 'spray', 'dried', 'grade',
    # Demografi
    'pria', 'wanita', 'dewasa', 'anak',
    # Rasa / sifat generic
    'manis', 'pahit', 'rasa', 'aroma', 'wangi',
])


class DatabaseConnector:
    """Menangani koneksi ke ClickHouse."""

    def __init__(self):
        load_dotenv()
        self.host     = os.getenv("CLICKHOUSE_HOST")
        self.port     = int(os.getenv("CLICKHOUSE_PORT", 8123))
        self.db       = os.getenv("CLICKHOUSE_DB")
        self.user     = os.getenv("CLICKHOUSE_USER")
        self.password = os.getenv("CLICKHOUSE_PASSWORD")
        self.client   = None

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

    def fetch_unbranded_items(self, table: str) -> pd.DataFrame:
        """Mengambil Listing Name dari item berstatus 'No Brand'."""
        query = f"""
        SELECT ItemId, ListingLink, ListingName, DailySalesValue
        FROM {table}
        WHERE Brand = 'No Brand'
        """
        logger.info("Mendownload unbranded items dari database...")
        result = self.client.query_df(query)
        logger.info(f"Mendapatkan {len(result)} baris unbranded items.")
        return result

    def fetch_master_brands(self, table: str) -> Set[str]:
        """Mengambil Master Brand yang sudah ada."""
        query = f"""
        SELECT DISTINCT Brand
        FROM {table}
        WHERE Brand != 'No Brand' AND Brand != ''
        """
        logger.info("Mendownload Master Brands...")
        result = self.client.query_df(query)
        brands = set(result['Brand'].dropna().astype(str).str.title().tolist())
        logger.info(f"Mendapatkan {len(brands)} master brands.")
        return brands

    def close(self):
        pass


class TextProcessor:
    """Mengolah teks dan mengekstrak N-Gram menggunakan TF-IDF."""

    @staticmethod
    def clean_text(text: str) -> str:
        if pd.isna(text):
            return ""
        text = str(text).lower()
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def discover_ngrams(self, df: pd.DataFrame) -> Dict:
        logger.info("Memulai proses Auto-Discovery TF-IDF N-Grams...")

        df['cleaned_name'] = df['ListingName'].apply(self.clean_text)
        corpus = df['cleaned_name'].tolist()

        vectorizer = TfidfVectorizer(
            ngram_range=NGRAM_RANGE,
            max_df=MAX_DF_THRESHOLD,
            min_df=2,
            stop_words=list(CUSTOM_STOPWORDS),
            token_pattern=r'(?u)\b[a-zA-Z]{3,}\b'
        )

        logger.info("Memfitting TfidfVectorizer pada Corpus...")
        vectorizer.fit(corpus)
        valid_vocabulary = set(vectorizer.vocabulary_.keys())
        logger.info(f"Ditemukan {len(valid_vocabulary)} valid N-grams.")

        ngram_stats = defaultdict(lambda: {"count": 0, "total_gmv": 0.0, "samples": []})

        logger.info("Menghitung frekuensi & GMV untuk valid N-grams...")
        for _, row in df.iterrows():
            cleaned_text = row['cleaned_name']
            gmv = float(row['DailySalesValue']) if pd.notna(row['DailySalesValue']) else 0.0
            sample_data = {
                "ItemId":      row.get('ItemId', ''),
                "ListingName": row.get('ListingName', ''),
                "ListingLink": row.get('ListingLink', '')
            }

            words = cleaned_text.split()
            limit = min(len(words), 4)

            for i in range(limit):
                ug = words[i]
                if ug in valid_vocabulary:
                    ngram_stats[ug]["count"]     += 1
                    ngram_stats[ug]["total_gmv"] += gmv
                    if len(ngram_stats[ug]["samples"]) < 3:
                        ngram_stats[ug]["samples"].append(sample_data)

            for i in range(limit - 1):
                bg = f"{words[i]} {words[i+1]}"
                if bg in valid_vocabulary:
                    ngram_stats[bg]["count"]     += 1
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
    """
    Mesin Pengecek Sinonim — Phonetic + Skeleton Fuzz.

    Perubahan vs versi lama:
      [FIX A] Kandidat dengan skeleton < MIN_SKELETON_FOR_MATCHING langsung
              skip matching → dikembalikan sebagai 'Short Candidate (To LLM)'.
      [FIX B] Phonetic match hanya lolos jika:
              - Panjang kode Metaphone >= MIN_PHONETIC_CODE_LENGTH
              - Rasio panjang skeleton kandidat/master dalam batas wajar
    """

    def __init__(self, master_brands: List[str]):
        self.master_brands    = [str(b).strip() for b in master_brands]
        self.master_skeletons = [self._get_skeleton(b) for b in self.master_brands]
        self.master_phonetics = [self._get_phonetic(b) for b in self.master_brands]
        logger.info(f"SemanticBrandMatcher initialized dengan {len(self.master_brands)} Master Brand.")

    def _get_skeleton(self, text: str) -> str:
        return re.sub(r'[^a-z0-9]', '', str(text).lower())

    def _get_phonetic(self, text: str) -> str:
        skel = self._get_skeleton(text)
        return jellyfish.metaphone(skel)

    def match(self, candidate: str) -> Tuple[str, str, float]:
        """
        Return: (Status, Suggested Master Brand, Score)

        Status kemungkinan:
          - 'Existing Brand'           → exact match 100%
          - 'Auto-Matched (Synonym)'   → phonetic atau skeleton fuzz lolos
          - 'Short Candidate (To LLM)' → skeleton terlalu pendek, serahkan LLM
          - 'New Brand Discovery'      → tidak ada match, serahkan LLM
        """
        cand_lower    = str(candidate).lower()
        cand_skel     = self._get_skeleton(candidate)
        cand_phonetic = self._get_phonetic(candidate)

        # -- Lapis 1: Exact Match (100%) — selalu dicek tanpa syarat panjang
        for master in self.master_brands:
            if cand_lower == master.lower():
                return "Existing Brand", master, 100.0

        # -- [FIX A] Guard panjang skeleton sebelum phonetic & fuzz
        # Kata pendek (skeleton < 5 char) tidak punya cukup sinyal untuk
        # dibandingkan secara akurat → langsung lempar ke LLM.
        if len(cand_skel) < MIN_SKELETON_FOR_MATCHING:
            return "Short Candidate (To LLM)", "", 0.0

        best_skeleton_score = 0.0
        best_master_match   = ""

        # -- Lapis 2 & 3: Iterasi Master Brand
        for idx, master in enumerate(self.master_brands):
            m_skel     = self.master_skeletons[idx]
            m_phonetic = self.master_phonetics[idx]

            # -- Lapis 2: Phonetic Match
            # [FIX B-1] Kode fonetik harus cukup panjang agar tidak ambigu
            # [FIX B-2] Rasio panjang skeleton harus masuk akal
            if cand_phonetic != "" and cand_phonetic == m_phonetic:
                code_long_enough = len(cand_phonetic) >= MIN_PHONETIC_CODE_LENGTH
                length_ratio     = len(cand_skel) / max(len(m_skel), 1)
                ratio_sane       = PHONETIC_LENGTH_RATIO_MIN <= length_ratio <= PHONETIC_LENGTH_RATIO_MAX

                if code_long_enough and ratio_sane:
                    return "Auto-Matched (Synonym)", master, 99.0

            # -- Lapis 3: Skeleton Fuzz (cari skor terbaik)
            sim_score = fuzz.ratio(cand_skel, m_skel)
            if sim_score > best_skeleton_score:
                best_skeleton_score = sim_score
                best_master_match   = master

        # -- Lapis 3: Evaluasi Skeleton Fuzz
        if best_skeleton_score >= SKELETON_FUZZ_THRESHOLD:
            return "Auto-Matched (Synonym)", best_master_match, best_skeleton_score

        # Tidak ada match → ke LLM
        return "New Brand Discovery", "", best_skeleton_score


class EmbeddingPreFilter:
    """
    Stage 1 dari HybridValidator.

    Menggunakan sentence-transformers (lokal, gratis, tanpa API) untuk memisahkan
    kandidat ke dalam 3 bucket sebelum menyentuh LLM:

      ACCEPT   → embedding context jauh lebih dekat ke pola brand
                 → langsung lolos, tidak perlu LLM
      REJECT   → embedding context jauh lebih dekat ke pola generik
                 → langsung buang, tidak perlu LLM
      AMBIGUOUS → sinyal tidak cukup kuat ke salah satu sisi
                 → diteruskan ke LLM untuk keputusan final

    MENGAPA CONTEXT-ENRICHED ENCODING:
    Model sentence-transformers ditraining untuk similarity antar kalimat/frasa,
    bukan kata tunggal. Encode kata "Pak" atau "Ban" secara langsung menghasilkan
    vector yang hampir tidak punya sinyal semantik — delta brand vs generic-nya
    mendekati nol, sehingga semua masuk AMBIGUOUS.

    Solusinya: setiap kandidat di-wrap dalam template kalimat kontekstual sebelum
    di-encode. "Pak" menjadi "produk dengan merek Pak" — model kini punya konteks
    yang cukup untuk membedakan apakah kata itu berperan sebagai brand atau bukan.

    Anchor juga di-encode dalam kalimat, bukan kata tunggal, agar embedding
    space-nya simetris dan comparable.

    Model default: paraphrase-multilingual-MiniLM-L12-v2
      - Support Bahasa Indonesia secara native
      - Ukuran ~471MB, download otomatis pertama kali dari HuggingFace
      - Inference cepat di CPU, tidak butuh GPU
      - Tidak butuh Ollama, Docker, atau server apapun
    """

    # Anchor BRAND — dikemas dalam kalimat kontekstual agar embedding-nya kaya
    BRAND_ANCHORS = [
        "produk dengan merek Indomie",
        "produk dengan merek Aqua",
        "produk dengan merek Samsung",
        "produk dengan merek Wardah",
        "produk dengan merek Maybelline",
        "produk dengan merek Torabika",
        "produk dengan merek Good Day",
        "produk dengan merek Kapal Api",
        "produk dengan merek Sosro",
        "produk dengan merek Mie Sedaap",
        "produk dengan merek Chitato",
        "produk dengan merek Pringles",
        "produk dengan merek Dove",
        "produk dengan merek Lifebuoy",
        "produk dengan merek Garnier",
        "ini adalah nama brand atau merek dagang resmi",
        "nama perusahaan atau brand yang menjual produk ini",
    ]

    # Anchor GENERIK — dikemas dalam kalimat kontekstual
    GENERIC_ANCHORS = [
        "produk murah berkualitas terbaik",
        "harga promo diskon gratis ongkir",
        "ready stock original asli premium",
        "kemasan sachet botol kaleng bubuk serbuk cair",
        "rasa aroma wangi pahit manis pedas gurih",
        "nama daerah wilayah kota provinsi kabupaten",
        "ini bukan nama brand, ini deskripsi produk",
        "kata sifat atau keterangan produk bukan merek",
        "istilah promosi atau jenis produk generik",
        "kata umum yang bukan merupakan nama brand",
    ]

    # Template kalimat untuk wrap kandidat sebelum di-encode
    CANDIDATE_TEMPLATE = "produk dengan merek {}"

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"):
        logger.info(f"Memuat embedding model: {model_name} ...")
        logger.info("(Download otomatis ~471MB jika belum ada di cache HuggingFace)")
        self.model = SentenceTransformer(model_name)

        logger.info("Encoding anchor vectors (brand & generic)...")
        self._brand_vec   = self._mean_encode(self.BRAND_ANCHORS)
        self._generic_vec = self._mean_encode(self.GENERIC_ANCHORS)
        logger.info("EmbeddingPreFilter siap.")

    def _mean_encode(self, texts: List[str]) -> np.ndarray:
        """Encode list teks dan ambil rata-rata vektornya sebagai centroid."""
        vecs = self.model.encode(texts, batch_size=64, show_progress_bar=False)
        return np.mean(vecs, axis=0, keepdims=True)

    def _enrich(self, candidate: str) -> str:
        """Wrap kandidat dalam template kalimat kontekstual."""
        return self.CANDIDATE_TEMPLATE.format(candidate)

    def classify(
        self,
        candidates: List[str],
        accept_threshold: float = 0.10,
        reject_threshold: float = 0.08,
    ) -> Dict[str, List[str]]:
        """
        Klasifikasikan kandidat ke tiga bucket.

        Setiap kandidat di-wrap dulu via _enrich() sebelum di-encode,
        sehingga model mendapat konteks kalimat yang cukup.

        accept_threshold : delta (sim_brand - sim_generic) >= nilai ini → ACCEPT
        reject_threshold : delta <= -nilai ini → REJECT
        Sisanya → AMBIGUOUS

        Threshold sengaja lebih kecil dari versi sebelumnya karena delta
        cosine similarity antar kalimat yang mirip secara template lebih
        rapat — sinyal bermakna sudah mulai di kisaran 0.08-0.10.

        Return: dict dengan keys 'accept', 'reject', 'ambiguous'
        """
        if not candidates:
            return {"accept": [], "reject": [], "ambiguous": []}

        # Enrichment — wrap semua kandidat dalam template kalimat
        enriched = [self._enrich(c) for c in candidates]

        logger.info(f"[Embedding] Encoding {len(candidates)} kandidat (context-enriched)...")
        cand_vecs = self.model.encode(
            enriched, batch_size=128, show_progress_bar=False
        )

        sim_brand   = cosine_similarity(cand_vecs, self._brand_vec).flatten()
        sim_generic = cosine_similarity(cand_vecs, self._generic_vec).flatten()
        delta       = sim_brand - sim_generic

        result: Dict[str, List[str]] = {"accept": [], "reject": [], "ambiguous": []}
        for i, cand in enumerate(candidates):
            if delta[i] >= accept_threshold:
                result["accept"].append(cand)
            elif delta[i] <= -reject_threshold:
                result["reject"].append(cand)
            else:
                result["ambiguous"].append(cand)

        logger.info(
            f"[Embedding] Hasil klasifikasi — "
            f"ACCEPT: {len(result['accept'])} | "
            f"REJECT: {len(result['reject'])} | "
            f"AMBIGUOUS (→ LLM): {len(result['ambiguous'])}"
        )
        logger.info(
            f"[Embedding] Delta stats — "
            f"min: {delta.min():.4f} | max: {delta.max():.4f} | mean: {delta.mean():.4f}"
        )
        return result


class LLMJudge:
    """
    Stage 2 dari HybridValidator.

    Hanya menerima kandidat AMBIGUOUS dari EmbeddingPreFilter — jumlahnya
    sudah jauh lebih sedikit dari total kandidat, sehingga biaya & waktu
    turun drastis vs melempar semua kandidat ke LLM.

    Perubahan vs LLMValidator lama:
      - Domain context dinamis (tidak hardcode 'kopi')
      - Strict mode untuk kandidat pendek
      - Default ke model DeepSeek yang valid + fallback model
    """

    def __init__(self):
        # Pastikan env terbaca walau LLMJudge dipakai standalone.
        load_dotenv()
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        self.client  = OpenAI(
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key=self.api_key if self.api_key else "dummy_key",
        )
        requested_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        # Alias umum yang sering dipakai user tapi bukan model id resmi API DeepSeek.
        model_alias_map = {
            "deepseek-v3.2": "deepseek-chat",
            "deepseek-v3-2": "deepseek-chat",
            "deepseek-v3": "deepseek-chat",
            "v3.2": "deepseek-chat",
        }
        self.model = model_alias_map.get(requested_model.strip().lower(), requested_model.strip())

        fallback_raw = os.getenv("DEEPSEEK_FALLBACK_MODELS", "deepseek-reasoner")
        self.fallback_models = [
            m.strip() for m in fallback_raw.split(",")
            if m.strip() and m.strip() != self.model
        ]

    def _request_with_model_fallback(self, messages: List[Dict[str, str]]):
        models_to_try = [self.model, *self.fallback_models]
        last_error = None

        for model_name in models_to_try:
            try:
                logger.info(f"[LLM] Mencoba model: {model_name}")
                return self.client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=4096,
                    timeout=120.0,
                )
            except Exception as e:
                last_error = e
                err_text = str(e).lower()
                # Jika model invalid/tidak ada, coba model fallback berikutnya.
                if "model not exist" in err_text or "invalid_request_error" in err_text:
                    logger.warning(f"[LLM] Model '{model_name}' gagal: {e}. Mencoba fallback...")
                    continue
                # Untuk error lain (network/auth/rate limit), langsung lempar.
                raise

        raise last_error if last_error else RuntimeError("LLM request gagal tanpa detail error.")

    def filter_brands(
        self,
        candidates: List[str],
        domain_context: str = "",
        strict_mode: bool = False,
    ) -> Set[str]:
        if not candidates:
            return set()

        if not self.api_key:
            logger.warning("DEEPSEEK_API_KEY tidak disetel. Melewati LLM, semua AMBIGUOUS dianggap valid.")
            return set(candidates)

        mode_label = "SHORT-WORD STRICT" if strict_mode else "NORMAL"
        logger.info(
            f"[LLM {mode_label}] Mengirim {len(candidates)} kandidat ambiguous "
            f"(domain: '{domain_context or 'generic'}')..."
        )

        domain_line = (
            f"Produk yang dijual adalah: **{domain_context}**."
            if domain_context
            else "Produk yang dijual bersifat umum (e-commerce multi-kategori)."
        )

        if strict_mode:
            strictness_instruction = (
                "PERHATIAN: Daftar ini berisi kata-kata SANGAT PENDEK (≤4 karakter). "
                "Hanya loloskan jika YAKIN BENAR itu singkatan merek terkenal "
                "(contoh: 'BMW', 'KFC', 'LG', '3M'). Jika ragu, BUANG."
            )
        else:
            strictness_instruction = (
                "Buang: kata sifat umum, deskripsi produk, nama geografis biasa, "
                "istilah promosi, singkatan tidak jelas. "
                "Loloskan: nama merek tulen termasuk brand UMKM lokal yang unik."
            )

        prompt = f"""Tugasmu memfilter kandidat nama merek (brand) dari e-commerce Indonesia.

{domain_line}
{strictness_instruction}

Kembalikan HANYA JSON array string yang LOLOS. Tanpa penjelasan, tanpa markdown.

Kandidat:
{json.dumps(candidates, ensure_ascii=False)}"""

        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a brand name validation system for Indonesian e-commerce. "
                        "Output only a raw JSON array of valid brand name strings. "
                        "No markdown, no explanation."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            response = self._request_with_model_fallback(messages)
            raw = response.choices[0].message.content.strip()

            if raw.startswith("```json"):
                raw = raw[7:].rsplit("```", 1)[0].strip()
            elif raw.startswith("```"):
                raw = raw[3:].rsplit("```", 1)[0].strip()

            valid_brands = self._safe_parse_json_array(raw)
            if valid_brands is None:
                logger.error("LLM tidak mengembalikan JSON array yang bisa diparsing. Batch ini diabaikan.")
                return set()

            return set(str(b).strip().lower() for b in valid_brands)

        except Exception as e:
            logger.error(f"Error pada LLM API: {e}")
            return set()

    @staticmethod
    def _safe_parse_json_array(raw: str) -> list | None:
        """
        Parsing JSON array secara robust — 3 lapisan fallback:

        1. json.loads() langsung (happy path)
        2. Cari blok [...] terluar lalu parse
        3. Salvage: potong di string item terakhir yang valid sebelum truncation,
           tutup array secara manual, parse ulang.

        Return list jika berhasil, None jika semua cara gagal.
        """
        # Lapisan 1 — parsing normal
        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Lapisan 2 — ekstrak blok [...] terluar
        start = raw.find("[")
        end   = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                result = json.loads(raw[start:end + 1])
                if isinstance(result, list):
                    logger.warning("[LLM Parser] Lapisan 2: berhasil ekstrak blok [...] terluar.")
                    return result
            except json.JSONDecodeError:
                pass

        # Lapisan 3 — salvage response truncated
        # Cari posisi koma terakhir yang diikuti whitespace/newline sebelum truncation,
        # asumsi setiap item adalah string — potong di sana lalu tutup array.
        if start != -1:
            truncated = raw[start:]
            last_comma = truncated.rfind('",')
            if last_comma == -1:
                last_comma = truncated.rfind('"')
            if last_comma != -1:
                salvaged = truncated[:last_comma + 1].rstrip().rstrip(",") + "]"
                try:
                    result = json.loads(salvaged)
                    if isinstance(result, list):
                        logger.warning(
                            f"[LLM Parser] Lapisan 3 (salvage): respons truncated, "
                            f"berhasil salvage {len(result)} item."
                        )
                        return result
                except json.JSONDecodeError:
                    pass

        return None


class HybridValidator:
    """
    Validator utama yang menggabungkan EmbeddingPreFilter + LLMJudge.

    Alur per pool kandidat:
      1. EmbeddingPreFilter.classify() → pisahkan ACCEPT / REJECT / AMBIGUOUS
      2. ACCEPT  → langsung LLM Verified Brand (tanpa LLM)
      3. REJECT  → langsung Rejected by Embedding (tanpa LLM)
      4. AMBIGUOUS → LLMJudge.filter_brands() → LLM Verified Brand / Rejected by LLM

    Estimasi penghematan vs full LLM:
      - 30k kandidat masuk embedding → ~21k langsung resolved (70%)
      - Hanya ~9k yang butuh LLM
      - Biaya token turun ~70%, waktu total turun signifikan
    """

    # Label status output — konsisten dipakai di PipelineManager
    STATUS_VERIFIED   = "LLM Verified Brand"
    STATUS_REJ_EMBED  = "Rejected by Embedding (Generic)"
    STATUS_REJ_SHORT  = "Rejected (Too Short / Generic)"
    STATUS_REJ_LLM    = "Rejected by LLM (Generic/Region)"

    def __init__(self):
        self.embedder = EmbeddingPreFilter()
        self.llm      = LLMJudge()

    def validate_pool(
        self,
        candidates: List[str],
        domain_context: str = "",
        strict_mode: bool = False,
        llm_batch_size: int = 500,
    ) -> Dict[str, str]:
        """
        Validasi satu pool kandidat.

        Return: dict {candidate_name_lower: status_string}
        """
        if not candidates:
            return {}

        # Stage 1 — Embedding pre-filter
        buckets = self.embedder.classify(candidates)

        result: Dict[str, str] = {}

        # ACCEPT — lolos tanpa LLM
        reject_status = self.STATUS_REJ_SHORT if strict_mode else self.STATUS_REJ_EMBED
        for cand in buckets["accept"]:
            result[cand.lower()] = self.STATUS_VERIFIED

        # REJECT — buang tanpa LLM
        for cand in buckets["reject"]:
            result[cand.lower()] = reject_status

        # AMBIGUOUS — lempar ke LLM
        ambiguous = buckets["ambiguous"]
        if ambiguous:
            verified_by_llm: Set[str] = set()
            for i in range(0, len(ambiguous), llm_batch_size):
                batch = ambiguous[i: i + llm_batch_size]
                verified_by_llm.update(
                    self.llm.filter_brands(
                        batch,
                        domain_context=domain_context,
                        strict_mode=strict_mode,
                    )
                )

            llm_reject_status = self.STATUS_REJ_SHORT if strict_mode else self.STATUS_REJ_LLM
            for cand in ambiguous:
                cand_skel = re.sub(r'[^a-z0-9]', '', cand.lower())
                matched = any(
                    cand_skel == re.sub(r'[^a-z0-9]', '', v)
                    or cand_skel in re.sub(r'[^a-z0-9]', '', v)
                    or re.sub(r'[^a-z0-9]', '', v) in cand_skel
                    for v in verified_by_llm
                )
                result[cand.lower()] = self.STATUS_VERIFIED if matched else llm_reject_status

        return result


class PipelineManager:
    """Manajer pengontrol eksekusi utama."""

    def __init__(self):
        self.db               = DatabaseConnector()
        self.text_processor   = TextProcessor()
        self.semantic_matcher = None

    def run(
        self,
        table: str          = "default.dm_Kopi_rpr_copy_al",
        domain_context: str = "",
        output_file: str    = "advanced_brand_discovery_result.xlsx",
    ):
        """
        table          : nama tabel ClickHouse yang akan diproses
        domain_context : konteks kategori produk untuk prompt LLM dinamis.
                         Contoh: "produk kopi", "kosmetik & skincare", "power tools"
                         Biarkan kosong untuk mode generic.
        output_file    : path output Excel
        """
        logger.info("=== Memulai Advanced Brand Auto-Discovery Pipeline ===")
        logger.info(f"  Table         : {table}")
        logger.info(f"  Domain context: '{domain_context or 'generic (tidak dispesifikasi)'}'")

        # 1. Tarik Data
        self.db.connect()
        master_brands = self.db.fetch_master_brands(table)
        self.semantic_matcher = SemanticBrandMatcher(master_brands)
        unbranded_items = self.db.fetch_unbranded_items(table)
        self.db.close()

        # 2. Extract TF-IDF
        ngram_stats = self.text_processor.discover_ngrams(unbranded_items)

        # 3. Semantic & Phonetic Matching
        logger.info("Validasi Kandidat menggunakan SemanticBrandMatcher...")
        candidates = []
        for cand_name, data in ngram_stats.items():
            if data["count"] < MIN_OCCURRENCES_TO_EVALUATE:
                continue

            status, exact_brand, score = self.semantic_matcher.match(cand_name)

            if score == 100.0:
                status = "Existing Brand"

            candidates.append({
                "Candidate Name":        cand_name.title(),
                "Occurrences":           data["count"],
                "Total GMV":             data["total_gmv"],
                "Status":                status,
                "Suggested Exact Brand": exact_brand,
                "Match Score (%)":       round(score, 2),
                "Example Items":         self.text_processor.format_samples(data["samples"]),
            })

        # 4. Hybrid Validation (Embedding pre-filter + LLM untuk ambiguous)
        # Inisialisasi di sini supaya model embedding hanya dimuat satu kali
        # meskipun ada dua pool (Short + New Brand Discovery)
        hybrid = HybridValidator()

        # Pool A — Short Candidate (skeleton < 5 char) → strict mode
        short_idx = [i for i, c in enumerate(candidates) if c["Status"] == "Short Candidate (To LLM)"]
        logger.info(f"Pool A (Short Candidate, strict): {len(short_idx)} kandidat.")

        if short_idx:
            short_names  = [candidates[i]["Candidate Name"] for i in short_idx]
            short_result = hybrid.validate_pool(
                short_names,
                domain_context=domain_context,
                strict_mode=True,
            )
            for i, idx in enumerate(short_idx):
                cname = candidates[idx]["Candidate Name"]
                candidates[idx]["Status"] = short_result.get(
                    cname.lower(), HybridValidator.STATUS_REJ_SHORT
                )

        # Pool B — New Brand Discovery → normal mode
        new_idx = [i for i, c in enumerate(candidates) if c["Status"] == "New Brand Discovery"]
        logger.info(f"Pool B (New Brand Discovery, normal): {len(new_idx)} kandidat.")

        if new_idx:
            new_names  = [candidates[i]["Candidate Name"] for i in new_idx]
            new_result = hybrid.validate_pool(
                new_names,
                domain_context=domain_context,
                strict_mode=False,
            )
            for idx in new_idx:
                cname = candidates[idx]["Candidate Name"]
                candidates[idx]["Status"] = new_result.get(
                    cname.lower(), HybridValidator.STATUS_REJ_LLM
                )

        # 5. Export Excel
        result_df = pd.DataFrame(candidates)
        result_df = result_df.sort_values(by="Total GMV", ascending=False).reset_index(drop=True)

        logger.info(f"Menyimpan hasil pipeline ke {output_file}...")
        result_df.to_excel(output_file, index=False)

        # 6. Summary log
        summary = result_df["Status"].value_counts()
        logger.info("=== SUMMARY HASIL ===")
        for status, count in summary.items():
            logger.info(f"  {status:<45} : {count:>6}")
        logger.info("Pipeline Berhasil Diselesaikan! 🚀")


if __name__ == "__main__":
    pipeline = PipelineManager()
    pipeline.run(
        table="default.dm_Kopi_rpr_copy_al",
        # Ganti domain_context sesuai kategori DM yang sedang diproses.
        # Contoh: "kosmetik & skincare", "makanan & minuman", "power tools & perkakas"
        domain_context="produk kopi",
        output_file="advanced_brand_discovery_result.xlsx",
    )