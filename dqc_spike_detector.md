# DQC Spike Detector — `dm_Mandom`

Script Python untuk mendeteksi anomali spike pada data penjualan harian di tabel `dm_Mandom` (dan tabel lain yang dikonfigurasi lewat `TABLE_NAME`), mencakup semua Channel dan L3Title untuk periode yang ditentukan.

---

## Cara Kerja (Overview)

Script bekerja dalam **dua lapisan deteksi**, dengan tambahan **Stage 2** untuk flag [F] dan [G]:

```
┌─────────────────────────────────────────────────────────┐
│  fetch_raw_data (window ±7 hari dari PERIOD)            │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │  Stage 1: Flag A–E          │
          │  IQR → candidate dates      │
          │  Semua tanggal → C + D      │
          └──────────────┬──────────────┘
                         │
              IQR spike date ditemukan?
                    │ Ya
          ┌─────────▼─────────────────────────┐
          │  Stage 2: Ghost Detection          │
          │  Query CH: last seen per ItemId    │
          │  Items dengan history → [F]        │
          │  Items brand new     → [G]         │
          └─────────┬─────────────────────────┘
                    │
          ┌─────────▼──────────┐
          │  Merge & Output    │
          │  Excel + SQL       │
          └────────────────────┘
```

1. **Lapisan 1 — Candidate Date Discovery (IQR-based)**
   Script menghitung total `DailySalesValue` per hari untuk setiap kombinasi `Channel × L3Title`. Tanggal yang nilainya melebihi **Q3 + IQR_MULT × IQR** dianggap sebagai *candidate spike date*.

2. **Lapisan 2 — Item-level Flagging**
   Untuk setiap *candidate date*, script memeriksa item-item mana yang benar-benar anomali menggunakan flag A–D. Script juga tetap scan **semua tanggal** di periode untuk flag C & D.

3. **Stage 2 — Ghost Detection (Flag F & G)**
   Hanya berjalan pada candidate IQR dates. Query terpisah ke ClickHouse untuk mengambil riwayat scraping item-item yang tidak ada dalam window ±7 hari — mendeteksi akumulasi penjualan dari gap panjang.

---

## Penjelasan Flag Anomali

### [A] `STALE_FROZEN_DAILY`
**DailySalesCount tidak berubah (frozen) dalam detection window.**

- Terjadi saat scraper mengambil data yang sama berulang kali tanpa ada perubahan aktual.
- Kondisi: Dalam window ±7 hari, item hanya memiliki **≤ 2 nilai unik** untuk `DailySalesCount`, muncul setidaknya **2 hari**, dan nilainya **> MIN_DAILY_COUNT**.
- Aktif hanya dengan flag `--include-stale`.
- **Contoh**: Item selalu menunjukkan `DailySalesCount = 500` selama 10 hari berturut-turut.

---

### [B] `NEW_ITEM_UNIFORM_COUNT`
**Item baru dengan DailySalesCount yang seragam.**

- Item yang baru pertama kali muncul di dalam detection window namun langsung menunjukkan nilai `DailySalesCount` yang sama terus-menerus.
- Kondisi: `first_seen_ever >= detection_window_from` AND nilai unik `DailySalesCount` **≤ 2** AND nilai minimum **> MIN_DAILY_COUNT**.
- Aktif hanya dengan flag `--include-stale`.
- **Contoh**: Item baru muncul sejak 20 Januari dan setiap hari selalu tercatat `DailySalesCount = 300`.

---

### [C] `CUMULATIVE_SALESCOUNT`
**DailySalesCount hampir sama dengan total SalesCount kumulatif.**

- Terjadi ketika scraper membaca `DailySalesCount` secara kumulatif (total sejak listing dibuat), bukan harian.
- Kondisi: `DailySalesCount / SalesCount > 0.7` pada hari target, dengan `DailySalesCount > MIN_DAILY_COUNT`.
- **Contoh**: Item punya `SalesCount = 1000` total, dan pada hari itu `DailySalesCount = 850`.

---

### [D] `SPIKE_JUMP`
**DailySalesCount pada hari spike jauh lebih tinggi dari baseline.**

- Lonjakan ekstrem dibandingkan rata-rata penjualan harian pada hari-hari lain di sekitarnya.
- Kondisi: `DailySalesCount (hari spike) > SPIKE_JUMP_MULT × rata-rata baseline`, dengan minimal 2 hari baseline.
- **Contoh**: Item biasanya terjual 50 unit/hari, tiba-tiba satu hari tercatat 600 unit.

> Flag ini bisa juga menangkap **hari kampanye legit** (Harbolnas, Flash Sale besar). Selalu review hasilnya sebelum delete.

---

### [F] `GHOST_REAPPEARANCE` *(baru)*
**Item yang absen lama tiba-tiba muncul kembali; DailySalesCount merupakan akumulasi penjualan selama gap tersebut.**

