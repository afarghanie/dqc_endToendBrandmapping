# DQC Spike Detector — `dm_Mandom`

Script Python untuk mendeteksi anomali spike pada data penjualan harian di tabel `dm_Mandom`, mencakup semua Channel dan L3Title untuk periode **1 Januari 2026 s/d 31 Maret 2026**.

---

## Cara Kerja (Overview)

Script bekerja dalam **dua lapisan deteksi**:

1. **Lapisan 1 — Candidate Date Discovery (IQR-based)**
   Script menghitung total `DailySalesValue` per hari untuk setiap kombinasi `Channel × L3Title`. Tanggal yang nilainya melebihi **Q3 + 3× IQR** dianggap sebagai *candidate spike date* — yaitu hari yang secara agregat terlihat anomali.

2. **Lapisan 2 — Item-level Flagging**
   Untuk setiap *candidate date*, script memeriksa item-item mana yang benar-benar anomali menggunakan 4 flag di bawah. Script juga tetap scan **semua tanggal** di periode untuk flag C & D, agar tidak ada item yang lolos meski hari tersebut tidak terdeteksi di lapisan 1.

---

## Penjelasan Flag Anomali

### 🔴 [A] `STALE_FROZEN_DAILY`
**DailySalesCount tidak berubah (frozen) dalam detection window.**

- Terjadi saat scraper mengambil data yang sama berulang kali tanpa ada perubahan aktual.
- Kondisi: Dalam window deteksi (spike date ± 7 hari), sebuah item hanya memiliki **≤ 2 nilai unik** untuk `DailySalesCount`, muncul setidaknya **2 hari**, dan nilainya **> 10**.
- **Contoh**: Item selalu menunjukkan `DailySalesCount = 500` selama 10 hari berturut-turut — ini tidak wajar karena penjualan harian seharusnya berfluktuasi.

---

### 🟠 [B] `NEW_ITEM_UNIFORM_COUNT`
**Item baru dengan DailySalesCount yang seragam.**

- Item yang baru pertama kali muncul di dalam detection window, namun langsung menunjukkan nilai `DailySalesCount` yang sama terus-menerus.
- Kondisi: `first_seen_ever >= detection_window_from` AND jumlah nilai unik `DailySalesCount` **≤ 2** AND nilai minimum **> 10**.
- **Contoh**: Item baru muncul sejak 20 Januari dan setiap hari selalu tercatat `DailySalesCount = 300` — ini indikasi data scraping yang tidak reliabel.

---

### 🟡 [C] `CUMULATIVE_SALESCOUNT`
**DailySalesCount hampir sama dengan total SalesCount kumulatif.**

- Terjadi ketika scraper membaca `DailySalesCount` secara kumulatif (total sejak listing dibuat), bukan harian.
- Kondisi: `DailySalesCount / SalesCount > 0.7` pada hari target, dengan `DailySalesCount > 10`.
- **Contoh**: Sebuah item punya total `SalesCount = 1000` (sejak listing dibuat), dan pada hari itu `DailySalesCount = 850` — artinya hampir semua penjualan sepanjang sejarah listing dianggap terjadi dalam satu hari.

---

### 🔵 [D] `SPIKE_JUMP`
**DailySalesCount pada hari spike jauh lebih tinggi dari baseline.**

- Lonjakan ekstrem dibandingkan rata-rata penjualan harian pada hari-hari lain di sekitarnya.
- Kondisi: `DailySalesCount (hari spike) > 5× rata-rata baseline`, di mana baseline adalah hari-hari dalam window deteksi **kecuali** hari spike itu sendiri, dengan minimal 2 hari baseline.
- **Contoh**: Item biasanya terjual 50 unit/hari, tapi tiba-tiba pada satu hari tercatat 600 unit — ini 12× lebih tinggi dari baseline, sehingga ter-flag.

> ⚠️ Flag ini bisa juga menangkap **hari kampanye legit** (Harbolnas, Flash Sale besar). Selalu review hasilnya sebelum delete.

---

## Output Script

Script menghasilkan **2 file** di folder `csv/`:

| File | Isi |
|------|-----|
| `dqc_mandom_spikes_YYYYMMDD_HHMMSS.xlsx` | File Excel 2 sheet: **Summary** (agregat) dan **Detail** (per item) |
| `dqc_mandom_delete_YYYYMMDD_HHMMSS.sql` | SQL `ALTER TABLE ... DELETE` siap pakai — **review dulu sebelum dieksekusi!** |

