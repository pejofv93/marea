-- MAREA — Sesión 8: motor de alertas + bot de Telegram
-- Ejecutar DESPUÉS de 001…007.
--
-- Diseño anti-duplicado: UNIQUE(alert_type, entity, state) garantiza una sola
-- fila por transición de estado. Histéresis en flow_extreme: cuando el score
-- baja del umbral, se actualiza sent=false para re-armar la alerta.

CREATE TABLE IF NOT EXISTS alerts (
    id               BIGSERIAL    PRIMARY KEY,
    alert_type       TEXT         NOT NULL CHECK (
                         alert_type IN ('flow_extreme','regime_change','decoupling','exposure')
                     ),
    entity           TEXT         NOT NULL,  -- ticker / 'market' / 'BTC/SPY' / 'OpenAI→MSFT'
    state            TEXT         NOT NULL,  -- 'extreme' | 'risk_off' | 'decoupled' | 'confirmado_oficial'
    payload          JSONB        NOT NULL DEFAULT '{}',
    confidence       FLOAT        NOT NULL DEFAULT 0.0,
    sent             BOOL         NOT NULL DEFAULT FALSE,
    not_sent_reason  TEXT,                   -- 'low_confidence' | 'duplicate' | NULL si enviado
    ts               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sent_at          TIMESTAMPTZ,
    UNIQUE (alert_type, entity, state)
);

CREATE INDEX IF NOT EXISTS idx_alerts_type_entity ON alerts (alert_type, entity, sent);
CREATE INDEX IF NOT EXISTS idx_alerts_ts           ON alerts (ts DESC);
