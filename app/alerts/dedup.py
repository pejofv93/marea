"""
Anti-duplicado por cambio de estado para alertas MAREA (Sesión 8).

Lógica central:
- was_sent(db, type, entity, state)           → True si ya enviado
- upsert_alert(db, row)                       → INSERT OR UPDATE via on_conflict
- rearm_flow_scores(db, currently_extreme)    → update sent=False para assets
                                                 que bajaron del umbral
- get_last_sent_regime(db)                    → último régimen enviado (para rules.py)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("marea.alerts.dedup")


def was_sent(db, alert_type: str, entity: str, state: str) -> bool:
    """
    Devuelve True si existe una fila (alert_type, entity, state) con sent=True.
    """
    try:
        resp = (
            db.table("alerts")
            .select("id,sent")
            .eq("alert_type", alert_type)
            .eq("entity", entity)
            .eq("state", state)
            .eq("sent", True)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        logger.error("was_sent(%s, %s, %s): %s", alert_type, entity, state, e)
        # En caso de error de BD, asumimos "no enviado" para no silenciar alertas
        return False


def upsert_alert(db, row: dict) -> None:
    """
    Persiste la alerta en la tabla alerts.
    on_conflict=(alert_type, entity, state) → actualiza si ya existe.
    """
    try:
        db.table("alerts").upsert(
            row,
            on_conflict="alert_type,entity,state",
        ).execute()
    except Exception as e:
        logger.error(
            "upsert_alert(%s, %s, %s): %s",
            row.get("alert_type"), row.get("entity"), row.get("state"), e,
        )


def rearm_flow_scores(db, currently_extreme: set[str]) -> int:
    """
    Histéresis: para cada ticker que tenía una alerta flow_extreme enviada
    (sent=True) pero cuyo score YA NO es extremo, resetea sent=False.
    Devuelve el número de alertas re-armadas.
    """
    try:
        resp = (
            db.table("alerts")
            .select("id,entity")
            .eq("alert_type", "flow_extreme")
            .eq("state", "extreme")
            .eq("sent", True)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("rearm_flow_scores: error leyendo alertas: %s", e)
        return 0

    count = 0
    for row in rows:
        ticker = row.get("entity", "")
        if ticker not in currently_extreme:
            try:
                db.table("alerts").upsert(
                    {
                        "alert_type":      "flow_extreme",
                        "entity":          ticker,
                        "state":           "extreme",
                        "sent":            False,
                        "not_sent_reason": None,
                        "sent_at":         None,
                    },
                    on_conflict="alert_type,entity,state",
                ).execute()
                count += 1
                logger.debug("Re-armada alerta flow_extreme para %s", ticker)
            except Exception as e:
                logger.error("rearm_flow_scores %s: %s", ticker, e)

    if count:
        logger.info("Re-armadas %d alertas flow_extreme (scores bajaron del umbral)", count)
    return count


def rearm_intraday_flow(db, currently_extreme: set[str]) -> int:
    """
    Histéresis para alertas intradía: resetea sent=False para assets que
    tenían una alerta intraday_flow enviada pero cuyo score ya no es extremo.
    Patrón análogo a rearm_flow_scores().
    """
    try:
        resp = (
            db.table("alerts")
            .select("id,entity")
            .eq("alert_type", "intraday_flow")
            .eq("state", "intraday_extreme")
            .eq("sent", True)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("rearm_intraday_flow: error leyendo alertas: %s", e)
        return 0

    count = 0
    for row in rows:
        ticker = row.get("entity", "")
        if ticker not in currently_extreme:
            try:
                db.table("alerts").upsert(
                    {
                        "alert_type":      "intraday_flow",
                        "entity":          ticker,
                        "state":           "intraday_extreme",
                        "sent":            False,
                        "not_sent_reason": None,
                        "sent_at":         None,
                    },
                    on_conflict="alert_type,entity,state",
                ).execute()
                count += 1
                logger.debug("Re-armada alerta intraday_flow para %s", ticker)
            except Exception as e:
                logger.error("rearm_intraday_flow %s: %s", ticker, e)

    if count:
        logger.info("Re-armadas %d alertas intraday_flow (scores bajaron del umbral)", count)
    return count


def get_last_sent_regime(db) -> str | None:
    """
    Devuelve el estado (nombre de régimen) de la última alerta regime_change
    enviada, o None si no se ha enviado ninguna aún.
    """
    try:
        resp = (
            db.table("alerts")
            .select("state")
            .eq("alert_type", "regime_change")
            .eq("entity", "market")
            .eq("sent", True)
            .order("sent_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("state") if rows else None
    except Exception as e:
        logger.error("get_last_sent_regime: %s", e)
        return None


def build_alert_row(
    alert_type: str,
    entity: str,
    state: str,
    payload: dict,
    confidence: float,
    sent: bool,
    not_sent_reason: str | None,
) -> dict:
    """Construye el dict para upsert en la tabla alerts."""
    now = datetime.now(timezone.utc).isoformat()
    row: dict = {
        "alert_type":      alert_type,
        "entity":          entity,
        "state":           state,
        "payload":         payload,
        "confidence":      confidence,
        "sent":            sent,
        "not_sent_reason": not_sent_reason,
        "ts":              now,
    }
    if sent:
        row["sent_at"] = now
    return row
