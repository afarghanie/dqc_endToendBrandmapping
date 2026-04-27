"""
=======================================================================
DQC Spike Detector — dm_Mandom
=======================================================================
Deteksi anomali spike di setiap Channel × L3Title untuk dm_Mandom
Periode: 1 Januari 2026 s/d 31 Maret 2026

Flag anomali (sama seperti SQL reusable):
  [A] STALE_FROZEN_DAILY    : DailySalesCount frozen (≤2 nilai unik) dalam window
  [B] NEW_ITEM_UNIFORM_COUNT: Item baru + DailySalesCount seragam
  [C] CUMULATIVE_SALESCOUNT : DailySalesCount / SalesCount > 0.7
  [D] SPIKE_JUMP            : DailySalesCount pada hari spike > N× rata-rata baseline

Output:
  - Summary tabel per Channel × L3Title (spike dates + GMV terdampak)
  - CSV detail semua item yang ter-flag
  - SQL DELETE statements siap pakai per spike date yang ditemukan

Usage:
  python dqc_spike_detector_mandom.py
  Koneksi dibaca otomatis dari .env di direktori yang sama.
  python dqc_spike_detector_mandom.py --channel "Tiktok x Tokopedia" --l3title "Cleanser"
  python dqc_spike_detector_mandom.py --dry-run --output-dir ./output
=======================================================================
"""

import os
import sys
import argparse
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

# Load .env dari direktori script (otomatis)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
except ImportError:
    pass  # python-dotenv tidak wajib, env var manual juga bisa

import clickhouse_connect
import pandas as pd
import numpy as np

# ────────────────────────────────────────────────────────────────────
# CONFIG DEFAULT  (dibaca dari .env / environment variable)
# ────────────────────────────────────────────────────────────────────
DEFAULT_HOST       = os.getenv("CLICKHOUSE_HOST", "localhost")
DEFAULT_PORT       = int(os.getenv("CLICKHOUSE_PORT", "8123"))
DEFAULT_USER       = os.getenv("CLICKHOUSE_USER", "default")
DEFAULT_PASSWORD   = os.getenv("CLICKHOUSE_PASSWORD", "")
DEFAULT_DATABASE   = os.getenv("CLICKHOUSE_DB", "default")

TABLE_NAME         = "dm_Electrolux"
PERIOD_FROM        = date(2026, 4, 15) 
PERIOD_TO          = date(2026, 4, 21)
# Detection window radius di sekitar candidate spike date (hari)
DETECTION_RADIUS   = 7   # baseline window = spike_date ± 7 hari (dikecualikan spike_date itu sendiri)

# Threshold parameter
MIN_DAILY_COUNT    = 10   # DailySalesCount minimal agar dianggap signifikan
MIN_BASELINE_DAYS  = 2    # Minimal baseline hari untuk SPIKE_JUMP
SPIKE_JUMP_MULT    = 3    # [D] daily > N× avg_baseline = spike
CUMULATIVE_RATIO   = 0.7  # [C] daily/total > ratio ini = spike
FROZEN_MAX_UNIQUE  = 2    # [A/B] max nilai unik DailySalesCount agar dianggap frozen
FROZEN_MIN_DAYS    = 2    # [A/B] min hari agar frozen check valid
IQR_MULT           = 3.0  # [E] IQR-based outlier: daily > Q3 + IQR_MULT × IQR

# ────────────────────────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# STEP 1 — Fetch data dari ClickHouse
# ════════════════════════════════════════════════════════════════════

def get_client(host, port, user, password, database):
    return clickhouse_connect.get_client(
        host=host, port=port,
        username=user, password=password,
        database=database,
    )


