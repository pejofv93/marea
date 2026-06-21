-- MAREA — Sesión 3: universo dinámico
-- Ejecutar DESPUÉS de 001 y 002.

-- ── 1. Renombrar 'source' → 'ingest_source' (semántica más clara) ─────────
ALTER TABLE assets RENAME COLUMN source TO ingest_source;

-- ── 2. Añadir is_active (soft-delete: el histórico NUNCA se borra) ─────────
ALTER TABLE assets ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- ── 3. Ampliar CHECK constraint para asset_class 'stock' ──────────────────
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_asset_class_check;
ALTER TABLE assets ADD CONSTRAINT assets_asset_class_check
    CHECK (asset_class IN ('index','etf','commodity','macro','crypto','onchain','stock'));

-- ── 4. Índices útiles para queries de ingesta y recálculo ─────────────────
CREATE INDEX IF NOT EXISTS idx_assets_active_source
    ON assets (ingest_source, is_active)
    WHERE is_active = TRUE;

-- ── 5. Tabla de auditoría: cuándo entró/salió cada asset del top-N ─────────
--    Permite backtest: "¿qué estaba en el top-20 el 1 de enero?"
CREATE TABLE IF NOT EXISTS universe_history (
    id          BIGSERIAL   PRIMARY KEY,
    asset_id    BIGINT      NOT NULL REFERENCES assets(id),
    action      TEXT        NOT NULL CHECK (action IN ('activated','deactivated')),
    reason      TEXT        NOT NULL,
    rank        INT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_universe_history_asset_ts
    ON universe_history (asset_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_universe_history_ts
    ON universe_history (ts DESC);
