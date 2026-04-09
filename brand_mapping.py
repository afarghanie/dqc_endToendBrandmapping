import asyncio
import math
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


INPUT_FILE = "ch_ui_export_2026-04-08T10-27-37.xlsx"
OUTPUT_FILE = "brandmapping_kopi.xlsx"

URL_COL = "ListingLink"
LISTING_NAME_COL = "ListingName"
REFERENCE_BRAND_COL = "Brand"   # kalau ada isi dari SQL, dipakai sebagai referensi tambahan

CONCURRENCY = 10
STAGE_PERCENT = 20           # simpan hasil setiap 20%
TARGET_PERCENT = 100            # proses sampai berapa persen total data
REQUEST_TIMEOUT_MS = 45000
MAX_RETRY = 2
HEADLESS = True
MIN_ACCEPT_CONFIDENCE = 0.60    # kalau di bawah ini, BrandName dikosongkan agar lebih konservatif


GENERIC_WORDS = {
    "fashion", "summer", "winter", "autumn", "spring",
    "push", "up", "pushup", "comfort", "comfortable",
    "simple", "breathable", "premium", "new", "best",
    "original", "ori", "promo", "sale", "diskon",
    "murah", "bagus", "wanita", "pria", "cowok", "cewek",
    "bra", "bh", "shirt", "tshirt", "kaos", "hoodie",
    "pants", "celana", "sepatu", "tas", "anak", "dewasa",
    "small", "large", "big", "mini", "maxi",
    "light", "soft", "thin", "busa", "tipis",
    "korean", "style", "modern", "casual"
}


@dataclass
class BrandResult:
    brand_name: str
    confidence: float
    source: str
    status: str
    note: str


