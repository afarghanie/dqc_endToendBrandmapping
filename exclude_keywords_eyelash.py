"""
Exclude keywords untuk dm_Eyelash_al
Dikumpulkan dari analisis frekuensi ListingName - kata yang SERING muncul tapi BUKAN brand
"""

EXCLUDE_KEYWORDS_EYELASH = [

    # ── Promo / Logistik e-commerce ──────────────────────────────────────────
    'premium', 'free', 'murah', 'gratis', 'terlaris', 'termurah',
    'original', 'ready', 'stock', 'new', 'best', 'seller', 'hits',
    'promo', 'grosir', 'eceran', 'paket', 'bundle', 'custom', 'hemat',
    'harga', 'jual', 'berkualitas', 'kualitas', 'cod', 'wsp', 'bonus',
    'diskon', 'flash', 'sale', 'ori', 'asli', 'official', 'import',
    'resmi', 'tempel', 'langsung', 'beli',

    # ── Satuan / Kemasan ─────────────────────────────────────────────────────
    'pcs', 'pack', 'box', 'set', 'satuan', 'single', 'lusinan',
    'baris', 'pasang', 'pairs', 'eceran', 'kode', 'isi', 'satu',
    'per', 'helai', 'strip', 'combo',

    # ── Sertifikasi / Regulasi ───────────────────────────────────────────────
    'bpom', 'halal', 'sertifikat', 'certified',

    # ── Atribut Produk Generic ───────────────────────────────────────────────
    'natural', 'alami', 'lentik', 'tebal', 'lembut', 'halus', 'ringan',
    'nyaman', 'praktis', 'mudah', 'soft', 'smooth', 'reusable', 'reuseable',
    'waterproof', 'tahan', 'lama', 'bebas', 'anti', 'perih', 'pedih',
    'berperekat', 'perekat', 'adhesive', 'magnetik', 'magnetic', 'magnet',
    'easy', 'daily', 'pro', 'cepat',

    # ── Ukuran / Dimensi ─────────────────────────────────────────────────────
    '8mm', '9mm', '10mm', '11mm', '12mm', '13mm', '14mm', '15mm',
    'short', 'medium', 'long', 'mini', 'big', 'mega', 'jumbo', 'extra',
    'super', 'pendek', 'panjang', 'sedang', 'kecil', 'besar', 'tinggi',
    'size', 'ukuran', 'small', 'large',

    # ── Jenis / Style Eyelash ────────────────────────────────────────────────
    'curl', 'curly', 'russian', 'wispy', 'cluster', 'individual', 'flare',
    'knot', 'classic', 'hybrid', 'volume', 'fanning', 'extension',
    'extention', 'ekstensi', 'ekstension', 'lift', 'lashlift', 'tanam',
    'sambung', 'sulam', 'douyin', 'anime', 'manga', 'fairy', 'glam',
    'look', 'style', 'dramatic',

    # ── Material / Bahan ─────────────────────────────────────────────────────
    'mink', 'silk', 'serat', 'fiber', 'synthetic', 'sintetis', 'silikon',
    'stainless', 'gel', 'cream', 'tinta', 'bahan',

    # ── Asal Negara / Style Origin ───────────────────────────────────────────
    'korea', 'korean', 'japanese', 'thailand',

    # ── Warna ────────────────────────────────────────────────────────────────
    'black', 'hitam', 'putih', 'pink', 'brown', 'bening', 'transparan',
    'berlian', 'warna',

    # ── Produk/Alat Terkait (bukan brand) ───────────────────────────────────
    'lem', 'glue', 'remover', 'bond', 'tweezer', 'tweezers',
    'pinset', 'penjepit', 'brush', 'kuas', 'spatula', 'microbrush',
    'aplikator', 'kapas', 'eyepatch', 'sisir', 'alat', 'ring', 'cincin',
    'mika', 'cover', 'penutup', 'tempat', 'tutup', 'kotak', 'wadah',
    'pot', 'stiker', 'sticker', 'maskara', 'mascara', 'eyeliner',
    'eyeshadow', 'foundation', 'bedak', 'palette', 'palet', 'kosmetik',
    'kecantikan', 'riasan', 'rias', 'alis', 'aksesoris',

    # ── Descriptor Pemasaran / Generic ───────────────────────────────────────
    'tanpa', 'cocok', 'wajah', 'pemula', 'diy', 'self', 'berulang',
    'lengkap', 'seri', 'series', 'model', 'type', 'tipe', 'gaya',
    'shape', 'shaped', 'duo', 'trio', 'double', 'mix', 'mixing',
    'baby', 'plus', 'full',

    # ── Kata Produk Eyelash Generic (nama produk, bukan brand) ───────────────
    'false', 'fake', 'faux', 'eyelash', 'eyelashes', 'lash', 'lashes',
    'bulumata', 'palsu',
]
