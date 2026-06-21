-- Migración 009: Componentes auditables de confianza del régimen
--
-- Añade dos columnas a la tabla regimes para poder auditar por qué la
-- confianza final es la que es. La confianza final (columna confidence
-- existente) = structural_confidence × data_confidence_factor.
--
-- structural_confidence: proporción de señales de flujo y moduladores que
--   se alinearon, sin penalizar por calidad de datos. Rango [0, 1].
--
-- data_confidence_factor: penalización por cold start. Calculada como:
--   DATA_CONFIDENCE_FLOOR + (1 - FLOOR) × ok_ratio, donde ok_ratio es
--   la fracción de flow scores con confidence='ok' entre los activos que
--   alimentan el clasificador de régimen. Rango [0.35, 1.0].
--   Factor 1.0 = todos los scores tienen datos suficientes.
--   Factor 0.35 = todos en cold start (datos insuficientes).
--
-- Ambas columnas son NULLABLE para compatibilidad con registros previos
-- a esta migración que no tienen los componentes calculados.

ALTER TABLE regimes
  ADD COLUMN IF NOT EXISTS structural_confidence FLOAT
    CHECK (structural_confidence IS NULL OR structural_confidence BETWEEN 0 AND 1),
  ADD COLUMN IF NOT EXISTS data_confidence_factor FLOAT
    CHECK (data_confidence_factor IS NULL OR data_confidence_factor BETWEEN 0 AND 1);