def fetch_raw_data(client, channel_filter=None, l3title_filter=None, brand_filter=None) -> pd.DataFrame:
    """
    Ambil semua baris dm_Mandom dalam periode yang relevan.
    Data yang diambil: per (ItemId, ScrapDate, Channel, L3Title, Brand).
    """
    log.info("Fetching raw data from ClickHouse ...")

    # Extend window untuk baseline: ambil DETECTION_RADIUS hari sebelum & sesudah periode
    fetch_from = PERIOD_FROM - timedelta(days=DETECTION_RADIUS)
    fetch_to   = PERIOD_TO   + timedelta(days=DETECTION_RADIUS)

    channel_clause  = f"AND Channel = '{channel_filter}'"  if channel_filter  else ""
    l3title_clause  = f"AND L3Title = '{l3title_filter}'"  if l3title_filter  else ""
    brand_clause    = f"AND Brand = '{brand_filter}'"      if brand_filter    else ""

    query = f"""
        SELECT
            ItemId,
            ScrapDate,
            Channel,
            L3Title,
            Brand,
            ShopName,
            ListingName,
            SalePrice,
            SalesCount,
            DailySalesCount,
            DailySalesValue,
            MIN(ScrapDate) OVER (PARTITION BY ItemId, Channel) AS first_seen_ever
        FROM default.{TABLE_NAME}
        WHERE ScrapDate BETWEEN '{fetch_from}' AND '{fetch_to}'
          AND DailySalesValue > 0
          {channel_clause}
          {l3title_clause}
          {brand_clause}
    """

    result = client.query(query)
    cols = [
        "ItemId", "ScrapDate", "Channel", "L3Title", "Brand",
        "ShopName", "ListingName", "SalePrice", "SalesCount",
        "DailySalesCount", "DailySalesValue", "first_seen_ever",
    ]
    df = pd.DataFrame(result.result_rows, columns=cols)

    # Type casting
    df["ScrapDate"]       = pd.to_datetime(df["ScrapDate"]).dt.date
    df["first_seen_ever"] = pd.to_datetime(df["first_seen_ever"]).dt.date
    df["DailySalesCount"] = pd.to_numeric(df["DailySalesCount"], errors="coerce").fillna(0)
    df["DailySalesValue"] = pd.to_numeric(df["DailySalesValue"], errors="coerce").fillna(0)
    df["SalePrice"]       = pd.to_numeric(df["SalePrice"],       errors="coerce").fillna(0)
    df["SalesCount"]      = pd.to_numeric(df["SalesCount"],      errors="coerce")  # bisa NaN

    log.info(f"  → {len(df):,} rows fetched, {df['ItemId'].nunique():,} unique items")
    return df


# ════════════════════════════════════════════════════════════════════
# STEP 2 — Deteksi Spike per Channel × L3Title
# ════════════════════════════════════════════════════════════════════

