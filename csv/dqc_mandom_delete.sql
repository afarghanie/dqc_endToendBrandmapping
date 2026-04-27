-- ============================================================
-- DQC AUTO-GENERATED DELETE — dm_Mandom
-- Generated: 2026-04-27 13:53:40
-- Periode  : 2026-04-15 s/d 2026-04-21
-- Item diurutkan: GMV terbesar → terkecil
-- REVIEW DULU sebelum dijalankan!
-- ============================================================

-- [Lazada] × [Rice Cooker] | Spike: 2026-04-18 | Window: 2026-04-11~2026-04-25
-- Items terdampak: 1 (sorted by GMV desc)
ALTER TABLE default.dm_Electrolux
DELETE WHERE
    Channel   = 'Lazada'
    AND ScrapDate BETWEEN '2026-04-18' AND '2026-04-18'
    AND DailySalesValue > 0
    AND L3Title = 'Rice Cooker'
    AND ItemId IN ('7180426738');