- Akar masalah: formula `DailySalesCount = SalesCount_hari_ini − SalesCount_scrape_terakhir`. Jika item tidak di-scrape selama berbulan-bulan, selisih ini mencerminkan akumulasi penjualan multi-bulan — bukan penjualan harian.
- Kondisi:
  1. Item tidak ada dalam window ±7 hari sebelum spike date
  2. Ditemukan riwayat scraping sebelumnya di luar window (query stage 2 ke CH)
  3. Gap ≥ `GHOST_MIN_GAP_DAYS` hari
  4. `|DailySalesCount − (SalesCount_hari_ini − SalesCount_scrape_terakhir)| ≤ GHOST_DELTA_TOLERANCE`
- Output tambahan: kolom **`Gap Hari (F)`** di sheet Detail Excel — menunjukkan berapa hari item absen.
- **Contoh nyata (April 2026, Lazada)**: Item Denpoo Twin Tub terakhir di-scrape 30 Nov 2025 (SalesCount=981). Muncul kembali 21 Apr 2026 (SalesCount=1025). `DailySalesCount = 1025 − 981 = 44` — penjualan 142 hari dihitung sebagai 1 hari, menggelembungkan DailySalesValue sebesar ~Rp 69 juta hanya dari satu item.

---

### [G] `BRAND_NEW_SPIKE` *(baru)*
**Item benar-benar baru (belum pernah ada di database) yang muncul pertama kali pada spike date dengan DailySalesCount tinggi.**

- Berbeda dengan [F]: item ini tidak memiliki riwayat scraping sama sekali di ClickHouse.
- Kondisi:
  1. Tidak ditemukan di ClickHouse sebelum spike date (query stage 2)
  2. `DailySalesCount > GHOST_NEW_MIN_COUNT`
  3. Item hanya muncul pada spike date, tidak ada di hari-hari sesudahnya dalam window (one-day appearance)
- **Contoh**: Item baru listing di Lazada, muncul pertama kali saat campaign, lalu tidak muncul lagi keesokan harinya.

---

## Output Script

Script menghasilkan **2 file** di folder `csv/` (atau `--output-dir` yang ditentukan):

| File | Isi |
|------|-----|
| `dqc_mandom_spikes.xlsx` | File Excel 2 sheet: **Summary** (agregat) dan **Detail** (per item) |
| `dqc_mandom_delete.sql` | SQL `ALTER TABLE ... DELETE` siap pakai — **review dulu sebelum dieksekusi!** |

### Sheet Excel:
- **Summary**: Agregat per `Channel × L3Title × Spike Date × Flag` — berisi jumlah item dan total GMV terdampak.
- **Detail**: Satu baris per item yang ter-flag — berisi `ItemId`, `ShopName`, `ListingName`, `DailySalesCount`, `GMV (jt)`, **`Gap Hari (F)`** (hanya untuk flag F), dll.

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
> Proses ini akan memakan waktu cukup lama karena memproses semua kombinasi Channel × L3Title. Flag [F] dan [G] aktif secara default dan akan menambah round-trip query ke ClickHouse untuk setiap IQR-candidate spike date.

---

### 2. Filter by Channel saja
```bash
# Hanya Lazada
python3 dqc_spike_detector_mandom.py --channel "Lazada"

# Hanya Tiktok x Tokopedia
python3 dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia"

# Hanya Shopee
python3 dqc_spike_detector_mandom.py --channel "Shopee"
```

---

### 3. Filter by Channel + L3Title
```bash
# Lazada, kategori Mesin Cuci saja
python3 dqc_spike_detector_mandom.py --channel "Lazada" --l3title "Mesin Cuci"

# Tiktok x Tokopedia, kategori Cleanser
python3 dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia" --l3title "Cleanser"

# Shopee, kategori Body Lotion
python3 dqc_spike_detector_mandom.py --channel "Shopee" --l3title "Body Lotion"
```

---

### 4. Preview saja (tanpa simpan file)
```bash
python3 dqc_spike_detector_mandom.py --dry-run

python3 dqc_spike_detector_mandom.py --channel "Lazada" --dry-run
```

---

### 5. Threshold flag D & IQR (opsional)
```bash
# Lebih ketat: spike harus 10× baseline (default: 3×)
python3 dqc_spike_detector_mandom.py --spike-mult 10

# Lebih ketat: IQR multiplier 4× untuk candidate date (default: 3.0×)
python3 dqc_spike_detector_mandom.py --iqr-mult 4

# Min DailySalesCount yang dianggap signifikan (default: 10)
python3 dqc_spike_detector_mandom.py --min-count 50

# Gabungan
python3 dqc_spike_detector_mandom.py --channel "Shopee" --spike-mult 8 --iqr-mult 4
```

---

### 6. Kustomisasi threshold flag [F] Ghost Reappearance
```bash
# Gap minimum 60 hari (default: 30)
python3 dqc_spike_detector_mandom.py --ghost-gap 60

# Toleransi delta lebih ketat (default: 2)
python3 dqc_spike_detector_mandom.py --ghost-tolerance 1

# Min DailySalesCount untuk brand new item (default: 5)
python3 dqc_spike_detector_mandom.py --ghost-min-count 10

# Gabungan: deteksi ghost yang absen minimal 45 hari, delta harus persis
python3 dqc_spike_detector_mandom.py --channel "Lazada" --ghost-gap 45 --ghost-tolerance 0
```