class SpikeDetector:
    """
    Menjalankan 5 flag anomali per (Channel, L3Title):
      [A] STALE_FROZEN_DAILY
      [B] NEW_ITEM_UNIFORM_COUNT
      [C] CUMULATIVE_SALESCOUNT
      [D] SPIKE_JUMP
      [E] IQR_OUTLIER  (tambahan statistik)
    """

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.flagged_rows: list[dict] = []

    # ── helper ──
    @staticmethod
    def _detection_window(target_date: date, radius: int = DETECTION_RADIUS):
        return target_date - timedelta(days=radius), target_date + timedelta(days=radius)

    # ────────────────────────────────────────────────────────────────
    # [A] STALE_FROZEN_DAILY
    # Item yang DailySalesCount-nya ≤ FROZEN_MAX_UNIQUE nilai unik
    # dalam window deteksi DAN masih muncul di hari target.
    # ────────────────────────────────────────────────────────────────
    def _flag_stale_frozen(self, grp: pd.DataFrame, win_from: date, win_to: date) -> set:
        win = grp[(grp["ScrapDate"] >= win_from) & (grp["ScrapDate"] <= win_to)]
        stats = win.groupby("ItemId").agg(
            days_seen=("ScrapDate", "nunique"),
            unique_vals=("DailySalesCount", "nunique"),
            max_daily=("DailySalesCount", "max"),
        ).reset_index()
        frozen = stats[
            (stats["days_seen"] >= FROZEN_MIN_DAYS) &
            (stats["unique_vals"] <= FROZEN_MAX_UNIQUE) &
            (stats["max_daily"] > MIN_DAILY_COUNT)
        ]
        return set(frozen["ItemId"])

    # ────────────────────────────────────────────────────────────────
    # [B] NEW_ITEM_UNIFORM_COUNT
    # Item yang first_seen >= detection_window_from, count seragam
    # ────────────────────────────────────────────────────────────────
    def _flag_new_item_uniform(self, grp: pd.DataFrame, win_from: date, win_to: date) -> set:
        win = grp[(grp["ScrapDate"] >= win_from) & (grp["ScrapDate"] <= win_to)]
        stats = win.groupby("ItemId").agg(
            unique_vals=("DailySalesCount", "nunique"),
            min_daily=("DailySalesCount", "min"),
            first_seen=("first_seen_ever", "first"),
        ).reset_index()
        new_uniform = stats[
            (stats["first_seen"] >= win_from) &
            (stats["unique_vals"] <= FROZEN_MAX_UNIQUE) &
            (stats["min_daily"] > MIN_DAILY_COUNT)
        ]
        return set(new_uniform["ItemId"])

    # ────────────────────────────────────────────────────────────────
    # [C] CUMULATIVE_SALESCOUNT
    # DailySalesCount / SalesCount > CUMULATIVE_RATIO pada hari target
    # ────────────────────────────────────────────────────────────────
    def _flag_cumulative(self, grp: pd.DataFrame, target_date: date) -> set:
        day = grp[grp["ScrapDate"] == target_date].copy()
        day = day[
            (day["DailySalesCount"] > MIN_DAILY_COUNT) &
            (day["SalesCount"].notna()) &
            (day["SalesCount"] > 0)
        ]
        day["ratio"] = day["DailySalesCount"] / day["SalesCount"]
        flagged = day[day["ratio"] > CUMULATIVE_RATIO]
        return set(flagged["ItemId"])

    # ────────────────────────────────────────────────────────────────
    # [D] SPIKE_JUMP
    # DailySalesCount pada hari target > SPIKE_JUMP_MULT × avg baseline
    # Baseline = window MINUS target date
    # ────────────────────────────────────────────────────────────────
    def _flag_spike_jump(self, grp: pd.DataFrame, target_date: date,
                         win_from: date, win_to: date) -> set:
        baseline = grp[
            (grp["ScrapDate"] >= win_from) &
            (grp["ScrapDate"] <= win_to) &
            (grp["ScrapDate"] != target_date) &
            (grp["DailySalesCount"] > 0)
        ]
        baseline_stats = baseline.groupby("ItemId").agg(
            avg_baseline=("DailySalesCount", "mean"),
            baseline_days=("ScrapDate", "nunique"),
        ).reset_index()
        baseline_stats = baseline_stats[baseline_stats["baseline_days"] >= MIN_BASELINE_DAYS]

        target_day = grp[
            (grp["ScrapDate"] == target_date) &
            (grp["DailySalesCount"] > MIN_DAILY_COUNT)
        ][["ItemId", "DailySalesCount"]].rename(columns={"DailySalesCount": "target_daily"})

        merged = target_day.merge(baseline_stats, on="ItemId")
        merged = merged[merged["avg_baseline"] > 0]
        flagged = merged[merged["target_daily"] > merged["avg_baseline"] * SPIKE_JUMP_MULT]
        return set(flagged["ItemId"])

    # ────────────────────────────────────────────────────────────────
    # [E] IQR_OUTLIER  (tambahan: aggregate harian per channel-l3title)
    # Deteksi tanggal mana yang total GMV-nya outlier secara statistik
    # di level Channel × L3Title (bukan item level)
    # ────────────────────────────────────────────────────────────────
    def _find_spike_dates_iqr(self, grp: pd.DataFrame) -> list[date]:
        """
        Temukan tanggal-tanggal yang total DailySalesValue (agregat) outlier.
        Returns list of candidate spike dates dalam periode target.
        """
        daily_agg = grp.groupby("ScrapDate")["DailySalesValue"].sum().reset_index()
        daily_agg = daily_agg.sort_values("ScrapDate")

        # Filter hanya periode target untuk evaluasi
        daily_agg = daily_agg[
            (daily_agg["ScrapDate"] >= PERIOD_FROM) &
            (daily_agg["ScrapDate"] <= PERIOD_TO)
        ]

        if len(daily_agg) < 7:
            return []

        vals = daily_agg["DailySalesValue"].values
        q1, q3 = np.percentile(vals, 25), np.percentile(vals, 75)
        iqr    = q3 - q1
        upper  = q3 + IQR_MULT * iqr

        spike_dates = daily_agg[daily_agg["DailySalesValue"] > upper]["ScrapDate"].tolist()
        return spike_dates

    # ────────────────────────────────────────────────────────────────
    # MAIN: run detection untuk satu (channel, l3title) group
    # ────────────────────────────────────────────────────────────────
    def detect_group(self, channel: str, l3title: str) -> list[dict]:
        grp = self.df[
            (self.df["Channel"] == channel) &
            (self.df["L3Title"] == l3title)
        ].copy()

        if grp.empty:
            return []

        include_stale = getattr(self, "include_stale", False)
        results = []

        # ── Helper: tambahkan metadata kolom ke spike_items ──
        def enrich(df, spike_date, win_from, win_to, flag_map):
            df = df.copy()
            df["spike_flag"] = df["ItemId"].map(flag_map)
            df["spike_date"] = spike_date
            df["win_from"]   = win_from
            df["win_to"]     = win_to
            df["gmv_jt"]     = (df["DailySalesValue"] / 1e6).round(3)
            df["daily_to_total_ratio"] = np.where(
                df["SalesCount"].notna() & (df["SalesCount"] > 0),
                (df["DailySalesCount"] / df["SalesCount"]).round(4),
                np.nan
            )
            return df

        # ── Semua tanggal dalam periode ──
        all_dates = sorted(grp[
            (grp["ScrapDate"] >= PERIOD_FROM) &
            (grp["ScrapDate"] <= PERIOD_TO)
        ]["ScrapDate"].unique())

        # ── Candidate spike dates via IQR (untuk flag A & B) ──
        candidate_dates = set(self._find_spike_dates_iqr(grp)) if include_stale else set()

        processed_dates = set()

        # --- Lapisan 1: candidate spike dates (A+B+C+D) ---
        for spike_date in candidate_dates:
            win_from, win_to = self._detection_window(spike_date)

            flag_C = self._flag_cumulative(grp, spike_date)
            flag_D = self._flag_spike_jump(grp, spike_date, win_from, win_to)

            # Flag A & B hanya jika include_stale aktif
            flag_A = self._flag_stale_frozen(grp, win_from, win_to)     if include_stale else set()
            flag_B = self._flag_new_item_uniform(grp, win_from, win_to) if include_stale else set()

            all_flagged = flag_A | flag_B | flag_C | flag_D
            if not all_flagged:
                processed_dates.add(spike_date)
                continue

            spike_items = grp[
                (grp["ScrapDate"] == spike_date) &
                (grp["ItemId"].isin(all_flagged)) &
                (grp["DailySalesValue"] > 0)
            ].copy()

            if not spike_items.empty:
                def assign_flag_abcd(item_id):
                    if item_id in flag_A: return "STALE_FROZEN_DAILY"
                    if item_id in flag_B: return "NEW_ITEM_UNIFORM_COUNT"
                    if item_id in flag_C: return "CUMULATIVE_SALESCOUNT"
                    if item_id in flag_D: return "SPIKE_JUMP"
                    return "UNKNOWN"
                results.extend(enrich(spike_items, spike_date, win_from, win_to,
                                      {i: assign_flag_abcd(i) for i in spike_items["ItemId"]}).to_dict("records"))

            processed_dates.add(spike_date)

        # --- Lapisan 2: semua tanggal, hanya flag C & D ---
        for target_date in all_dates:
            if target_date in processed_dates:
                continue

            win_from, win_to = self._detection_window(target_date)
            flag_C = self._flag_cumulative(grp, target_date)
            flag_D = self._flag_spike_jump(grp, target_date, win_from, win_to)

            all_flagged = flag_C | flag_D
            if not all_flagged:
                continue

            spike_items = grp[
                (grp["ScrapDate"] == target_date) &
                (grp["ItemId"].isin(all_flagged)) &
                (grp["DailySalesValue"] > 0)
            ].copy()

            if spike_items.empty:
                continue

            flag_map = {i: ("CUMULATIVE_SALESCOUNT" if i in flag_C else "SPIKE_JUMP")
                        for i in spike_items["ItemId"]}
            results.extend(enrich(spike_items, target_date, win_from, win_to, flag_map).to_dict("records"))

        return results

    def run_all(self, channels=None, l3titles=None, include_stale=False) -> pd.DataFrame:
        self.include_stale = include_stale
        groups = self.df[["Channel", "L3Title"]].drop_duplicates()
        if channels:
            groups = groups[groups["Channel"].isin(channels)]
        if l3titles:
            groups = groups[groups["L3Title"].isin(l3titles)]

        all_results = []
        total = len(groups)
        for i, (_, row) in enumerate(groups.iterrows(), 1):
            ch, l3 = row["Channel"], row["L3Title"]
            log.info(f"  [{i}/{total}] Checking {ch} × {l3} ...")
            r = self.detect_group(ch, l3)
            if r:
                log.info(f"    → {len(r)} flagged rows found")
            all_results.extend(r)

        if not all_results:
            log.info("No anomalies detected!")
            return pd.DataFrame()

        return pd.DataFrame(all_results)


