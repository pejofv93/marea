-- MAREA — migración inicial
-- Ejecutar en Supabase SQL Editor (o via psql apuntando a la instancia)

-- ──────────────────────────────────────────
-- 1. Tabla de catálogo de assets
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assets (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    asset_class TEXT        NOT NULL CHECK (asset_class IN ('index','etf','commodity','macro')),
    sector      TEXT,
    source      TEXT        NOT NULL DEFAULT 'yfinance',
    is_fixed    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────────────────
-- 2. Tabla de snapshots crudos
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    asset_id    BIGINT      NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    extra       JSONB       NOT NULL DEFAULT '{}'
);

-- Constraint único para upsert idempotente
ALTER TABLE raw_snapshots
    ADD CONSTRAINT raw_snapshots_asset_id_ts_key UNIQUE (asset_id, ts);

-- Índice compuesto para queries de series temporales por asset
CREATE INDEX IF NOT EXISTS idx_raw_snapshots_asset_ts
    ON raw_snapshots (asset_id, ts DESC);

-- ──────────────────────────────────────────
-- 3. Seed: universo fijo (is_fixed = true)
-- ──────────────────────────────────────────
INSERT INTO assets (ticker, name, asset_class, sector, source, is_fixed) VALUES
-- Índices macro globales
('^GSPC',    'S&P 500',                            'index',     NULL,               'yfinance', TRUE),
('^IXIC',    'Nasdaq Composite',                   'index',     NULL,               'yfinance', TRUE),
('^IBEX',    'IBEX 35',                            'index',     NULL,               'yfinance', TRUE),
('^N225',    'Nikkei 225',                         'index',     NULL,               'yfinance', TRUE),
-- Commodities
('GC=F',     'Gold Futures',                       'commodity', 'metals',           'yfinance', TRUE),
('SI=F',     'Silver Futures',                     'commodity', 'metals',           'yfinance', TRUE),
-- Macro / tipos
('DX-Y.NYB', 'US Dollar Index',                   'macro',     'currency',         'yfinance', TRUE),
('^VIX',     'CBOE Volatility Index',              'macro',     'volatility',       'yfinance', TRUE),
('^TNX',     '10-Year Treasury Yield',             'macro',     'rates',            'yfinance', TRUE),
-- ETFs principales
('SPY',      'SPDR S&P 500 ETF',                  'etf',       'broad_market',     'yfinance', TRUE),
('QQQ',      'Invesco QQQ Trust',                  'etf',       'broad_market',     'yfinance', TRUE),
('GLD',      'SPDR Gold Shares',                   'etf',       'commodities',      'yfinance', TRUE),
('SLV',      'iShares Silver Trust',               'etf',       'commodities',      'yfinance', TRUE),
('IBIT',     'iShares Bitcoin Trust',              'etf',       'crypto',           'yfinance', TRUE),
-- ETFs sectoriales
('SOXX',     'iShares Semiconductor ETF',          'etf',       'semiconductor',    'yfinance', TRUE),
('SMH',      'VanEck Semiconductor ETF',           'etf',       'semiconductor',    'yfinance', TRUE),
('XME',      'SPDR S&P Metals & Mining ETF',       'etf',       'metals_mining',    'yfinance', TRUE),
('GDX',      'VanEck Gold Miners ETF',             'etf',       'gold_miners',      'yfinance', TRUE),
('SIL',      'Global X Silver Miners ETF',         'etf',       'silver_miners',    'yfinance', TRUE),
('ITA',      'iShares US Aerospace & Defense ETF', 'etf',       'aerospace_defense','yfinance', TRUE),
('XAR',      'SPDR S&P Aerospace & Defense ETF',   'etf',       'aerospace_defense','yfinance', TRUE),
('XLE',      'Energy Select Sector SPDR ETF',      'etf',       'energy',           'yfinance', TRUE),
('XLK',      'Technology Select Sector SPDR ETF',  'etf',       'technology',       'yfinance', TRUE),
('XLF',      'Financial Select Sector SPDR ETF',   'etf',       'financials',       'yfinance', TRUE),
('XLV',      'Health Care Select Sector SPDR ETF', 'etf',       'healthcare',       'yfinance', TRUE)
ON CONFLICT (ticker) DO NOTHING;
