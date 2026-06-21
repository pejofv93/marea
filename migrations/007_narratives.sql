-- MAREA — Sesión 7: capa narrativa (LLM explica el snapshot interno, sin web)
-- Ejecutar DESPUÉS de 001, 002, 003, 004, 005 y 006.

-- Cada fila = una narrativa generada para un timestamp (normalmente medianoche UTC).
-- snapshot_json guarda exactamente qué datos vio el LLM → auditable post-hoc.
-- UNIQUE (ts): regenerar para el mismo día actualiza en vez de duplicar (idempotente).
CREATE TABLE IF NOT EXISTS narratives (
    id              BIGSERIAL    PRIMARY KEY,
    ts              TIMESTAMPTZ  NOT NULL,
    regime_at_ts    TEXT         NOT NULL,              -- régimen vigente al generar
    confidence      FLOAT        NOT NULL DEFAULT 0.0,  -- confianza heredada del snapshot
    text            TEXT         NOT NULL,              -- narrativa generada
    snapshot_json   JSONB        NOT NULL DEFAULT '{}', -- snapshot que la generó (auditoría)
    llm_engine      TEXT         NOT NULL DEFAULT 'groq',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (ts)
);

CREATE INDEX IF NOT EXISTS idx_narratives_ts ON narratives (ts DESC);
