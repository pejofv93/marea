-- MAREA — Sesión 6: mapa de exposición indirecta (cotizados con exposición a privadas/cripto)
-- Ejecutar DESPUÉS de 001, 002, 003, 004 y 005.

-- Constraint a nivel de BD que impide persistir sin fuentes reales.
-- La defensa principal es en código (verify.py), pero esta es la red de seguridad.
CREATE TABLE IF NOT EXISTS exposures (
    id               BIGSERIAL    PRIMARY KEY,
    source_entity    TEXT         NOT NULL,   -- 'OpenAI', 'BTC', 'SpaceX'…
    exposed_ticker   TEXT         NOT NULL,   -- cotizado/ETF afectado: 'MSFT', 'MSTR'…
    exposure_type    TEXT         NOT NULL CHECK (exposure_type IN ('pre_ipo','crypto')),
    relationship     TEXT         NOT NULL,   -- descripción breve de la exposición
    confidence       TEXT         NOT NULL CHECK (
                                     confidence IN ('confirmado_oficial','rumor_prensa','especulacion')
                                 ),
    sources          JSONB        NOT NULL DEFAULT '[]',  -- array de URLs reales
    llm_engine       TEXT         NOT NULL,   -- 'groq' | 'gemini'
    discovered_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_verified_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (source_entity, exposed_ticker, exposure_type),
    -- Red de seguridad a nivel BD: NUNCA persistas sin fuentes
    CONSTRAINT exposures_sources_not_empty CHECK (jsonb_array_length(sources) > 0)
);

CREATE INDEX IF NOT EXISTS idx_exposures_entity ON exposures (source_entity);
CREATE INDEX IF NOT EXISTS idx_exposures_ticker  ON exposures (exposed_ticker);
CREATE INDEX IF NOT EXISTS idx_exposures_type    ON exposures (exposure_type, confidence);
