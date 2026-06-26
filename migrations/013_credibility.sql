-- MAREA — Bloque 2: capa de CREDIBILIDAD del flujo
-- Ejecutar DESPUÉS de 001…012. NO toca ninguna tabla anterior (solo AÑADE
-- columnas nullable a flow_scores y flow_scores_intraday).
--
-- POR QUÉ ESTAS COLUMNAS
-- El flow score sale casi siempre de UN proxy (volumen). El volumen solo no
-- distingue un flujo SANO (volumen + precio acompañando + sostenido) de un
-- FOGONAZO (pico aislado, o volumen sin que el precio confirme). La capa de
-- credibilidad cruza volumen+precio (+persistencia cuando hay histórico) para
-- juzgar si el flujo es creíble y PENALIZA el score cuando no lo está:
--
--     score (penalizado) = score_raw (bruto) × credibility
--
-- Guardamos AMBOS para auditoría y para el digest:
--   score        → YA penalizado (lo que consumen rankings, régimen, alertas).
--                  Así todo el downstream queda limpio de fogonazos sin cambios.
--   score_raw    → el score bruto pre-credibilidad (lo que 'score' era antes).
--   credibility  → factor [0..1] aplicado.
--   credibility_label  → 'confirmado' | 'dudoso' | 'fogonazo' (etiqueta legible).
--   credibility_reason → motivo corto en es (p. ej. "precio plano (sin confirmación)").
--
-- DISTINTO DE confidence: 'confidence' (ok/low) mide si hay SUFICIENTE HISTÓRICO
-- (cold start); 'credibility' mide si ESTE flujo concreto es CREÍBLE. Son dos
-- ejes independientes (un flujo puede tener histórico de sobra y aun así ser un
-- fogonazo, y viceversa). Por eso viven en columnas separadas.
--
-- AUTO-ACTIVACIÓN: la persistencia solo influye cuando hay suficientes
-- observaciones (settings.credibility_persist_min_obs); por debajo, la
-- credibilidad se calcula solo con volumen+precio (activos desde el día 1).
-- Las columnas son nullable: filas antiguas y assets sin credibilidad (VIX, FNG,
-- DXY, bonos, stablecoins) las dejan en NULL sin romper nada.

-- ── flow_scores (carril diario) ───────────────────────────────────────────────
ALTER TABLE flow_scores ADD COLUMN IF NOT EXISTS score_raw          FLOAT;
ALTER TABLE flow_scores ADD COLUMN IF NOT EXISTS credibility         FLOAT;
ALTER TABLE flow_scores ADD COLUMN IF NOT EXISTS credibility_label   TEXT;
ALTER TABLE flow_scores ADD COLUMN IF NOT EXISTS credibility_reason  TEXT;

ALTER TABLE flow_scores DROP CONSTRAINT IF EXISTS flow_scores_credibility_label_check;
ALTER TABLE flow_scores ADD CONSTRAINT flow_scores_credibility_label_check
    CHECK (credibility_label IS NULL OR credibility_label IN
        ('confirmado','dudoso','fogonazo'));

-- ── flow_scores_intraday (carril intradía) ────────────────────────────────────
ALTER TABLE flow_scores_intraday ADD COLUMN IF NOT EXISTS score_raw          FLOAT;
ALTER TABLE flow_scores_intraday ADD COLUMN IF NOT EXISTS credibility         FLOAT;
ALTER TABLE flow_scores_intraday ADD COLUMN IF NOT EXISTS credibility_label   TEXT;
ALTER TABLE flow_scores_intraday ADD COLUMN IF NOT EXISTS credibility_reason  TEXT;

ALTER TABLE flow_scores_intraday DROP CONSTRAINT IF EXISTS flow_scores_intraday_credibility_label_check;
ALTER TABLE flow_scores_intraday ADD CONSTRAINT flow_scores_intraday_credibility_label_check
    CHECK (credibility_label IS NULL OR credibility_label IN
        ('confirmado','dudoso','fogonazo'));
