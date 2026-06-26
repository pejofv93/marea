-- MAREA — Sesión 12: capa de comparación temporal de los partes de Telegram
-- Ejecutar DESPUÉS de 001…010. NO toca ninguna tabla anterior.
--
-- POR QUÉ ESTA TABLA
-- El rediseño de los partes (digest.py) compone un bloque "🔄 Cambio desde…"
-- que compara el parte ACTUAL con el ANTERIOR relevante (la "película", no la
-- "foto"). Para hacerlo de forma honesta y uniforme entre el carril diario y el
-- intradía, cada ciclo persiste aquí los flow scores que REALMENTE usó, marcados
-- con su "momento" (apertura/media/cierre). El siguiente ciclo lee la fila previa
-- y calcula los deltas por activo.
--
-- Guardar los scores como JSONB (no una fila por activo) mantiene la tabla
-- compacta y hace trivial el diff: una sola lectura devuelve el set completo.
--
-- DEGRADACIÓN ELEGANTE (cold start): si no hay fila previa, el bloque 🔄 dice
-- explícitamente "sin parte anterior para comparar". Esta tabla se llena sola a
-- medida que MAREA corre; no requiere backfill.

CREATE TABLE IF NOT EXISTS digest_cycles (
    id          BIGSERIAL    PRIMARY KEY,
    ts          TIMESTAMPTZ  NOT NULL,                 -- ts lógico del ciclo (día o barra)
    rail        TEXT         NOT NULL CHECK (rail IN ('daily','intraday')),
    moment      TEXT         NOT NULL CHECK (moment IN ('apertura','media','cierre')),
    scores      JSONB        NOT NULL DEFAULT '[]',    -- [{ticker, score, asset_class, confidence}]
    regime      TEXT,                                  -- régimen vigente al componer (solo diario)
    confidence  DOUBLE PRECISION,                      -- confianza del régimen [0,1]
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),   -- orden REAL de emisión (clave para "el anterior")
    -- Idempotencia: re-emitir el mismo momento del mismo día/rail ACTUALIZA, no duplica.
    UNIQUE (ts, rail, moment)
);

-- "El parte anterior" se busca por orden de EMISIÓN real, no por ts lógico
-- (en intradía varios momentos comparten día). Por eso se ordena por created_at.
CREATE INDEX IF NOT EXISTS idx_digest_cycles_rail_created
    ON digest_cycles (rail, created_at DESC);
