-- ============================================================
-- DQC AUTO-GENERATED DELETE — dm_Mandom
-- Generated: 2026-05-06 15:56:48
-- Periode  : 2026-04-03 s/d 2026-04-30
-- Item diurutkan: GMV terbesar → terkecil
-- REVIEW DULU sebelum dijalankan!
-- ============================================================

-- [Tiktok x Tokopedia] × [Paket Perawatan Wajah] | Spike: 2026-04-12 | Window: 2025-12-13~2026-08-10
-- Items terdampak: 1 (sorted by GMV desc)
ALTER TABLE default.dm_Msglow
DELETE WHERE
    Channel   = 'Tiktok x Tokopedia'
    AND ScrapDate BETWEEN '2026-04-12' AND '2026-04-12'
    AND DailySalesValue > 0
    AND L3Title = 'Paket Perawatan Wajah'
    AND ItemId IN ('1734821484008539426');