# ════════════════════════════════════════════════════════════════════
# STEP 3 — Reporting
# ════════════════════════════════════════════════════════════════════

def print_summary(result_df: pd.DataFrame):
    """Print summary table ke terminal."""
    if result_df.empty:
        print("\n✅  No anomalies detected across all Channel × L3Title groups.\n")
        return

    print("\n" + "═" * 90)
    print(f"  DQC ANOMALY SPIKE SUMMARY — {TABLE_NAME}  |  {PERIOD_FROM} s/d {PERIOD_TO}")
    print("═" * 90)

    summary = (
        result_df
        .groupby(["Channel", "L3Title", "spike_date", "spike_flag"])
        .agg(
            item_count=("ItemId", "nunique"),
            gmv_jt=("gmv_jt", "sum"),
        )
        .reset_index()
        .sort_values(["Channel", "L3Title", "spike_date"])
    )

    current_group = None
    for _, row in summary.iterrows():
        group_key = (row["Channel"], row["L3Title"])
        if group_key != current_group:
            print(f"\n  📦  {row['Channel']}  ›  {row['L3Title']}")
            print(f"  {'Date':<14}  {'Flag':<26}  {'Items':>6}  {'GMV (jt)':>12}")
            print(f"  {'─'*14}  {'─'*26}  {'─'*6}  {'─'*12}")
            current_group = group_key
        print(f"  {str(row['spike_date']):<14}  {row['spike_flag']:<26}  "
              f"{int(row['item_count']):>6}  {row['gmv_jt']:>12,.3f}")

    total_gmv = result_df["gmv_jt"].sum()
    total_items = result_df["ItemId"].nunique()
    print(f"\n  TOTAL Flagged Items: {total_items:,}  |  Total GMV Terdampak: {total_gmv:,.3f} jt")
    print("═" * 90 + "\n")


