-- ============================================================
-- DQC: Spike Detection & DELETE — dm_Wacoal
--
-- Filter ShopName: 'wacoal_id' dan 'Indonesia Wacoal Official Shop'
-- Channel       : Tiktok
-- Detection     : 2026-03-16 s/d 2026-03-30
-- Target (spike): 2026-03-23 (ubah sesuai kebutuhan)
-- spike_jump_mult: 5x
--
-- Flag:
--   [A] STALE_FROZEN_DAILY    : DailySalesCount frozen dalam detection window
--   [B] NEW_ITEM_UNIFORM_COUNT: Item baru + DailySalesCount seragam
--   [C] CUMULATIVE_SALESCOUNT : DailySalesCount / SalesCount > 0.7
--   [D] SPIKE_JUMP            : DailySalesCount target > N x rata-rata baseline
-- ============================================================


-- ============================================================
-- [STEP 1 - PREVIEW] Item yang ter-flag — jalankan ini dulu
-- ============================================================
WITH params AS (
    -- ⬇️  UBAH DI SINI
    SELECT
        'dm_Wacoal'          AS table_name,
        toDate('2026-03-28') AS detection_from,    -- window LEBAR (awal)
        toDate('2026-04-15') AS detection_to,      -- window LEBAR (akhir)
        toDate('2026-04-11') AS target_from,       -- tanggal spike yang mau di-cek / DELETE
        toDate('2026-04-11') AS target_to,         -- tanggal spike yang mau di-cek / DELETE
        'Tiktok x Tokopedia'             AS channel,
        2                    AS min_days,           -- min hari detection window untuk stale
        10                    AS min_count,          -- min DailySalesCount agar signifikan
        5                    AS spike_jump_mult     -- [D] flag kalau daily target > N x avg baseline
),

-- [A+B] Statistik per item dalam DETECTION window
spike_stats AS (
    SELECT
        e.ItemId,
        e.Channel,
        COUNT(DISTINCT e.ScrapDate)       AS days_seen,
        COUNT(DISTINCT e.DailySalesCount) AS unique_daily_count_vals,
        MIN(e.DailySalesCount)            AS min_daily_count,
        MAX(e.DailySalesCount)            AS max_daily_count,
        if(
            COUNT(DISTINCT e.DailySalesCount) <= 2
            AND COUNT(DISTINCT e.ScrapDate) >= (SELECT min_days FROM params),
            1, 0
        ) AS is_frozen_count
    FROM default.dm_Wacoal e, params p
    WHERE e.Channel   = p.channel
      AND e.ScrapDate BETWEEN p.detection_from AND p.detection_to
      AND e.DailySalesCount > 0
      AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
    GROUP BY e.ItemId, e.Channel
),

-- First seen per item (untuk cek item baru)
item_history AS (
    SELECT e.ItemId, e.Channel, MIN(e.ScrapDate) AS first_seen_ever
    FROM default.dm_Wacoal e, params p
    WHERE e.Channel = p.channel
      AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
    GROUP BY e.ItemId, e.Channel
),

-- Klasifikasi flag [A] dan [B]
flagged AS (
    SELECT ss.ItemId,
        CASE
            WHEN ss.is_frozen_count = 1
                AND ss.max_daily_count > (SELECT min_count FROM params)
                THEN 'STALE_FROZEN_DAILY'
            WHEN ih.first_seen_ever >= (SELECT detection_from FROM params)
                AND ss.unique_daily_count_vals <= 2
                AND ss.min_daily_count > (SELECT min_count FROM params)
                THEN 'NEW_ITEM_UNIFORM_COUNT'
            ELSE NULL
        END AS spike_flag
    FROM spike_stats ss
    LEFT JOIN item_history ih ON ss.ItemId = ih.ItemId AND ss.Channel = ih.Channel
),

