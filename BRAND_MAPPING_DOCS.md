# 📘 Project Documentation: Automated Brand Discovery & Synonym Mapping Pipeline

## 1. Project Description
**Automated Brand Discovery** adalah inisiatif rekayasa data (*Data Engineering*) guna meningkatkan kualitas pelaporan GMV (Gross Merchandise Value) dengan cara menurunkan drastis persentase item berstatus `"No Brand"`. 
Proyek ini mengatasi masalah keterbatasan manusia dalam mengecek jutaan judul produk e-commerce (*listing name*) yang sering dipenuhi dengan *typo* ejaan, kekurangan/kelebihan spasi, atau *brand* UMKM liar yang belum didaftarkan. Dengan strategi "*Funneling Array*", sistem secara otomatis menemukan, memvalidasi sinonim, dan menyuntikkan (inject) perbaikan pemetaan merek tersebut langsung ke dalam target basis log harian.

---

## 2. Tech Stack & Dependencies
Proyek ini mengawinkan pemrosesan database sekuensial dengan kecerdasan buatan NLP, didukung oleh spesifikasi teknologi:
*   **Database Engine:** ClickHouse (Untuk penarikan batch besar & eksekusi massal `ALTER TABLE UPDATE`).
*   **Bahasa Pemrograman:** Python 3 (Object-Oriented Programming).
*   **Vectorization & NLP:** `scikit-learn` (TF-IDF N-Grams).
*   **Phonetics & Fuzzing:** `jellyfish` (Metaphone Algorithm) & `rapidfuzz` (Levenshtein Distance).
*   **LLM API:** OpenRouter (Model: `openai/gpt-5-nano` / `gpt-oss-120b`).
*   **Data Handling:** `pandas`, `clickhouse-connect`, `python-dotenv`.

---

## 3. Skema & Logika Algoritma Utama
Sistem ini terbagi dalam dua skenario eksekusi: **Discovery Pipeline** (`advanced_brand_discovery.py`) lalu diakhiri oleh **Injection Pipeline** (`execute_brand_mapping.py`).

### Tahap A: The Funneling Pipeline (Penyaringan Kandidat)
1.  **Pengambilan Data:** Menarik ± 840.000 judul item `No Brand` dan ± 500 Master Brand resmi dari ClickHouse.
2.  **Lapis 1 (Scrubbing TF-IDF):**
    *   Membunuh kalimat-kalimat *"garbage"* (promosi, jenis barang) menggunakan *Custom Stopwords* agresif.
    *   Mengeliminasi kata/frasa pasaran menggunakan `Max_DF (0.5%)`. Apapun yang muncul lebih dari 0.5% (misal kata *"Kopi"*) dianggap kata sifat/generik dan akan didiskualifikasi.
    *   Mengekstrak N-Gram (Unigram & Bigram) murni, mengompres 840k item menjadi hanya **± 20.000 kandidat merek bersih**.
3.  **Lapis 2 (Semantic & Phonetic Synonym Matcher):**
    *   **Aturan Exact (100%):** Bila sama ejaannya mutlak ➡ `Existing Brand`.
    *   **Aturan Metaphone (Bunyi Suara):** Karena *skintific* dan *skintifik* memiliki jejak `jellyfish.metaphone` yang kembar identik (`SKNTFK`), keduanya disahkan tanpa ragu ➡ `Auto-Matched (Synonym)`.
    *   **Aturan Skeleton Fuzz (> 95.0%):** Skrip menghapus seluruh spasi dan karakter unik (menjadi tulisan tulang/skeleton). Kemudian algoritma *RapidFuzz* mencari kemiripan ejaan. Berbeda dengan fuzzy biasa, Fuzz-Tulang ini mensyaratkan skor ekstrem 95% agar kebal *false-positives* layaknya *max preso* = *maxpresso* ➡ `Auto-Matched (Synonym)`.
4.  **Lapis 3 (LLM Verification Final):**
    *   Sisa kandidat yang benar-benar tidak terpetakan (berstatus `New Brand Discovery`) akan dibundel menjadi paket *batching JSON* (2.000 kandidat/ping) lalu dilempar ke OpenAI/OpenRouter untuk diperiksa apakah kata langka tersebut adalah merek UMKM yang sah atau sekadar singkatan desa/bentuk *(Contoh: Sakha Coffee)* ➡ disahkan menjadi `LLM Verified Brand`.

### Tahap B: The Injection Executor (Eksekusi ke ClickHouse)
Setelah hasil diekspor ke Excel dan divalidasi manusia tipis-tipis, file tersebut diracik kembali oleh skrip Injektor:
*   Membaca baris yang memiliki stempel sah (Synonym, Existing).
*   Sistem melakukan kompresi ratusan relasi Mapping tersebut menjadi 1 argumen bersarang SQL **`multiIf`** berskala raksasa demi mengakali kinerja mutasi (*ALTER TABLE UPDATE*) di partisi ClickHouse. Skrip mengeksekusi 100 injeksi mapping brand sekaligus per detik.

---

## 4. Problem Saat Ini (The Short-Letter Anomaly)
Meskipun arsitektur Phonetic & Skeleton berhasil menurunkan metrik "No Brand" hingga di bawah 1% dan menghancurkan problem *typo / space anomaly*, pelacakan hasil kami mendeteksi sumbangsih persentase yang tak wajar besar untuk kategori `Auto-Matched (Synonym)`.
Usut punya usut, ada titik buta (Blind-spot) pada arsitektur pendeteksi sinonim ini:
1.  **Tabrakan Kata Sangat Pendek (Collisions):** Skema pencocokan Fonetik (Kode Suara) dan Fuzz sangat fatal diterapkan ke dalam kata yang sangat pendek (contoh: 2 hingga 3 huruf). Kata *Garbage* langka (atau singkatan promosi aneh) yang tidak sengaja berawalan satu atau dua huruf yang sama dengan Master Brand (seperti singkatan B&B atau H N), punya probabilitas sangat tinggi untuk dikira sebagai "salah eja / sinonim" karena panjang datanya tidak cukup untuk dibandingkan dengan ketat.
2.  **Hipotesis Tabel Master Kotor:** Kami meragukan kemurnian tabel Master Brand ClickHouse di hulu. Jika tanpa sengaja salah satu entri di Master Brand berbunyi kata generik (*misal: "Kopi Asli"*), maka skrip ini (yang tugas aslinya meloloskan sinonim dari Sang Master) akan sekuat tenaga memasukkan ratusan kata berbau "Kopi Asti/Kopi Asri" sebagai miliknya.

### Next Action Plans:
*   [ ] Menyisipkan perlindungan keamanan `len() > 3` di barisan Python Matcher. Mewajibkan segala *Merek Pendek (Maksimal 3 Huruf)* untuk HARUS lulus ujian Lapis 1 (Exact Match Mutlak), membatalkan pemeriksaan Phonetic.
*   [ ] Mengaudit tabel Master Brands di ClickHouse demi menyingkirkan entri penipu (Fake master brands).