def save_excel(result_df: pd.DataFrame, output_dir: str) -> str:
    """Simpan hasil ke Excel (.xlsx) dengan 2 sheet: Summary & Detail."""
    os.makedirs(output_dir, exist_ok=True)

    # ── Sheet 1: Summary
    summary_df = (
        result_df
        .groupby(["Channel", "L3Title", "spike_date", "spike_flag"])
        .agg(
            item_count=("ItemId", "nunique"),
            gmv_jt=("gmv_jt", "sum"),
        )
        .reset_index()
        .sort_values(["Channel", "L3Title", "spike_date"])
        .rename(columns={
            "spike_date": "Spike Date",
            "spike_flag": "Flag",
            "item_count": "Jumlah Item",
            "gmv_jt":     "GMV Terdampak (jt)",
        })
    )

    # ── Sheet 2: Detail
    detail_cols = [
        "spike_date", "Channel", "L3Title", "Brand", "spike_flag",
        "ItemId", "ShopName", "ListingName",
        "SalePrice", "DailySalesCount", "gmv_jt", "daily_to_total_ratio",
        "win_from", "win_to",
    ]
    detail_df = (
        result_df[[c for c in detail_cols if c in result_df.columns]]
        .copy()
        .sort_values(["Channel", "L3Title", "spike_date", "gmv_jt"],
                     ascending=[True, True, True, False])
        .rename(columns={
            "spike_date":           "Spike Date",
            "spike_flag":           "Flag",
            "gmv_jt":               "GMV (jt)",
            "daily_to_total_ratio": "Daily/Total Ratio",
            "win_from":             "Window From",
            "win_to":               "Window To",
        })
    )

    path = os.path.join(output_dir, "dqc_mandom_spikes.xlsx")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        detail_df.to_excel(writer, sheet_name="Detail",  index=False)

        # ── Auto-fit kolom lebar
        for sheet_name, df in [("Summary", summary_df), ("Detail", detail_df)]:
            ws = writer.sheets[sheet_name]
            for col_idx, col in enumerate(df.columns, 1):
                max_len = max(
                    len(str(col)),
                    df[col].astype(str).str.len().max() if len(df) > 0 else 0,
                )
                ws.column_dimensions[
                    ws.cell(row=1, column=col_idx).column_letter
                ].width = min(max_len + 4, 60)

            # ── Freeze header row
            ws.freeze_panes = "A2"

    log.info(f"Excel saved → {path}")
    return path