-- [C] DailySalesCount / SalesCount > 0.7
cumulative_flagged AS (
    SELECT DISTINCT e.ItemId
    FROM default.dm_Wacoal e, params p
    WHERE e.Channel   = p.channel
      AND e.ScrapDate BETWEEN p.target_from AND p.target_to
      AND e.DailySalesCount > (SELECT min_count FROM params)
      AND toFloat64OrNull(e.SalesCount) IS NOT NULL
      AND e.SalesCount != 'null'
      AND e.DailySalesCount / toFloat64(e.SalesCount) > 0.7
      AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
),

-- [D] SPIKE_JUMP: daily target >> avg baseline (non-target days)
baseline_stats AS (
    SELECT
        e.ItemId,
        e.Channel,
        AVG(e.DailySalesCount)          AS avg_baseline,
        COUNT(DISTINCT e.ScrapDate)     AS baseline_days
    FROM default.dm_Wacoal e, params p
    WHERE e.Channel   = p.channel
      -- baseline = detection window MINUS target dates
      AND e.ScrapDate BETWEEN p.detection_from AND p.detection_to
      AND (e.ScrapDate < p.target_from OR e.ScrapDate > p.target_to)
      AND e.DailySalesCount > 0
      AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
    GROUP BY e.ItemId, e.Channel
    HAVING COUNT(DISTINCT e.ScrapDate) >= 2   -- perlu minimal 2 hari baseline
),
spike_jump_flagged AS (
    SELECT DISTINCT t.ItemId
    FROM (
        SELECT e.ItemId, e.Channel, e.DailySalesCount AS target_daily
        FROM default.dm_Wacoal e, params p
        WHERE e.Channel   = p.channel
          AND e.ScrapDate BETWEEN p.target_from AND p.target_to
          AND e.DailySalesCount > (SELECT min_count FROM params)
          AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
    ) t
    INNER JOIN baseline_stats b ON t.ItemId = b.ItemId AND t.Channel = b.Channel
    WHERE b.avg_baseline > 0
      AND t.target_daily > b.avg_baseline * (SELECT spike_jump_mult FROM params)
)

SELECT
    e.ScrapDate,
    e.ItemId,
    e.L3Title,
    e.Channel,
    e.ShopName,
    e.ListingName,
    e.SalePrice,
    e.SalesCount,
    round(e.DailySalesCount, 0)                                              AS daily_count,
    round(e.DailySalesValue / 1e6, 3)                                        AS gmv_jt,
    round(e.DailySalesCount / nullIf(toFloat64OrNull(e.SalesCount), 0), 4)  AS daily_to_total_ratio,
    COALESCE(f.spike_flag,
        if(cf.ItemId IS NOT NULL, 'CUMULATIVE_SALESCOUNT',
           if(sj.ItemId IS NOT NULL, 'SPIKE_JUMP', NULL)
        )
    ) AS spike_flag
FROM default.dm_Wacoal e, params p
-- INNER JOIN: hanya item yang benar-benar ada di tabel pada target date
INNER JOIN (
    SELECT ItemId FROM flagged WHERE spike_flag IN ('STALE_FROZEN_DAILY', 'NEW_ITEM_UNIFORM_COUNT')
    UNION ALL SELECT ItemId FROM cumulative_flagged
    UNION ALL SELECT ItemId FROM spike_jump_flagged
) all_flagged ON e.ItemId = all_flagged.ItemId
LEFT JOIN flagged f             ON e.ItemId = f.ItemId
LEFT JOIN cumulative_flagged cf ON e.ItemId = cf.ItemId
LEFT JOIN spike_jump_flagged sj ON e.ItemId = sj.ItemId
WHERE e.Channel   = p.channel
  AND e.ScrapDate BETWEEN p.target_from AND p.target_to
  AND e.DailySalesValue > 0
  AND e.ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
ORDER BY gmv_jt DESC;