### Sheet Excel:
- **Summary**: Agregat per `Channel × L3Title × Spike Date × Flag` — berisi jumlah item dan total GMV terdampak.
- **Detail**: Satu baris per item yang ter-flag — berisi `ItemId`, `ShopName`, `ListingName`, `DailySalesCount`, `GMV (jt)`, dll.

---

## Cara Run Script

### Prasyarat
Pastikan file `.env` ada di direktori yang sama dengan koneksi ClickHouse:
```
CLICKHOUSE_HOST=xxx.xxx.xxx.xxx
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=your_user
CLICKHOUSE_PASSWORD=your_password
CLICKHOUSE_DB=default
```

---

### 1. Scan semua Channel & L3Title (full scan)
```bash
python3 dqc_spike_detector_mandom.py
```
> ⚠️ Proses ini akan memakan waktu cukup lama karena memproses semua kombinasi Channel × L3Title.

---

### 2. Filter by Channel saja
```bash
# Hanya Tiktok x Tokopedia
python3 dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia"

# Hanya Shopee
python3 dqc_spike_detector_mandom.py --channel "Shopee"

# Hanya Tokopedia
python3 dqc_spike_detector_mandom.py --channel "Tokopedia"
```

---

### 3. Filter by Channel + L3Title
```bash
# Tiktok x Tokopedia, kategori Cleanser saja
python3 dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia" --l3title "Cleanser"

# Shopee, kategori Body Lotion saja
python3 dqc_spike_detector_mandom.py --channel "Shopee" --l3title "Body Lotion"

# Tiktok x Tokopedia, kategori Parfum Unisex
python3 dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia" --l3title "Parfum Unisex"
```

---

### 4. Preview saja (tanpa simpan file)
Gunakan `--dry-run` untuk melihat summary di terminal tanpa menyimpan Excel maupun SQL:
```bash
python3 dqc_spike_detector_mandom.py --dry-run

python3 dqc_spike_detector_mandom.py --channel "Shopee" --l3title "Shampoo" --dry-run
```

---

### 5. Custom threshold (opsional)
```bash
# Lebih ketat: spike harus 10× baseline (default: 5×)
python3 dqc_spike_detector_mandom.py --spike-mult 10

# Lebih ketat: IQR multiplier 4× untuk candidate date (default: 3×)
python3 dqc_spike_detector_mandom.py --iqr-mult 4

# Gabungan filter + threshold custom
python3 dqc_spike_detector_mandom.py --channel "Shopee" --spike-mult 8 --iqr-mult 4

# Min DailySalesCount yang dianggap signifikan (default: 10)
python3 dqc_spike_detector_mandom.py --min-count 50
```

---

### 6. Simpan ke folder custom
```bash
python3 dqc_spike_detector_mandom.py --output-dir ./output_april2026
```

---

### 7. Generate Excel saja (tanpa SQL DELETE)
```bash
python3 dqc_spike_detector_mandom.py --no-sql
```

---

## Daftar Argumen Lengkap

| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--channel` | *(semua)* | Filter ke satu Channel tertentu |
| `--l3title` | *(semua)* | Filter ke satu L3Title tertentu |
| `--spike-mult` | `5` | Multiplier SPIKE_JUMP (flag D): daily > N× avg baseline |
| `--iqr-mult` | `3.0` | Multiplier IQR untuk candidate date discovery |
| `--min-count` | `10` | Min `DailySalesCount` agar item dianggap signifikan |
| `--output-dir` | `./csv` | Direktori penyimpanan output Excel & SQL |
| `--dry-run` | `False` | Hanya print summary, tidak simpan file |
| `--no-sql` | `False` | Skip generate file SQL DELETE |
| `--host` | dari `.env` | ClickHouse host (override .env) |
| `--port` | dari `.env` | ClickHouse port (override .env) |
| `--user` | dari `.env` | ClickHouse user (override .env) |
| `--password` | dari `.env` | ClickHouse password (override .env) |

---

## Alur Kerja yang Disarankan

```
1. Preview dulu (--dry-run)
        ↓
2. Review summary di terminal
        ↓
3. Kalau masuk akal → jalankan tanpa --dry-run
        ↓
4. Buka Excel di folder csv/ → review item per item di sheet Detail
        ↓
5. Kalau setuju → buka file .sql → copy & jalankan di ClickHouse client
        ↓
6. Verifikasi data sudah bersih
```

> ⚠️ **Script TIDAK otomatis menghapus data.** SQL DELETE hanya di-*generate*, bukan dieksekusi langsung. Kamu harus menjalankan file `.sql` secara manual di ClickHouse.
