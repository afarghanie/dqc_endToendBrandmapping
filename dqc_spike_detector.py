"""
=======================================================================
DQC Spike Detector 
=======================================================================
Deteksi anomali spike di setiap Channel × L3Title untuk dm
Periode: 1 Januari 2026 s/d 31 Maret 2026

Flag anomali (sama seperti SQL reusable):
  [A] STALE_FROZEN_DAILY    : DailySalesCount frozen (≤2 nilai unik) dalam window
  [B] NEW_ITEM_UNIFORM_COUNT: Item baru + DailySalesCount seragam
  [C] CUMULATIVE_SALESCOUNT : DailySalesCount / SalesCount > 0.7
  [D] SPIKE_JUMP            : DailySalesCount pada hari spike > N× rata-rata baseline
  [F] GHOST_REAPPEARANCE    : Item absen lama lalu muncul kembali; DailySalesCount =
                               akumulasi penjualan selama gap (delta SalesCount)
  [G] BRAND_NEW_SPIKE       : Item benar-benar baru (never seen before) yang muncul
                               dengan DailySalesCount tinggi pada hari spike

Output:
  - Summary tabel per Channel × L3Title (spike dates + GMV terdampak)
  - CSV detail semua item yang ter-flag
  - SQL DELETE statements siap pakai per spike date yang ditemukan

Usage:
  python dqc_spike_detector.py
  Koneksi dibaca otomatis dari .env di direktori yang sama.
  python dqc_spike_detector.py --channel "Tiktok x Tokopedia" --l3title "Cleanser"
  python dqc_spike_detector.py --dry-run --output-dir ./output
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

TABLE_NAME         = "dm_Multimedika"
PERIOD_FROM        = date(2026, 4, 9) 
PERIOD_TO          = date(2026, 4, 28)
# Detection window radius di sekitar candidate spike date (hari)
DETECTION_RADIUS   = 28   # baseline window = spike_date ± 7 hari (dikecualikan spike_date itu sendiri)

# Threshold parameter
MIN_DAILY_COUNT    = 10   # DailySalesCount minimal agar dianggap signifikan
MIN_BASELINE_DAYS  = 2    # Minimal baseline hari untuk SPIKE_JUMP
SPIKE_JUMP_MULT    = 3    # [D] daily > N× avg_baseline = spike
CUMULATIVE_RATIO   = 0.7  # [C] daily/total > ratio ini = spike
FROZEN_MAX_UNIQUE  = 2    # [A/B] max nilai unik DailySalesCount agar dianggap frozen
FROZEN_MIN_DAYS    = 2    # [A/B] min hari agar frozen check valid
IQR_MULT           = 3.0  # [E] IQR-based outlier: daily > Q3 + IQR_MULT × IQR

# Ghost / brand-new detection (flag F & G)
GHOST_MIN_GAP_DAYS    = 30   # [F] gap minimum (hari) sejak scrape terakhir agar di-flag
GHOST_DELTA_TOLERANCE = 2    # [F] toleransi |DailySalesCount - delta_SalesCount| yang masih dianggap match
GHOST_NEW_MIN_COUNT   = 5    # [G] min DailySalesCount agar brand new item dianggap mencurigakan

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


def fetch_raw_data(
    client,
    channel_filter=None,
    l3title_filter=None,
    brand_filter=None,
    keyword_category_filter=None,
) -> pd.DataFrame:
    """
    Ambil semua baris dm_AryaNoble dalam periode yang relevan.
    Data yang diambil: per (ItemId, ScrapDate, Channel, L3Title, Brand, KeywordCategory).
    """
    log.info("Fetching raw data from ClickHouse ...")

    # Extend window untuk baseline: ambil DETECTION_RADIUS hari sebelum & sesudah periode
    fetch_from = PERIOD_FROM - timedelta(days=DETECTION_RADIUS)
    fetch_to   = PERIOD_TO   + timedelta(days=DETECTION_RADIUS)

    table_columns = {
        str(row[0])
        for row in client.query(f"DESCRIBE TABLE default.{TABLE_NAME}").result_rows
    }
    has_keyword_category = "KeywordCategory" in table_columns
    keyword_category_select = "KeywordCategory" if has_keyword_category else "'' AS KeywordCategory"

    channel_clause          = f"AND Channel = '{channel_filter}'"                   if channel_filter          else ""
    l3title_clause          = f"AND L3Title = '{l3title_filter}'"                   if l3title_filter          else ""
    brand_clause            = f"AND Brand = '{brand_filter}'"                       if brand_filter            else ""
    keyword_category_clause = ""
    if keyword_category_filter:
        if has_keyword_category:
            keyword_category_clause = f"AND KeywordCategory = '{keyword_category_filter}'"
        else:
            log.warning(
                f"Table {TABLE_NAME} has no KeywordCategory column; "
                "--keyword-category filter is ignored."
            )

    query = f"""
        SELECT
            ItemId,
            ScrapDate,
            Channel,
            L3Title,
            Brand,
            {keyword_category_select},
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
          {keyword_category_clause}
    """

    result = client.query(query)
    cols = [
        "ItemId", "ScrapDate", "Channel", "L3Title", "Brand",
        "KeywordCategory",
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
    Menjalankan 7 flag anomali per (Channel, L3Title):
      [A] STALE_FROZEN_DAILY
      [B] NEW_ITEM_UNIFORM_COUNT
      [C] CUMULATIVE_SALESCOUNT
      [D] SPIKE_JUMP
      [E] IQR_OUTLIER      (tambahan statistik — deteksi candidate date)
      [F] GHOST_REAPPEARANCE  (item absen lama, DailySalesCount = akumulasi delta)
      [G] BRAND_NEW_SPIKE     (item baru pertama kali muncul dengan count tinggi)
    """

    def __init__(self, df: pd.DataFrame, client=None):
        self.df = df
        self.client = client   # diperlukan untuk stage 2 query [F] dan [G]
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
    # [F] + [G] Stage-2 helper: query CH untuk last scrape sebelum
    # spike_date, diluar window yang sudah di-fetch
    # ────────────────────────────────────────────────────────────────
    def _fetch_last_seen_before(
        self,
        channel: str,
        spike_date: date,
        item_ids: list[str],
    ) -> dict[str, dict]:
        """
        Untuk setiap ItemId dalam item_ids, cari:
          - last_seen  : MAX(ScrapDate) sebelum spike_date
          - last_count : MAX(toInt64OrNull(SalesCount)) pada last_seen

        Return: {item_id: {"last_seen": date, "last_count": int | None}}
        Item yang tidak pernah muncul sebelumnya tidak akan ada di dict (→ kandidat [G]).
        """
        if not item_ids or self.client is None:
            return {}

        chunk_size = 500
        result_map: dict[str, dict] = {}

        for i in range(0, len(item_ids), chunk_size):
            chunk = item_ids[i : i + chunk_size]
            ids_literal = ", ".join(f"'{x}'" for x in chunk)
            query = f"""
                SELECT
                    ItemId,
                    MAX(ScrapDate)                        AS last_seen,
                    argMax(toInt64OrNull(SalesCount), ScrapDate) AS last_count
                FROM default.{TABLE_NAME}
                WHERE Channel  = '{channel}'
                  AND ScrapDate < '{spike_date}'
                  AND ItemId IN ({ids_literal})
                GROUP BY ItemId
            """
            try:
                res = self.client.query(query)
                for row in res.result_rows:
                    item_id, last_seen, last_count = row
                    result_map[str(item_id)] = {
                        "last_seen":  last_seen if isinstance(last_seen, date) else last_seen,
                        "last_count": last_count,
                    }
            except Exception as e:
                log.warning(f"  [ghost] _fetch_last_seen_before error: {e}")

        return result_map

    # ────────────────────────────────────────────────────────────────
    # [F] GHOST_REAPPEARANCE
    # Item yang absen > GHOST_MIN_GAP_DAYS, lalu muncul kembali di
    # spike_date dengan DailySalesCount ≈ akumulasi delta SalesCount
    # selama gap tersebut.
    # ────────────────────────────────────────────────────────────────
    def _flag_ghost_reappearance(
        self,
        grp: pd.DataFrame,
        target_date: date,
        last_seen_map: dict[str, dict],
        ghost_min_gap: int = GHOST_MIN_GAP_DAYS,
        ghost_tolerance: int = GHOST_DELTA_TOLERANCE,
    ) -> tuple[set[str], dict[str, int]]:
        """
        Return:
          flagged_ids : set of ItemId yang lolos filter [F]
          gap_days_map: {item_id: gap_days} untuk kolom output
        """
        day = grp[grp["ScrapDate"] == target_date].copy()
        if day.empty or not last_seen_map:
            return set(), {}

        flagged: set[str] = set()
        gap_days_map: dict[str, int] = {}

        for _, row in day.iterrows():
            item_id = str(row["ItemId"])
            if item_id not in last_seen_map:
                continue

            info       = last_seen_map[item_id]
            last_seen  = info["last_seen"]
            last_count = info["last_count"]

            # Hitung gap
            if isinstance(last_seen, date):
                gap = (target_date - last_seen).days
            else:
                try:
                    gap = (target_date - pd.to_datetime(last_seen).date()).days
                except Exception:
                    continue

            if gap <= ghost_min_gap:
                continue

            # Verifikasi delta: |DailySalesCount - (SalesCount_today - last_count)| <= tolerance
            sales_count_today = pd.to_numeric(row["SalesCount"], errors="coerce")
            if pd.notna(sales_count_today) and last_count is not None:
                delta = sales_count_today - last_count
                if abs(row["DailySalesCount"] - delta) <= ghost_tolerance:
                    flagged.add(item_id)
                    gap_days_map[item_id] = gap
            else:
                # Jika SalesCount tidak tersedia, pakai gap saja sebagai sinyal
                if gap > ghost_min_gap:
                    flagged.add(item_id)
                    gap_days_map[item_id] = gap

        return flagged, gap_days_map

    # ────────────────────────────────────────────────────────────────
    # [G] BRAND_NEW_SPIKE
    # Item benar-benar baru (tidak pernah muncul di CH sebelumnya)
    # yang muncul pada spike_date dengan DailySalesCount tinggi.
    # ────────────────────────────────────────────────────────────────
    def _flag_brand_new_spike(
        self,
        grp: pd.DataFrame,
        target_date: date,
        new_item_ids: set[str],
        ghost_new_min_count: float = GHOST_NEW_MIN_COUNT,
    ) -> set[str]:
        """
        Return: set of ItemId yang termasuk brand-new + DailySalesCount mencurigakan.
        Kriteria tambahan: item hanya muncul pada target_date, tidak ada di hari setelahnya
        dalam window (one-day appearance = lebih mencurigakan).
        """
        if not new_item_ids:
            return set()

        day = grp[
            (grp["ScrapDate"] == target_date) &
            (grp["ItemId"].astype(str).isin(new_item_ids)) &
            (grp["DailySalesCount"] > ghost_new_min_count)
        ]
        if day.empty:
            return set()

        # Secondary check: tidak muncul di hari setelah target_date dalam window
        next_day = target_date + timedelta(days=1)
        seen_after = set(
            grp[grp["ScrapDate"] >= next_day]["ItemId"].astype(str).unique()
        )
        flagged = set(day["ItemId"].astype(str)) - seen_after
        return flagged

    # ────────────────────────────────────────────────────────────────
    # [E] IQR_OUTLIER  (tambahan: aggregate harian per channel-l3title)
    # Deteksi tanggal mana yang total GMV-nya outlier dibandingkan
    # kondisi NORMAL sebelum periode (pre-period baseline).
    #
    # Sebelumnya: IQR dihitung hanya dari tanggal dalam periode itu
    # sendiri → kalau seluruh periode elevated (sustained spike),
    # tidak ada outlier internal → gagal deteksi.
    #
    # Sekarang: IQR dihitung dari hari-hari SEBELUM PERIOD_FROM
    # (pre-period = kondisi normal), lalu threshold-nya dipakai untuk
    # menilai apakah hari-hari di dalam periode itu anomali.
    # Fallback ke metode lama kalau pre-period data tidak cukup.
    # ────────────────────────────────────────────────────────────────
    def _find_spike_dates_iqr(self, grp: pd.DataFrame) -> list[date]:
        """
        Temukan tanggal-tanggal yang total DailySalesValue (agregat) outlier
        dibandingkan baseline pre-period (hari-hari normal sebelum periode).
        Returns list of candidate spike dates dalam periode target.
        """
        daily_agg = grp.groupby("ScrapDate")["DailySalesValue"].sum().reset_index()
        daily_agg = daily_agg.sort_values("ScrapDate")

        # ── Data periode target (yang akan dievaluasi) ──
        period_data = daily_agg[
            (daily_agg["ScrapDate"] >= PERIOD_FROM) &
            (daily_agg["ScrapDate"] <= PERIOD_TO)
        ]
        if len(period_data) < 3:
            return []

        # ── Pre-period data sebagai baseline "kondisi normal" ──
        # Ambil semua hari yang ter-fetch sebelum PERIOD_FROM
        pre_period = daily_agg[daily_agg["ScrapDate"] < PERIOD_FROM]

        if len(pre_period) >= 7:
            # ✅ Cukup data pre-period → pakai sebagai baseline normal
            baseline_vals = pre_period["DailySalesValue"].values
            q1, q3 = np.percentile(baseline_vals, 25), np.percentile(baseline_vals, 75)
            iqr    = q3 - q1

            if iqr == 0:
                # Pre-period semua flat → pakai median + IQR_MULT × std sebagai fallback
                upper = np.median(baseline_vals) * (1 + IQR_MULT * 0.1)
            else:
                upper = q3 + IQR_MULT * iqr

            log.debug(
                f"    [IQR] pre-period baseline: {len(pre_period)} hari, "
                f"Q3={q3/1e6:.1f}jt, upper={upper/1e6:.1f}jt"
            )
            spike_dates = period_data[
                period_data["DailySalesValue"] > upper
            ]["ScrapDate"].tolist()

        else:
            # ⚠️ Pre-period tidak cukup → fallback ke metode lama (internal period IQR)
            log.debug(
                f"    [IQR] pre-period hanya {len(pre_period)} hari "
                f"(< 7) → fallback ke within-period IQR"
            )
            vals = period_data["DailySalesValue"].values
            if len(vals) < 7:
                return []
            q1, q3 = np.percentile(vals, 25), np.percentile(vals, 75)
            iqr    = q3 - q1
            upper  = q3 + IQR_MULT * iqr
            spike_dates = period_data[
                period_data["DailySalesValue"] > upper
            ]["ScrapDate"].tolist()

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
        enable_ghost  = getattr(self, "enable_ghost", True)
        ghost_min_gap = getattr(self, "ghost_min_gap", GHOST_MIN_GAP_DAYS)
        ghost_tol     = getattr(self, "ghost_tolerance", GHOST_DELTA_TOLERANCE)
        ghost_new_min = getattr(self, "ghost_new_min_count", GHOST_NEW_MIN_COUNT)
        results = []

        # ── Helper: tambahkan metadata kolom ke spike_items ──
        def enrich(df, spike_date, win_from, win_to, flag_map, gap_days_map=None):
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
            # kolom gap_days: diisi hanya untuk flag F, None untuk yang lain
            if gap_days_map:
                df["gap_days"] = df["ItemId"].astype(str).map(gap_days_map)
            else:
                df["gap_days"] = None
            return df

        # ── Semua tanggal dalam periode ──
        all_dates = sorted(grp[
            (grp["ScrapDate"] >= PERIOD_FROM) &
            (grp["ScrapDate"] <= PERIOD_TO)
        ]["ScrapDate"].unique())

        # ── Candidate spike dates via IQR — selalu dijalankan (dipakai untuk stage 2) ──
        iqr_candidate_dates = set(self._find_spike_dates_iqr(grp))
        candidate_dates = iqr_candidate_dates if include_stale else set()

        processed_dates = set()

        # ─────────────────────────────────────────────────────────
        # Lapisan 1: candidate IQR spike dates (A+B+C+D + F+G)
        # ─────────────────────────────────────────────────────────
        for spike_date in iqr_candidate_dates:
            win_from, win_to = self._detection_window(spike_date)

            flag_C = self._flag_cumulative(grp, spike_date)
            flag_D = self._flag_spike_jump(grp, spike_date, win_from, win_to)

            flag_A = self._flag_stale_frozen(grp, win_from, win_to)     if include_stale else set()
            flag_B = self._flag_new_item_uniform(grp, win_from, win_to) if include_stale else set()

            # ── Stage 2: ghost detection [F] dan [G] ──
            flag_F: set[str] = set()
            flag_G: set[str] = set()
            gap_days_map: dict[str, int] = {}

            if enable_ghost:
                # Items di spike_date yang tidak ada dalam window sebelumnya
                items_in_window_before = set(
                    grp[
                        (grp["ScrapDate"] >= win_from) &
                        (grp["ScrapDate"] < spike_date)
                    ]["ItemId"].astype(str)
                )
                items_on_spike = set(
                    grp[grp["ScrapDate"] == spike_date]["ItemId"].astype(str)
                )
                candidate_ghost_ids = list(items_on_spike - items_in_window_before)

                if candidate_ghost_ids and self.client:
                    last_seen_map = self._fetch_last_seen_before(
                        channel, spike_date, candidate_ghost_ids
                    )
                    ghost_candidates = {i for i in candidate_ghost_ids if i in last_seen_map}
                    new_candidates   = {i for i in candidate_ghost_ids if i not in last_seen_map}

                    flag_F, gap_days_map = self._flag_ghost_reappearance(
                        grp, spike_date, last_seen_map,
                        ghost_min_gap=ghost_min_gap,
                        ghost_tolerance=ghost_tol,
                    )
                    flag_G = self._flag_brand_new_spike(
                        grp, spike_date, new_candidates,
                        ghost_new_min_count=ghost_new_min,
                    )

                    if flag_F:
                        log.info(f"    → [F] GHOST_REAPPEARANCE: {len(flag_F)} items on {spike_date}")
                    if flag_G:
                        log.info(f"    → [G] BRAND_NEW_SPIKE: {len(flag_G)} items on {spike_date}")

            all_flagged = flag_A | flag_B | flag_C | flag_D | flag_F | flag_G
            if not all_flagged:
                processed_dates.add(spike_date)
                continue

            spike_items = grp[
                (grp["ScrapDate"] == spike_date) &
                (grp["ItemId"].astype(str).isin(all_flagged)) &
                (grp["DailySalesValue"] > 0)
            ].copy()

            if not spike_items.empty:
                def assign_flag(item_id, _fA=flag_A, _fB=flag_B, _fC=flag_C,
                                _fD=flag_D, _fF=flag_F, _fG=flag_G):
                    sid = str(item_id)
                    if item_id in _fA or sid in _fA: return "STALE_FROZEN_DAILY"
                    if item_id in _fB or sid in _fB: return "NEW_ITEM_UNIFORM_COUNT"
                    if item_id in _fC or sid in _fC: return "CUMULATIVE_SALESCOUNT"
                    if item_id in _fD or sid in _fD: return "SPIKE_JUMP"
                    if sid in _fF:                   return "GHOST_REAPPEARANCE"
                    if sid in _fG:                   return "BRAND_NEW_SPIKE"
                    return "UNKNOWN"

                flag_map = {i: assign_flag(i) for i in spike_items["ItemId"]}
                results.extend(
                    enrich(spike_items, spike_date, win_from, win_to, flag_map, gap_days_map)
                    .to_dict("records")
                )

            processed_dates.add(spike_date)

        # ─────────────────────────────────────────────────────────
        # Lapisan 2: semua tanggal, hanya flag C & D
        # (F & G tidak dijalankan di lapisan ini karena tidak ada
        #  IQR signal — aggregate tidak anomali)
        # ─────────────────────────────────────────────────────────
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

    def run_all(
        self,
        channels=None,
        l3titles=None,
        include_stale=False,
        enable_ghost=True,
        ghost_min_gap=GHOST_MIN_GAP_DAYS,
        ghost_tolerance=GHOST_DELTA_TOLERANCE,
        ghost_new_min_count=GHOST_NEW_MIN_COUNT,
    ) -> pd.DataFrame:
        self.include_stale      = include_stale
        self.enable_ghost       = enable_ghost
        self.ghost_min_gap      = ghost_min_gap
        self.ghost_tolerance    = ghost_tolerance
        self.ghost_new_min_count = ghost_new_min_count
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
        "spike_date", "Channel", "L3Title", "KeywordCategory", "Brand", "spike_flag",
        "ItemId", "ShopName", "ListingName",
        "SalePrice", "DailySalesCount", "gmv_jt", "daily_to_total_ratio",
        "gap_days", "win_from", "win_to",
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
            "gap_days":             "Gap Hari (F)",
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
    parser.add_argument("--channel",           help="Filter ke satu Channel tertentu")
    parser.add_argument("--l3title",           help="Filter ke satu L3Title tertentu")
    parser.add_argument("--brand",             help="Filter ke satu Brand tertentu (opsional)")
    parser.add_argument("--keyword-category",  dest="keyword_category",
                        help="Filter ke satu KeywordCategory tertentu (contoh: 'Hair Restore')")
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
    parser.add_argument("--no-ghost", action="store_true",
                        help="Nonaktifkan flag [F] GHOST_REAPPEARANCE dan [G] BRAND_NEW_SPIKE "
                             "(default: aktif)")
    parser.add_argument("--ghost-gap", type=int, default=GHOST_MIN_GAP_DAYS,
                        help=f"[F] Gap minimum hari sejak scrape terakhir (default: {GHOST_MIN_GAP_DAYS})")
    parser.add_argument("--ghost-tolerance", type=int, default=GHOST_DELTA_TOLERANCE,
                        help=f"[F] Toleransi |DailySalesCount - delta_SalesCount| (default: {GHOST_DELTA_TOLERANCE})")
    parser.add_argument("--ghost-min-count", type=float, default=GHOST_NEW_MIN_COUNT,
                        help=f"[G] Min DailySalesCount agar brand new item di-flag (default: {GHOST_NEW_MIN_COUNT})")
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

    enable_ghost = not args.no_ghost

    log.info("=" * 60)
    log.info(f"DQC Spike Detector — {TABLE_NAME}")
    log.info(f"Periode  : {PERIOD_FROM} s/d {PERIOD_TO}")
    log.info(f"Channel  : {args.channel or 'ALL'}")
    log.info(f"L3Title  : {args.l3title or 'ALL'}")
    log.info(f"Brand    : {args.brand or 'ALL'}")
    log.info(f"KeyCat   : {args.keyword_category or 'ALL'}")
    log.info(f"Params   : spike_mult={SPIKE_JUMP_MULT}×, iqr_mult={IQR_MULT}×, "
             f"min_count={MIN_DAILY_COUNT}")
    log.info(f"Ghost    : {'ENABLED' if enable_ghost else 'DISABLED'}"
             + (f" (gap≥{args.ghost_gap}d, tol={args.ghost_tolerance}, "
                f"new_min={args.ghost_min_count})" if enable_ghost else ""))
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
        keyword_category_filter=args.keyword_category,
    )

    if df.empty:
        log.warning("No data returned. Check your filters / connection.")
        sys.exit(0)

    # Detect
    log.info("Running anomaly detection ...")
    detector  = SpikeDetector(df, client=client)
    result_df = detector.run_all(
        channels=[args.channel] if args.channel else None,
        l3titles=[args.l3title] if args.l3title else None,
        include_stale=args.include_stale,
        enable_ghost=enable_ghost,
        ghost_min_gap=args.ghost_gap,
        ghost_tolerance=args.ghost_tolerance,
        ghost_new_min_count=args.ghost_min_count,
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