def generate_sql_deletes(result_df: pd.DataFrame, output_dir: str) -> str:
    """
    Generate SQL DELETE statements per (Channel, L3Title, spike_date).
    Item diurutkan dari GMV terbesar ke terkecil.
    """
    if result_df.empty:
        return ""

    os.makedirs(output_dir, exist_ok=True)
    lines = [
        "-- ============================================================",
        f"-- DQC AUTO-GENERATED DELETE — dm_Mandom",
        f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"-- Periode  : {PERIOD_FROM} s/d {PERIOD_TO}",
        "-- Item diurutkan: GMV terbesar → terkecil",
        "-- REVIEW DULU sebelum dijalankan!",
        "-- ============================================================\n",
    ]

    # Urutkan per group by GMV descending sebelum generate SQL
    result_sorted = result_df.sort_values(
        ["Channel", "L3Title", "spike_date", "gmv_jt"],
        ascending=[True, True, True, False]
    )

    groups = (
        result_sorted
        .groupby(["Channel", "L3Title", "spike_date", "win_from", "win_to"], sort=False)
        .apply(lambda g: list(g.sort_values("gmv_jt", ascending=False)["ItemId"]))
        .reset_index(name="ItemId")
    )

    for _, row in groups.iterrows():
        ch          = row["Channel"]
        l3          = row["L3Title"]
        spike_date  = row["spike_date"]
        win_from    = row["win_from"]
        win_to      = row["win_to"]
        item_ids    = row["ItemId"]

        # Build item list — chunked per 500 agar tidak terlalu panjang
        chunk_size  = 500
        item_chunks = [item_ids[i:i+chunk_size] for i in range(0, len(item_ids), chunk_size)]

        for chunk_no, chunk in enumerate(item_chunks, 1):
            ids_literal = ", ".join(f"'{x}'" for x in chunk)

            l3_comment  = f"AND L3Title = '{l3}'" if l3 else "-- AND L3Title = ''"
            chunk_label = f" (chunk {chunk_no}/{len(item_chunks)})" if len(item_chunks) > 1 else ""

            lines.append(
                f"-- [{ch}] × [{l3}] | Spike: {spike_date}"
                f" | Window: {win_from}~{win_to}{chunk_label}"
            )
            lines.append(f"-- Items terdampak: {len(chunk)} (sorted by GMV desc)")
            lines.append(
                f"ALTER TABLE default.{TABLE_NAME}\n"
                f"DELETE WHERE\n"
                f"    Channel   = '{ch}'\n"
                f"    AND ScrapDate BETWEEN '{spike_date}' AND '{spike_date}'\n"
                f"    AND DailySalesValue > 0\n"
                f"    {l3_comment}\n"
                f"    AND ItemId IN ({ids_literal});\n"
            )

    sql_text = "\n".join(lines)

    path = os.path.join(output_dir, "dqc_mandom_delete.sql")
    with open(path, "w") as f:
        f.write(sql_text)
    log.info(f"SQL DELETE file saved → {path}")
    return path


def apply_pareto_filter(
    result_df: pd.DataFrame,
    pareto_pct: float = 80.0,
    min_gmv_jt: float = 0.5,
) -> pd.DataFrame:
    """
    Filter item yang benar-benar berkontribusi pada spike:
      1. Hapus item dengan GMV < min_gmv_jt (noise kecil)
      2. Per (spike_date, Channel, L3Title): sort by GMV desc,
         ambil item sampai kumulatif GMV-nya >= pareto_pct% dari
         total flagged GMV grup tersebut.
    """
    if result_df.empty:
        return result_df

    # Step 1: buang item GMV terlalu kecil
    df = result_df[result_df["gmv_jt"] >= min_gmv_jt].copy()
    if df.empty:
        return df

    kept = []
    group_keys = ["spike_date", "Channel", "L3Title"]

    for keys, grp in df.groupby(group_keys):
        grp_sorted   = grp.sort_values("gmv_jt", ascending=False)
        total_gmv    = grp_sorted["gmv_jt"].sum()
        cumulative   = 0.0
        cutoff_ratio = pareto_pct / 100.0

        for _, item_row in grp_sorted.iterrows():
            kept.append(item_row)
            cumulative += item_row["gmv_jt"]
            if cumulative / total_gmv >= cutoff_ratio:
                break

    if not kept:
        return pd.DataFrame()

    return pd.DataFrame(kept).reset_index(drop=True)


