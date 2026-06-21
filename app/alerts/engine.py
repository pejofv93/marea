"""
Orquestador del motor de alertas MAREA (Sesión 8).

Flujo por cada ejecución:
  1. Re-arm: resetea sent=False para assets de flow_extreme que bajaron del umbral.
  2. Evalúa las 4 reglas sobre el estado actual de la BD.
  3. Por cada alerta candidata:
     a. Confianza < MIN_ALERT_CONFIDENCE → registra (sent=False, 'low_confidence'), no envía.
     b. Ya enviada (dedup) → registra (sent=False, 'duplicate'), no envía.
     c. Pasa los filtros → formatea, envía a Telegram, registra (sent=True).
  4. Devuelve resumen: evaluadas, enviadas, no enviadas + razón.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("marea.alerts.engine")


@dataclass
class AlertSummary:
    evaluated: int = 0
    sent: int = 0
    not_sent_low_confidence: int = 0
    not_sent_duplicate: int = 0
    rearmed: int = 0
    errors: list[str] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "evaluated":               self.evaluated,
            "sent":                    self.sent,
            "not_sent_low_confidence": self.not_sent_low_confidence,
            "not_sent_duplicate":      self.not_sent_duplicate,
            "rearmed_flow_scores":     self.rearmed,
            "alerts":                  self.alerts,
            "errors":                  self.errors,
            "ok":                      len(self.errors) == 0,
        }


class AlertEngine:
    def __init__(self, db=None, send_fn=None, min_confidence: float | None = None):
        """
        db           — inyectable en tests.
        send_fn      — inyectable en tests (firma: (text, token, chat_id) → bool).
        min_confidence — sobreescribe MIN_ALERT_CONFIDENCE para tests.
        """
        self._db = db
        self._send_fn = send_fn
        self._min_confidence = min_confidence

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def _send(self, text: str) -> bool:
        from app.config import settings
        if self._send_fn is not None:
            return self._send_fn(text)
        from app.alerts.telegram import send_message
        return send_message(text, token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id)

    @property
    def _min_conf(self) -> float:
        if self._min_confidence is not None:
            return self._min_confidence
        from app.config import settings
        return settings.min_alert_confidence

    def run_sync(self) -> dict:
        from app.alerts import dedup, rules
        from app.alerts.telegram import format_alert
        from app.config import settings

        summary = AlertSummary()

        # ── 1. Re-arm histéresis en flow_extreme ─────────────────────────────
        try:
            currently_extreme = rules.get_current_extreme_tickers(
                self.db, threshold=settings.flow_extreme_threshold
            )
            summary.rearmed = dedup.rearm_flow_scores(self.db, currently_extreme)
        except Exception as e:
            msg = f"rearm_flow_scores: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        # ── 1b. Re-arm histéresis en intraday_flow ────────────────────────────
        try:
            currently_intraday = rules.get_current_intraday_extreme_tickers(
                self.db,
                threshold=settings.intraday_flow_threshold,
                interval=settings.intraday_interval,
            )
            summary.rearmed += dedup.rearm_intraday_flow(self.db, currently_intraday)
        except Exception as e:
            msg = f"rearm_intraday_flow: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        # ── 2. Evalúa las 4 reglas ────────────────────────────────────────────
        candidates = []
        try:
            candidates.extend(
                rules.check_flow_extreme(self.db, threshold=settings.flow_extreme_threshold)
            )
        except Exception as e:
            msg = f"check_flow_extreme: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        try:
            last_regime = dedup.get_last_sent_regime(self.db)
            candidates.extend(rules.check_regime_change(self.db, last_regime))
        except Exception as e:
            msg = f"check_regime_change: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        try:
            candidates.extend(rules.check_decoupling(self.db))
        except Exception as e:
            msg = f"check_decoupling: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        try:
            candidates.extend(rules.check_exposure(self.db))
        except Exception as e:
            msg = f"check_exposure: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        try:
            candidates.extend(
                rules.check_intraday_flow(
                    self.db,
                    threshold=settings.intraday_flow_threshold,
                    interval=settings.intraday_interval,
                )
            )
        except Exception as e:
            msg = f"check_intraday_flow: {e}"
            logger.error(msg)
            summary.errors.append(msg)

        summary.evaluated = len(candidates)

        # ── 3. Filtra, envía y registra ───────────────────────────────────────
        for alert in candidates:
            sent        = False
            reason: str | None = None

            # a. Filtro de confianza
            if alert.confidence < self._min_conf:
                reason = "low_confidence"
                summary.not_sent_low_confidence += 1
                logger.debug(
                    "Alerta %s/%s suprimida: confianza %.2f < %.2f",
                    alert.alert_type, alert.entity, alert.confidence, self._min_conf,
                )
            # b. Filtro anti-duplicado
            elif dedup.was_sent(self.db, alert.alert_type, alert.entity, alert.state):
                reason = "duplicate"
                summary.not_sent_duplicate += 1
                logger.debug(
                    "Alerta %s/%s/%s suprimida: ya enviada",
                    alert.alert_type, alert.entity, alert.state,
                )
            # c. Enviar
            else:
                try:
                    text = format_alert(alert.alert_type, alert.payload)
                    ok = self._send(text)
                    if ok:
                        sent = True
                        summary.sent += 1
                        logger.info(
                            "Alerta enviada: %s / %s / %s",
                            alert.alert_type, alert.entity, alert.state,
                        )
                    else:
                        reason = "send_failed"
                        logger.warning(
                            "Alerta %s/%s/%s: Telegram rechazó el mensaje",
                            alert.alert_type, alert.entity, alert.state,
                        )
                except Exception as e:
                    msg = f"send {alert.alert_type}/{alert.entity}: {e}"
                    logger.error(msg)
                    summary.errors.append(msg)
                    reason = "send_error"

            # Registra en alerts (siempre, con motivo)
            row = dedup.build_alert_row(
                alert_type=alert.alert_type,
                entity=alert.entity,
                state=alert.state,
                payload=alert.payload,
                confidence=alert.confidence,
                sent=sent,
                not_sent_reason=reason,
            )
            dedup.upsert_alert(self.db, row)

            summary.alerts.append({
                "alert_type":      alert.alert_type,
                "entity":          alert.entity,
                "state":           alert.state,
                "confidence":      alert.confidence,
                "sent":            sent,
                "not_sent_reason": reason,
            })

        logger.info(
            "AlertEngine: evaluadas=%d enviadas=%d low_conf=%d dup=%d errores=%d",
            summary.evaluated, summary.sent,
            summary.not_sent_low_confidence, summary.not_sent_duplicate,
            len(summary.errors),
        )
        return summary.to_dict()
