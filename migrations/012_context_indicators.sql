-- MAREA — Bloque 1: indicadores de CONTEXTO de régimen (con auto-activación)
-- Ejecutar DESPUÉS de 001…011. NO toca ninguna tabla anterior.
--
-- POR QUÉ ESTA TABLA
-- Añadimos indicadores macro que AFINAN la lectura de régimen (risk-on/off/
-- flight-to-safety) pero que NO son flujos de liquidez: son TERMÓMETROS de
-- estado, igual filosofía que ^VIX/CRYPTO_FNG. Por eso NO van por el carril de
-- flow_scores (donde "entra/sale dinero" y se rankea), sino a su propia tabla.
-- Así quedan excluidos de rankings y alertas de flujo POR CONSTRUCCIÓN, no por
-- una lista de exclusión que haya que mantener.
--
-- INDICADORES (Bloque 1):
--   'btc_dominance' → % de capitalización de BTC sobre el total crypto (CoinGecko
--                     /global). Sube = rotación de alts hacia BTC (miedo crypto);
--                     baja = apetito por riesgo en crypto.
--   'credit_spread' → comportamiento relativo HYG (high-yield) vs LQD (investment
--                     grade) por yfinance. value = ratio HYG/LQD. Cae = el high
--                     yield sufre más → ensanchamiento de spreads → risk-off.
--   'yield_curve'   → spread 10Y-2Y en puntos porcentuales (^TNX − 2YY=F, ambos
--                     en % directo). value < 0 = curva invertida → señal de
--                     recesión / risk-off defensivo.
--   (put/call OMITIDO: sin fuente gratuita y fiable — ver app/ingest/context_runner.py)
--
-- AUTO-ACTIVACIÓN
-- Cada indicador se "enciende" solo cuando acumula suficientes observaciones
-- (settings.context_min_obs). Mientras tanto se marca preliminar / se omite del
-- parte y NO modula el régimen. La tabla se llena sola ciclo a ciclo; sin backfill.
--
-- Guardamos una fila por (ts lógico de día, indicador). 'value' es el número
-- principal; 'extra' guarda los componentes crudos (hyg, lqd, tnx, two_y, eth…)
-- para auditoría y para recomponer la señal sin recalcular.

CREATE TABLE IF NOT EXISTS context_indicators (
    id          BIGSERIAL    PRIMARY KEY,
    ts          TIMESTAMPTZ  NOT NULL,                 -- ts lógico del ciclo (medianoche UTC del día)
    indicator   TEXT         NOT NULL CHECK (indicator IN (
                                'btc_dominance',
                                'credit_spread',
                                'yield_curve'
                            )),
    value       DOUBLE PRECISION,                      -- valor principal del indicador
    extra       JSONB        NOT NULL DEFAULT '{}',    -- componentes crudos para auditoría
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Idempotencia: re-ingerir el mismo día/indicador ACTUALIZA, no duplica.
    UNIQUE (ts, indicator)
);

-- La auto-activación y la dirección (trend) leen la serie de cada indicador
-- ordenada por ts: índice por (indicator, ts DESC).
CREATE INDEX IF NOT EXISTS idx_context_indicators_indicator_ts
    ON context_indicators (indicator, ts DESC);