---

### 7. Matikan flag [F] dan [G]
Jika hanya ingin menjalankan flag A–D (perilaku lama):
```bash
python3 dqc_spike_detector_mandom.py --no-ghost
```

---

### 8. Aktifkan flag [A] dan [B] (Stale / Frozen)
Flag ini nonaktif secara default karena cenderung noisy:
```bash
python3 dqc_spike_detector_mandom.py --include-stale

# Kombinasi semua flag aktif
python3 dqc_spike_detector_mandom.py --include-stale --channel "Lazada"
```

---

### 9. Interactive delete (langsung eksekusi ke ClickHouse)
```bash
# Konfirmasi y/n/q per grup sebelum delete
python3 dqc_spike_detector_mandom.py --execute

# Auto-confirm semua (hati-hati!)
python3 dqc_spike_detector_mandom.py --execute --yes-all

# Filter dulu sebelum execute
python3 dqc_spike_detector_mandom.py --channel "Lazada" --execute
```

---

### 10. Output ke folder custom & tanpa SQL
```bash
python3 dqc_spike_detector_mandom.py --output-dir ./output_april2026

python3 dqc_spike_detector_mandom.py --no-sql
```

---

## Daftar Argumen Lengkap

### Koneksi
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--host` | dari `.env` | ClickHouse host (override .env) |
| `--port` | dari `.env` | ClickHouse port (override .env) |
| `--user` | dari `.env` | ClickHouse user (override .env) |
| `--password` | dari `.env` | ClickHouse password (override .env) |

### Filter Data
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--channel` | *(semua)* | Filter ke satu Channel tertentu |
| `--l3title` | *(semua)* | Filter ke satu L3Title tertentu |
| `--brand` | *(semua)* | Filter ke satu Brand tertentu |

### Threshold Deteksi
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--spike-mult` | `3` | `[D]` Multiplier SPIKE_JUMP: daily > N× avg baseline |
| `--iqr-mult` | `3.0` | `[E]` Multiplier IQR untuk candidate date discovery |
| `--min-count` | `10` | Min `DailySalesCount` agar item dianggap signifikan |

### Ghost Detection (Flag F & G)
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--no-ghost` | `False` | Nonaktifkan flag [F] dan [G] sepenuhnya |
| `--ghost-gap` | `30` | `[F]` Gap minimum hari sejak scrape terakhir agar di-flag |
| `--ghost-tolerance` | `2` | `[F]` Toleransi `\|DailySalesCount − delta_SalesCount\|` yang masih dianggap match |
| `--ghost-min-count` | `5` | `[G]` Min `DailySalesCount` agar brand new item dianggap mencurigakan |

### Flag Tambahan
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--include-stale` | `False` | Aktifkan flag [A] `STALE_FROZEN_DAILY` dan [B] `NEW_ITEM_UNIFORM_COUNT` |

### Output & Eksekusi
| Argumen | Default | Keterangan |
|---------|---------|------------|
| `--output-dir` | `./csv` | Direktori penyimpanan output Excel & SQL |
| `--pareto-pct` | `80` | Cumulative GMV % yang dicakup untuk Pareto filter |
| `--min-gmv` | `0.5` | Minimum GMV (juta Rp) per item agar masuk output |
| `--dry-run` | `False` | Hanya print summary, tidak simpan file |
| `--no-sql` | `False` | Skip generate file SQL DELETE |
| `--execute` | `False` | Interactive DELETE: konfirmasi y/n/q per grup sebelum eksekusi |
| `--yes-all` | `False` | Auto-confirm semua DELETE (pakai bersama `--execute`) |

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
   Perhatikan kolom "Gap Hari (F)" untuk item dengan flag GHOST_REAPPEARANCE
        ↓
5. Kalau setuju → gunakan --execute untuk delete interaktif,
   ATAU copy file .sql → jalankan manual di ClickHouse client
        ↓
6. Verifikasi data sudah bersih
```

> **Script TIDAK otomatis menghapus data** kecuali dijalankan dengan `--execute`. SQL DELETE yang di-generate harus dieksekusi secara eksplisit.

---

## Catatan: Perbedaan Flag [C] vs [F]

Kedua flag sama-sama mendeteksi "penjualan kumulatif dihitung sebagai harian", tapi dari sudut yang berbeda:

| | [C] `CUMULATIVE_SALESCOUNT` | [F] `GHOST_REAPPEARANCE` |
|---|---|---|
| **Trigger** | Ratio `DailySalesCount / SalesCount > 0.7` | Gap scraping > N hari + delta match |
| **Item** | Muncul rutin, tapi satu hari ratio meledak | Absen lama, tiba-tiba muncul |
| **Penyebab umum** | Bug scraper membaca total sebagai harian | Scraper campaign page menangkap SKU lama |
| **Contoh April 21** | Tidak tertangkap (ratio 4–9%) | Tertangkap (gap 136–181 hari, delta exact match) |

Flag [F] dirancang khusus untuk menutup celah yang tidak bisa ditangkap flag [C].