def interactive_delete(result_df: pd.DataFrame, client, yes_all: bool = False):
    """
    Tampilkan summary per grup (Channel × L3Title × spike_date),
    tanya konfirmasi y/n/q, lalu eksekusi DELETE langsung ke ClickHouse.
    """
    if result_df.empty:
        print("\n✅  Tidak ada item yang perlu didelete.\n")
        return

    # Grup: (Channel, L3Title, spike_date) — urutkan berdasarkan gmv desc
    groups = (
        result_df
        .groupby(["Channel", "L3Title", "spike_date", "win_from", "win_to"], sort=False)
        .apply(lambda g: g.sort_values("gmv_jt", ascending=False), include_groups=False)
        .reset_index(level=[0,1,2,3,4])
        .groupby(["Channel", "L3Title", "spike_date", "win_from", "win_to"], sort=False)
        .agg(
            item_count=("ItemId", "nunique"),
            gmv_total=("gmv_jt", "sum"),
            item_ids=("ItemId", lambda s: list(s.unique())),
        )
        .reset_index()
        .sort_values(["Channel", "L3Title", "spike_date"])
    )

    total_groups  = len(groups)
    deleted_count = 0
    skipped_count = 0

    print("\n" + "═" * 70)
    print(f"  INTERACTIVE DELETE — {total_groups} grup spike ditemukan")
    print("  Ketik  y = delete  |  n = skip  |  q = keluar")
    print("═" * 70)

    for i, (_, row) in enumerate(groups.iterrows(), 1):
        ch         = row["Channel"]
        l3         = row["L3Title"]
        sdate      = row["spike_date"]
        win_from   = row["win_from"]
        win_to     = row["win_to"]
        n_items    = int(row["item_count"])
        gmv        = row["gmv_total"]
        item_ids   = row["item_ids"]

        print(f"\n  [{i}/{total_groups}] {ch} × {l3}")
        print(f"         Spike date : {sdate}")
        print(f"         Items      : {n_items:,} item")
        print(f"         GMV        : {gmv:,.3f} jt")
        # Tampilkan ItemId untuk verifikasi di database
        if len(item_ids) <= 5:
            ids_display = ", ".join(str(x) for x in item_ids)
        else:
            ids_display = ", ".join(str(x) for x in item_ids[:5]) + f"  … (+{len(item_ids)-5} lainnya)"
        print(f"         ItemId(s)  : {ids_display}")

        if yes_all:
            answer = "y"
            print("         Answer     : y (--yes-all)")
        else:
            try:
                answer = input("  → Delete? (y/n/q): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Dihentikan.")
                break

        if answer == "q":
            print("  Keluar dari interactive delete.")
            break
        elif answer == "y":
            # Build dan eksekusi DELETE
            ids_literal = ", ".join(f"'{x}'" for x in item_ids)
            l3_clause   = f"AND L3Title = '{l3}'" if l3 else ""
            delete_sql  = (
                f"ALTER TABLE default.{TABLE_NAME} DELETE WHERE "
                f"Channel = '{ch}' "
                f"AND ScrapDate BETWEEN '{sdate}' AND '{sdate}' "
                f"AND DailySalesValue > 0 "
                f"{l3_clause} "
                f"AND ItemId IN ({ids_literal})"
            )
            try:
                client.command(delete_sql)
                print(f"  ✔︎  Deleted {n_items:,} items ({gmv:,.3f} jt GMV)")
                deleted_count += 1
            except Exception as e:
                print(f"  ❌  Error: {e}")
        else:
            print("  ⏭︎  Skipped")
            skipped_count += 1

    print("\n" + "═" * 70)
    print(f"  SELESAI — Deleted: {deleted_count} grup | Skipped: {skipped_count} grup")
    print("═" * 70 + "\n")


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="DQC Spike Detector for dm_Mandom (Jan–Mar 2026)"
    )
    parser.add_argument("--host",       default=DEFAULT_HOST)
    parser.add_argument("--port",       type=int, default=DEFAULT_PORT)
    parser.add_argument("--user",       default=DEFAULT_USER)
    parser.add_argument("--password",   default=DEFAULT_PASSWORD)
    parser.add_argument("--channel",    help="Filter ke satu Channel tertentu")
    parser.add_argument("--l3title",    help="Filter ke satu L3Title tertentu")
    parser.add_argument("--brand",      help="Filter ke satu Brand tertentu (opsional)")
    parser.add_argument("--spike-mult", type=float, default=SPIKE_JUMP_MULT,
                        help=f"SPIKE_JUMP multiplier (default: {SPIKE_JUMP_MULT})")
    parser.add_argument("--iqr-mult",   type=float, default=IQR_MULT,
                        help=f"IQR multiplier untuk candidate date detection (default: {IQR_MULT})")
    parser.add_argument("--min-count",  type=float, default=MIN_DAILY_COUNT,
                        help=f"Min DailySalesCount signifikan (default: {MIN_DAILY_COUNT})")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "csv"),
                        help="Direktori output CSV & SQL (default: ./csv)")
    parser.add_argument("--pareto-pct", type=float, default=80.0,
                        help="Cumulative GMV %% yang dicakup (Pareto filter, default: 80)")
    parser.add_argument("--min-gmv",    type=float, default=0.5,
                        help="Minimum GMV (jt) per item agar masuk output (default: 0.5)")
    parser.add_argument("--include-stale", action="store_true",
                        help="Aktifkan flag [A] STALE_FROZEN_DAILY dan [B] NEW_ITEM_UNIFORM_COUNT "
                             "(default: nonaktif, hanya C dan D yang dipakai)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Hanya print summary, tidak simpan file")
    parser.add_argument("--execute",  action="store_true",
                        help="Interactive DELETE: konfirmasi y/n/q per grup sebelum eksekusi")
    parser.add_argument("--yes-all",  action="store_true",
                        help="Auto-confirm semua DELETE tanpa tanya (pakai bersama --execute)")
    parser.add_argument("--no-sql",     action="store_true",
                        help="Skip generate SQL DELETE")
    return parser.parse_args()