def clean(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize(text: str) -> str:
    text = clean(text).lower()
    text = re.sub(r"[^a-z0-9&\-\.\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_probably_generic(candidate: str) -> bool:
    parts = normalize(candidate).split()
    if not parts:
        return True
    generic_hits = sum(1 for p in parts if p in GENERIC_WORDS)
    return generic_hits >= max(1, math.ceil(len(parts) * 0.6))


def score_candidate(
    candidate: str,
    source: str,
    listing_name: str = "",
    reference_brand: str = ""
) -> float:
    """
    Score 0.0 - 1.0
    """
    c = clean(candidate)
    if not c:
        return 0.0

    n = normalize(c)
    if not n:
        return 0.0

    score = 0.0

    # 1) Base score dari sumber
    source_score = {
        "json_brand": 0.88,
        "json_brand_name": 0.86,
        "json_item_brand": 0.84,
        "structured_brand": 0.90,
        "label_inline": 0.82,
        "label_sibling": 0.78,
        "meta_brand": 0.80,
        "store_hint": 0.45,
        "title_hint": 0.35,
        "unknown": 0.20
    }
    score += source_score.get(source, 0.20)

    # 2) Panjang teks
    char_len = len(c)
    word_len = len(n.split())

    if 2 <= char_len <= 25:
        score += 0.08
    elif char_len <= 40:
        score += 0.03
    else:
        score -= 0.18

    if 1 <= word_len <= 3:
        score += 0.08
    elif word_len == 4:
        score += 0.03
    else:
        score -= 0.12

    # 3) Penalti jika terlalu deskriptif
    if is_probably_generic(c):
        score -= 0.30

    # 4) Penalti kalau ada angka terlalu dominan
    digit_count = sum(ch.isdigit() for ch in c)
    if digit_count >= max(2, len(c) // 3):
        score -= 0.10

    # 5) Bonus kalau sama dengan kolom Brand referensi
    ref = clean(reference_brand)
    if ref:
        if normalize(ref) == n:
            score += 0.22
        elif normalize(ref) in n or n in normalize(ref):
            score += 0.10

    # 6) Bonus kecil kalau kandidat muncul juga di ListingName
    ln = normalize(listing_name)
    if ln and n in ln:
        score += 0.05

    # 7) Penalti untuk kata terlalu umum
    if n in GENERIC_WORDS:
        score -= 0.35

    # Clamp
    score = max(0.0, min(score, 0.99))
    return round(score, 4)


async def build_page(context):
    page = await context.new_page()
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media", "font"}
        else route.continue_()
    )
    return page


async def extract_candidates(page, url: str) -> List[Dict[str, str]]:
    await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
    await page.wait_for_timeout(1500)

    candidates = await page.evaluate("""
    () => {
        function clean(t) {
            if (!t) return "";
            return t.replace(/\\s+/g, " ").trim();
        }

        const out = [];

        function pushCandidate(value, source) {
            value = clean(value);
            if (!value) return;
            out.push({ value, source });
        }

        // 1) cari label Brand / Merk / Merek
        const els = Array.from(document.querySelectorAll("body *"));

        for (const el of els) {
            const text = clean(el.innerText);
            if (!text) continue;

            // Brand: Nike
            let m = text.match(/^(brand|merk|merek)\\s*[:\\-]\\s*(.+)$/i);
            if (m) {
                pushCandidate(m[2], "label_inline");
            }

            // element berisi hanya Brand, sibling berisi value
            if (/^(brand|merk|merek)$/i.test(text)) {
                if (el.nextElementSibling) {
                    const val = clean(el.nextElementSibling.innerText);
                    if (val) pushCandidate(val, "label_sibling");
                }

                if (el.parentElement) {
                    const texts = Array.from(el.parentElement.children)
                        .map(x => clean(x.innerText))
                        .filter(Boolean);
                    const idx = texts.findIndex(x => /^(brand|merk|merek)$/i.test(x));
                    if (idx >= 0 && idx + 1 < texts.length) {
                        pushCandidate(texts[idx + 1], "label_sibling");
                    }
                }
            }
        }

        // 2) structured data / json
        const scripts = Array.from(document.querySelectorAll("script"));

        for (const s of scripts) {
            const t = s.innerText || s.textContent || "";
            if (!t) continue;

            let m = t.match(/"brand"\\s*:\\s*"([^"]+)"/i);
            if (m) pushCandidate(m[1], "json_brand");

            m = t.match(/"brand_name"\\s*:\\s*"([^"]+)"/i);
            if (m) pushCandidate(m[1], "json_brand_name");

            m = t.match(/"item_brand"\\s*:\\s*"([^"]+)"/i);
            if (m) pushCandidate(m[1], "json_item_brand");

            // JSON-LD brand.name
            m = t.match(/"brand"\\s*:\\s*\\{[^\\}]*"name"\\s*:\\s*"([^"]+)"/i);
            if (m) pushCandidate(m[1], "structured_brand");
        }

        // 3) meta tags
        const metas = Array.from(document.querySelectorAll("meta"));
        for (const meta of metas) {
            const name = (meta.getAttribute("name") || meta.getAttribute("property") || "").toLowerCase();
            const content = clean(meta.getAttribute("content") || "");
            if (!content) continue;

            if (name.includes("brand")) {
                pushCandidate(content, "meta_brand");
            }
        }

        // dedup
        const seen = new Set();
        return out.filter(x => {
            const key = (x.value + "||" + x.source).toLowerCase();
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }
    """)
    return candidates or []


def pick_best_candidate(
    candidates: List[Dict[str, str]],
    listing_name: str,
    reference_brand: str
) -> BrandResult:
    if not candidates:
        return BrandResult("", 0.0, "", "not_found", "no candidate")

    scored = []
    for c in candidates:
        value = clean(c.get("value", ""))
        source = clean(c.get("source", "unknown"))
        conf = score_candidate(
            candidate=value,
            source=source,
            listing_name=listing_name,
            reference_brand=reference_brand
        )
        scored.append((value, conf, source))

    scored.sort(key=lambda x: x[1], reverse=True)
    best_value, best_conf, best_source = scored[0]

    if best_conf < MIN_ACCEPT_CONFIDENCE:
        return BrandResult("", best_conf, best_source, "low_confidence", best_value)

    return BrandResult(best_value, best_conf, best_source, "success", "")


async def extract_brand_with_retry(
    page,
    url: str,
    listing_name: str,
    reference_brand: str
) -> BrandResult:
    if not url or not isinstance(url, str):
        return BrandResult("", 0.0, "", "invalid_url", "empty url")

    if "shopee" not in url.lower() and "tiktok" not in url.lower():
        return BrandResult("", 0.0, "", "invalid_url", "unsupported domain")

    last_error = ""
    for attempt in range(MAX_RETRY + 1):
        try:
            candidates = await extract_candidates(page, url)
            return pick_best_candidate(candidates, listing_name, reference_brand)
        except PlaywrightTimeoutError:
            last_error = f"timeout_attempt_{attempt+1}"
        except Exception as e:
            last_error = f"error_attempt_{attempt+1}: {type(e).__name__}"

        await page.wait_for_timeout(1000 * (attempt + 1))

    return BrandResult("", 0.0, "", "error", last_error)


def ensure_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    needed = {
        "BrandName": "",
        "BrandConfidence": 0.0,
        "BrandSource": "",
        "ResultStatus": "",
        "ResultNote": ""
    }
    for col, default in needed.items():
        if col not in df.columns:
            df[col] = default
    return df


def save_output(df: pd.DataFrame, output_file: str):
    df.to_excel(output_file, index=False)


def get_stage_ranges(total_rows: int, target_percent: int, stage_percent: int):
    target_rows = math.ceil(total_rows * target_percent / 100)
    stage_rows = max(1, math.ceil(total_rows * stage_percent / 100))

    ranges = []
    start = 0
    while start < target_rows:
        end = min(start + stage_rows, target_rows)
        ranges.append((start, end))
        start = end
    return ranges


async def worker(name: str, context, queue: asyncio.Queue, df: pd.DataFrame):
    page = await build_page(context)
    try:
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break

            idx = item
            url = clean(df.at[idx, URL_COL]) if URL_COL in df.columns else ""
            listing_name = clean(df.at[idx, LISTING_NAME_COL]) if LISTING_NAME_COL in df.columns else ""
            reference_brand = clean(df.at[idx, REFERENCE_BRAND_COL]) if REFERENCE_BRAND_COL in df.columns else ""

            result = await extract_brand_with_retry(
                page=page,
                url=url,
                listing_name=listing_name,
                reference_brand=reference_brand
            )

            df.at[idx, "BrandName"] = result.brand_name
            df.at[idx, "BrandConfidence"] = result.confidence
            df.at[idx, "BrandSource"] = result.source
            df.at[idx, "ResultStatus"] = result.status
            df.at[idx, "ResultNote"] = result.note

            print(
                f"[{name}] row={idx} status={result.status} "
                f"brand={result.brand_name} conf={result.confidence}"
            )

            queue.task_done()
    finally:
        await page.close()


async def process_stage(context, df: pd.DataFrame, start_idx: int, end_idx: int):
    queue = asyncio.Queue()

    # hanya masukkan row yang belum sukses
    for idx in range(start_idx, end_idx):
        existing_status = clean(df.at[idx, "ResultStatus"])
        if existing_status == "success":
            continue
        await queue.put(idx)

    workers = [
        asyncio.create_task(worker(f"W{i+1}", context, queue, df))
        for i in range(CONCURRENCY)
    ]

    for _ in workers:
        await queue.put(None)

    await queue.join()
    await asyncio.gather(*workers)


async def run():
    if os.path.exists(OUTPUT_FILE):
        print(f"Resume dari file existing: {OUTPUT_FILE}")
        df = pd.read_excel(OUTPUT_FILE)
    else:
        df = pd.read_excel(INPUT_FILE)

    if URL_COL not in df.columns:
        raise ValueError(f"Kolom '{URL_COL}' tidak ditemukan.")

    df = ensure_output_columns(df)

    total_rows = len(df)
    stage_ranges = get_stage_ranges(total_rows, TARGET_PERCENT, STAGE_PERCENT)

    print(f"Total rows      : {total_rows}")
    print(f"Target percent  : {TARGET_PERCENT}%")
    print(f"Stage percent   : {STAGE_PERCENT}%")
    print(f"Concurrency     : {CONCURRENCY}")
    print(f"Total stages    : {len(stage_ranges)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768}
        )

        for stage_no, (start_idx, end_idx) in enumerate(stage_ranges, start=1):
            print(f"\\n=== Stage {stage_no}: rows {start_idx} - {end_idx - 1} ===")
            await process_stage(context, df, start_idx, end_idx)
            save_output(df, OUTPUT_FILE)
            done_percent = round(end_idx / total_rows * 100, 2)
            print(f"Saved after stage {stage_no}. Progress: {done_percent}% -> {OUTPUT_FILE}")

        await context.close()
        await browser.close()

    print("\\nSelesai.")


if __name__ == "__main__":
    asyncio.run(run())