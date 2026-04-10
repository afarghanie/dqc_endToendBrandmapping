import pandas as pd
import clickhouse_connect
import os
from dotenv import load_dotenv
import logging

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("injection.log", mode="w")
    ]
)
logger = logging.getLogger(__name__)

# Config
EXCEL_FILE = "advanced_brand_discovery_result.xlsx"
TARGET_TABLE = "default.dm_Kopi_rpr_copy_al"
DRY_RUN = False  # Set ke False untuk benar-benar meninju ClickHouse
BATCH_SIZE = 100  # Jumlah mapping per 1 Query Mutasi

def get_db_client():
    load_dotenv()
    return clickhouse_connect.get_client(
        host=os.getenv('CLICKHOUSE_HOST'),
        port=int(os.getenv('CLICKHOUSE_PORT', 8123)),
        username=os.getenv('CLICKHOUSE_USER'),
        password=os.getenv('CLICKHOUSE_PASSWORD')
    )

def escape_sql(text):
    """Menghindari SQL Injection/Error karena kutip tunggal"""
    return str(text).replace("'", "''")

def execute_injection():
    if not os.path.exists(EXCEL_FILE):
        logger.error(f"File {EXCEL_FILE} tidak ditemukan!")
        return

    logger.info(f"Membaca {EXCEL_FILE}...")
    df = pd.read_excel(EXCEL_FILE)
    
    # Filter hanya status yang relevan
    valid_statuses = ["Auto-Matched (Synonym)", "Existing Brand"]
    df_valid = df[df['Status'].isin(valid_statuses)].copy()
    
    mapping_rules = []
    
    for idx, row in df_valid.iterrows():
        candidate = str(row['Candidate Name']).strip()
        suggested = str(row['Suggested Exact Brand']).strip()
        
        # Logika Fallback: Jika Suggested kosong/nan, gunakan Candidate Name dan kapitalisasi
        if pd.isna(row['Suggested Exact Brand']) or suggested == "" or suggested.lower() == "nan":
            final_brand = candidate.title()
        else:
            final_brand = suggested
            
        mapping_rules.append({
            "keyword": candidate,
            "brand": final_brand
        })
        
    logger.info(f"Ditemukan {len(mapping_rules)} mapping valid dari Excel.")
    
    if len(mapping_rules) == 0:
        logger.info("Tidak ada data untuk diupdate.")
        return
        
    client = get_db_client()
    
    # Chunking eksekusi untuk menghindari batas max_ast_elements di ClickHouse
    chunks = [mapping_rules[i:i + BATCH_SIZE] for i in range(0, len(mapping_rules), BATCH_SIZE)]
    
    logger.info(f"Memulai {len(chunks)} batch mutasi SQL (Dry Run: {DRY_RUN})...")
    
    for batch_idx, batch in enumerate(chunks, 1):
        conditions = []
        or_clauses = []
        
        for rule in batch:
            kw_escaped = escape_sql(rule['keyword'])
            br_escaped = escape_sql(rule['brand'])
            # multiIf args: (kondisi, hasil)
            conditions.append(f"ListingName ILIKE '%{kw_escaped}%'")
            conditions.append(f"'{br_escaped}'")
            or_clauses.append(f"ListingName ILIKE '%{kw_escaped}%'")
            
        # Gabung klausul multiIf
        multi_if_body = ",\n        ".join(conditions)
        # default value jika jatuh semua (walau tertahan WHERE sih harusnya)
        multi_if_body += ",\n        Brand" 
        
        where_or = " OR ".join(or_clauses)
        
        mutation_query = f"""
        ALTER TABLE {TARGET_TABLE}
        UPDATE Brand = multiIf(
            {multi_if_body}
        )
        WHERE Brand = 'No Brand' 
        AND ({where_or})
        """
        
        if DRY_RUN:
            if batch_idx == 1:
                logger.info(f"[DRY RUN] Snapshot Query Batch 1:\n{mutation_query[:1000]}...\n(Terpotong karena panjang)")
            logger.info(f"[DRY RUN] [Batch {batch_idx}/{len(chunks)}] Query berhasil di-generate. (Ukuran: {len(batch)} mappings)")
        else:
            try:
                # Mutasi di clickhouse dilakukan asynchronous. Oleh karena re-write parts berat.
                client.command(mutation_query)
                logger.info(f"✔ [Batch {batch_idx}/{len(chunks)}] Mutasi sukses dikirim (menunggu background update oleh ClickHouse).")
            except Exception as e:
                logger.error(f"❌ Error pada Batch {batch_idx}: {e}")
                
    if DRY_RUN:
        logger.info("ℹ️ Ini hanya DRY RUN. Tidak ada data di database ClickHouse yang berubah.")
        logger.info("Untuk benar-benar mengeksekusinya, ubah variabel DRY_RUN = False pada line 19 di script.")
    else:
        logger.info(f"✅ Injeksi Selesai! ClickHouse sedang memproses pembaruan partisi part {TARGET_TABLE} di backround.")

if __name__ == "__main__":
    execute_injection()