def main():
    args = parse_args()

    # Override globals dari CLI
    global SPIKE_JUMP_MULT, IQR_MULT, MIN_DAILY_COUNT
    SPIKE_JUMP_MULT  = args.spike_mult
    IQR_MULT         = args.iqr_mult
    MIN_DAILY_COUNT  = args.min_count

    log.info("=" * 60)
    log.info(f"DQC Spike Detector — {TABLE_NAME}")
    log.info(f"Periode  : {PERIOD_FROM} s/d {PERIOD_TO}")
    log.info(f"Channel  : {args.channel or 'ALL'}")
    log.info(f"L3Title  : {args.l3title or 'ALL'}")
    log.info(f"Brand    : {args.brand or 'ALL'}")
    log.info(f"Params   : spike_mult={SPIKE_JUMP_MULT}×, iqr_mult={IQR_MULT}×, "
             f"min_count={MIN_DAILY_COUNT}")
    log.info("=" * 60)

    # Connect
    client = get_client(args.host, args.port, args.user, args.password, DEFAULT_DATABASE)
    log.info(f"Connected to ClickHouse @ {args.host}:{args.port}")

    # Fetch
    df = fetch_raw_data(
        client,
        channel_filter=args.channel,
        l3title_filter=args.l3title,
        brand_filter=args.brand,
    )

    if df.empty:
        log.warning("No data returned. Check your filters / connection.")
        sys.exit(0)

    # Detect
    log.info("Running anomaly detection ...")
    detector   = SpikeDetector(df)
    result_df  = detector.run_all(
        channels=[args.channel] if args.channel else None,
        l3titles=[args.l3title] if args.l3title else None,
        include_stale=args.include_stale,
    )

    # Pareto filter
    before = len(result_df)
    result_df = apply_pareto_filter(
        result_df,
        pareto_pct=args.pareto_pct,
        min_gmv_jt=args.min_gmv,
    )
    after = len(result_df)
    log.info(f"Pareto filter ({args.pareto_pct}% cumulative GMV, min {args.min_gmv} jt): "
             f"{before:,} → {after:,} rows")

    # Report
    print_summary(result_df)

    if args.execute and not result_df.empty:
        # Mode interaktif: konfirmasi per grup lalu DELETE langsung
        interactive_delete(result_df, client, yes_all=args.yes_all)
        # Tetap simpan Excel sebagai catatan
        if not args.dry_run:
            xlsx_path = save_excel(result_df, args.output_dir)
            print(f"  📊 Excel  : {xlsx_path}")
    elif not args.dry_run and not result_df.empty:
        xlsx_path = save_excel(result_df, args.output_dir)
        if not args.no_sql:
            sql_path = generate_sql_deletes(result_df, args.output_dir)
            print(f"\n  📊 Excel  : {xlsx_path}")
            print(f"  🗑️  SQL   : {sql_path}")
        else:
            print(f"\n  📊 Excel  : {xlsx_path}")
    elif args.dry_run:
        log.info("Dry-run mode — no files saved.")

    log.info("Done.")


if __name__ == "__main__":
    main()
