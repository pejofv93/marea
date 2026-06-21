-- MAREA — Sesión 5: análisis intermercado (régimen, correlaciones, rotación sectorial)
-- Ejecutar DESPUÉS de 001, 002, 003 y 004.

-- Régimen de mercado detectado por reglas deterministas
CREATE TABLE IF NOT EXISTS regimes (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    win         TEXT        NOT NULL CHECK (win IN ('7d','30d')),
    regime      TEXT        NOT NULL,               -- risk_on | risk_off | flight_to_safety | sector_rotation | neutral
    confidence  FLOAT       NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    signals     JSONB       NOT NULL DEFAULT '[]',  -- condiciones que dispararon el régimen
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ts, win)
);

CREATE INDEX IF NOT EXISTS idx_regimes_ts ON regimes (ts DESC);

-- Correlaciones móviles entre pares (intermercado y sectorial)
CREATE TABLE IF NOT EXISTS correlations (
    id            BIGSERIAL   PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL,
    win           TEXT        NOT NULL CHECK (win IN ('7d','30d')),
    matrix_type   TEXT        NOT NULL CHECK (matrix_type IN ('intermarket','sector')),
    pair_a        TEXT        NOT NULL,
    pair_b        TEXT        NOT NULL,
    corr          FLOAT,
    is_decoupling BOOLEAN     NOT NULL DEFAULT FALSE,  -- True en row 7d cuando se desacopla vs 30d
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ts, win, matrix_type, pair_a, pair_b)
);

CREATE INDEX IF NOT EXISTS idx_correlations_ts ON correlations (ts DESC, matrix_type);

-- Eventos de rotación sectorial detectados
CREATE TABLE IF NOT EXISTS rotations (
    id           BIGSERIAL   PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    from_sector  TEXT        NOT NULL,
    to_sector    TEXT        NOT NULL,
    strength     FLOAT       NOT NULL CHECK (strength BETWEEN 0 AND 1),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ts, from_sector, to_sector)
);

CREATE INDEX IF NOT EXISTS idx_rotations_ts ON rotations (ts DESC);
