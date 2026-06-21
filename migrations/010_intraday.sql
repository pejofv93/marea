-- MAREA — Sesión 9b: carril intradía
-- Ejecutar DESPUÉS de 001…009.
--
-- Dos nuevas tablas que CONVIVEN con el carril diario sin tocarlo.
-- raw_snapshots_intraday: ts REAL de la barra (no aplastado a medianoche).
-- flow_scores_intraday:   scores de corto plazo, ventanas '4h' y '1d_intraday'.
--
-- El carril diario (raw_snapshots, flow_scores, regimes…) queda INTACTO.

-- ──────────────────────────────────────────
-- 1. Snapshots crudos intradía
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_snapshots_intraday (
    id          BIGSERIAL PRIMARY KEY,
    asset_id    BIGINT      NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,     -- timestamp REAL de la barra (no medianoche)
    interval    TEXT        NOT NULL CHECK (interval IN ('15m','60m')),
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    extra       JSONB       NOT NULL DEFAULT '{}'
);

ALTER TABLE raw_snapshots_intraday
    ADD CONSTRAINT raw_snapshots_intraday_asset_ts_interval_key
    UNIQUE (asset_id, ts, interval);

CREATE INDEX IF NOT EXISTS idx_raw_snapshots_intraday_asset_ts
    ON raw_snapshots_intraday (asset_id, ts DESC);

-- ──────────────────────────────────────────
-- 2. Flow scores intradía
--    win: '4h'          → últimas 4 barras×60m (o 16×15m) — señal de horas
--         '1d_intraday' → últimas 8 barras×60m (o 32×15m) — sesión completa
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS flow_scores_intraday (
    id          BIGSERIAL PRIMARY KEY,
    asset_id    BIGINT      NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,     -- ts de la última barra usada en el cálculo
    interval    TEXT        NOT NULL CHECK (interval IN ('15m','60m')),
    win         TEXT        NOT NULL CHECK (win IN ('4h','1d_intraday')),
    score       DOUBLE PRECISION NOT NULL,
    raw_zscore  DOUBLE PRECISION,
    proxy_used  TEXT,
    n_obs       INT         NOT NULL DEFAULT 0,
    confidence  TEXT        NOT NULL DEFAULT 'ok'
                    CHECK (confidence IN ('ok','low'))
);

ALTER TABLE flow_scores_intraday
    ADD CONSTRAINT flow_scores_intraday_asset_ts_interval_win_key
    UNIQUE (asset_id, ts, interval, win);

CREATE INDEX IF NOT EXISTS idx_flow_scores_intraday_asset_ts
    ON flow_scores_intraday (asset_id, ts DESC);

-- ──────────────────────────────────────────
-- 3. Ampliar el CHECK de alerts para incluir 'intraday_flow'
--    El constraint original de 008_alerts.sql solo tenía 4 tipos.
-- ──────────────────────────────────────────
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_alert_type_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_alert_type_check
    CHECK (alert_type IN (
        'flow_extreme',
        'regime_change',
        'decoupling',
        'exposure',
        'intraday_flow'
    ));
