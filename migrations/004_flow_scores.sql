-- MAREA — Sesión 4: tabla flow_scores
-- Ejecutar DESPUÉS de 001, 002 y 003.

CREATE TABLE IF NOT EXISTS flow_scores (
    id          BIGSERIAL   PRIMARY KEY,
    asset_id    BIGINT      NOT NULL REFERENCES assets(id),
    ts          TIMESTAMPTZ NOT NULL,
    win         TEXT        NOT NULL CHECK (win IN ('7d','30d')),
    score       FLOAT,                  -- clipeado a [-1, +1]
    raw_zscore  FLOAT,                  -- sin clipear, para debug
    proxy_used  TEXT        NOT NULL,   -- qué señal generó el score
    n_obs       INT         NOT NULL,   -- observaciones en la ventana
    confidence  TEXT        NOT NULL CHECK (confidence IN ('ok','low')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, ts, win)
);

CREATE INDEX IF NOT EXISTS idx_flow_scores_asset_ts
    ON flow_scores (asset_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_flow_scores_ts_win
    ON flow_scores (ts DESC, win);