-- ============================================================
-- [STEP 2 - DELETE]
-- ⬇️  Samakan nilai literal ini dengan params di STEP 1:
--    channel          : 'Tiktok x Tokopedia'
--    shop_names       : 'wacoal_id', 'Indonesia Wacoal Official Shop'
--    target_from/to   : '2026-04-11'
--    detection window : '2026-03-28' s/d '2026-04-15'
--    min_count        : 10
--    spike_jump_mult  : 5
-- ============================================================
ALTER TABLE default.dm_Wacoal
DELETE WHERE
    Channel   = 'Tiktok x Tokopedia'
    AND ScrapDate BETWEEN '2026-04-11' AND '2026-04-11'
    AND DailySalesValue > 0
    AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
    AND ItemId IN (
        SELECT ItemId FROM (
            -- [A+B] Stale & New Uniform
            SELECT ss.ItemId
            FROM (
                SELECT
                    ItemId, Channel,
                    COUNT(DISTINCT DailySalesCount) AS unique_daily_count_vals,
                    MIN(DailySalesCount)            AS min_daily_count,
                    MAX(DailySalesCount)            AS max_daily_count,
                    if(COUNT(DISTINCT DailySalesCount) <= 2
                       AND COUNT(DISTINCT ScrapDate) >= 2, 1, 0) AS is_frozen_count
                FROM default.dm_Wacoal
                WHERE Channel   = 'Tiktok x Tokopedia'
                  AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
                  AND ScrapDate BETWEEN '2026-03-28' AND '2026-04-15'
                  AND DailySalesCount > 0
                GROUP BY ItemId, Channel
            ) ss
            LEFT JOIN (
                SELECT ItemId, Channel, MIN(ScrapDate) AS first_seen_ever
                FROM default.dm_Wacoal
                WHERE Channel = 'Tiktok x Tokopedia'
                  AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
                GROUP BY ItemId, Channel
            ) ih ON ss.ItemId = ih.ItemId AND ss.Channel = ih.Channel
            WHERE (ss.is_frozen_count = 1 AND ss.max_daily_count > 10)
               OR (ih.first_seen_ever >= '2026-03-28' AND ss.unique_daily_count_vals <= 2 AND ss.min_daily_count > 10)

            UNION ALL

            -- [C] Cumulative SalesCount
            SELECT DISTINCT ItemId
            FROM default.dm_Wacoal
            WHERE Channel   = 'Tiktok x Tokopedia'
              AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
              AND ScrapDate BETWEEN '2026-04-11' AND '2026-04-11'
              AND DailySalesCount > 10
              AND toFloat64OrNull(SalesCount) IS NOT NULL
              AND SalesCount != 'null'
              AND DailySalesCount / toFloat64(SalesCount) > 0.7

            UNION ALL

            -- [D] Spike Jump: daily target > 5x avg baseline
            SELECT DISTINCT t.ItemId
            FROM (
                SELECT ItemId, Channel, DailySalesCount AS target_daily
                FROM default.dm_Wacoal
                WHERE Channel   = 'Tiktok x Tokopedia'
                  AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
                  AND ScrapDate BETWEEN '2026-04-11' AND '2026-04-11'
                  AND DailySalesCount > 10
            ) t
            INNER JOIN (
                SELECT ItemId, Channel, AVG(DailySalesCount) AS avg_baseline
                FROM default.dm_Wacoal
                WHERE Channel   = 'Tiktok x Tokopedia'
                  AND ShopName IN ('wacoal_id', 'Indonesia Wacoal Official Shop')
                  AND ScrapDate BETWEEN '2026-03-28' AND '2026-04-15'
                  AND (ScrapDate < '2026-04-11' OR ScrapDate > '2026-04-11')
                  AND DailySalesCount > 0
                GROUP BY ItemId, Channel
                HAVING COUNT(DISTINCT ScrapDate) >= 2
            ) b ON t.ItemId = b.ItemId AND t.Channel = b.Channel
            WHERE b.avg_baseline > 0
              AND t.target_daily > b.avg_baseline * 5
        )
    );
         
