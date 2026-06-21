-- MAREA — Sesión 2: assets crypto y on-chain
-- Ejecutar DESPUÉS de 001_init.sql

-- ──────────────────────────────────────────
-- 1. Ampliar el CHECK constraint de asset_class
--    (no se puede modificar in-place en Postgres: DROP + re-ADD)
-- ──────────────────────────────────────────
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_asset_class_check;
ALTER TABLE assets ADD CONSTRAINT assets_asset_class_check
    CHECK (asset_class IN ('index','etf','commodity','macro','crypto','onchain'));

-- ──────────────────────────────────────────
-- 2. Seeds: 7 nuevos assets fijos
-- ──────────────────────────────────────────
INSERT INTO assets (ticker, name, asset_class, sector, source, is_fixed) VALUES
-- Precios spot crypto (CoinGecko)
('BTC',          'Bitcoin',                       'crypto',  'l1',               'coingecko',      TRUE),
('ETH',          'Ethereum',                      'crypto',  'l1',               'coingecko',      TRUE),
-- Perpetuos crypto — mark price + funding + OI (Binance Futures)
-- Tickers separados de BTC/ETH spot para evitar conflicto de upsert entre fuentes
('BTC_PERP',     'Bitcoin Perpetual (Binance)',    'crypto',  'perp',             'binance',        TRUE),
('ETH_PERP',     'Ethereum Perpetual (Binance)',   'crypto',  'perp',             'binance',        TRUE),
-- Stablecoin supply on-chain (DefiLlama)
('STABLES_USDT', 'USDT Circulating Supply',        'onchain', 'stablecoin',       'defillama',      TRUE),
('STABLES_USDC', 'USDC Circulating Supply',        'onchain', 'stablecoin',       'defillama',      TRUE),
-- Sentimiento crypto (Alternative.me)
('CRYPTO_FNG',   'Crypto Fear & Greed Index',      'macro',   'crypto_sentiment', 'alternative_me', TRUE)
ON CONFLICT (ticker) DO NOTHING;
