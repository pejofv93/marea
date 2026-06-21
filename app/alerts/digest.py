"""
Mensaje-resumen por ciclo en Telegram (Sesión 11).

A diferencia de las ALERTAS POR EVENTO (que solo llegan con confianza alta,
anti-duplicado e histéresis), el resumen se envía SIEMPRE en cada ciclo. Cumple
dos funciones:

  1. Foto del mercado en ese momento (apertura / media sesión / cierre).
  2. Señal de vida: si llega, MAREA corre; si un día no llega, algo falló.

Es ADICIONAL a las alertas, no las sustituye, y NO usa su anti-duplicado (es
intencional que llegue aunque el contenido se parezca al del ciclo anterior).

Honestidad sobre la confianza (coherente con narrativa y dashboard):
  * Si los datos están en cold start / baja confianza, el mensaje lo dice
    explícitamente con la coletilla de "datos preliminares".
  * Cuando los datos maduran (confianza ok), la coletilla desaparece sola.

Composición (build_*_digest): funciones puras que reciben el estado ya
calculado → fáciles de testear con datos sintéticos.
Envío (send_*_digest): leen el estado de la BD / memoria, componen y envían
reutilizando el cliente Telegram existente. Nunca lanzan: un fallo de envío se
loguea y se continúa (no tumba el ciclo).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("marea.alerts.digest")

_DISCLAIMER = "⚠️ Interpretación automática · no es consejo de inversión."
_COLD_NOTICE = "⚠️ <i>Datos preliminares (histórico insuficiente, baja confianza)</i>"
_COLD_CONF_THRESHOLD = 0.4   # confianza de régimen por debajo de esto → preliminar

_DXY_TICKER = "DX-Y.NYB"
_VIX_TICKER = "^VIX"

# Traducción de nombres internos a lenguaje claro (es)
_REGIME_ES = {
    "risk_on":          "Risk-ON (apetito por riesgo)",
    "risk_off":         "Risk-OFF (aversión al riesgo)",
    "flight_to_safety": "Huida a refugio (oro/bonos)",
    "sector_rotation":  "Rotación sectorial",
    "neutral":          "Neutral (señales débiles o mixtas)",
}

_SIGNAL_ES = {
    "crypto_inflow":            "entrada a crypto",
    "equity_inflow":            "entrada a acciones",
    "gold_inflow":              "entrada a oro",
    "bonds_inflow":             "entrada a bonos",
    "crypto_outflow":           "salida de crypto",
    "equity_outflow":           "salida de acciones",
    "dxy_falling":              "dólar debilitándose",
    "dxy_rising":               "dólar fortaleciéndose",
    "vix_calm":                 "volatilidad baja",
    "vix_fearful":              "volatilidad alta (miedo)",
    "sector_rotation_detected": "rotación sectorial",
}


# ══════════════════════════════════════════════════════════════════════════════
# Composición — funciones PURAS (reciben estado, devuelven texto)
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_digest(
    snapshot: dict,
    narrative: str | None = None,
    now_label: str = "Cierre de mercado",
) -> str:
    """
    Compone el resumen del ciclo DIARIO a partir del snapshot
    (el mismo que alimenta la narrativa: régimen, top inflow/outflow, rotaciones,
    cold_start) y, opcionalmente, una línea de la narrativa más reciente.
    """
    snapshot = snapshot or {}
    regime = snapshot.get("regime")
    conf = float(regime["confidence"]) if regime else 0.0
    preliminary = (
        bool(snapshot.get("cold_start"))
        or regime is None
        or conf < _COLD_CONF_THRESHOLD
    )

    lines = [f"📊 <b>MAREA — {now_label}</b>"]
    if preliminary:
        lines.append(_COLD_NOTICE)

    # Régimen + señales en lenguaje claro
    if regime:
        name = _REGIME_ES.get(regime.get("name", ""), regime.get("name", "?"))
        lines.append(f"\n<b>Régimen:</b> {name} · confianza {conf:.0%}")
        signals = regime.get("signals") or []
        if signals:
            sig_es = ", ".join(_SIGNAL_ES.get(s, s) for s in signals)
            lines.append(f"Señales: {sig_es}")
    else:
        lines.append("\n<b>Régimen:</b> sin determinar (datos insuficientes)")

    # Top 3 inflows / outflows por activo
    inflow = snapshot.get("top_inflow") or []
    outflow = snapshot.get("top_outflow") or []
    if inflow:
        lines.append("\n<b>Top entradas:</b>")
        lines.extend(f"  ▲ {a.get('ticker', '?')} {a.get('score', 0.0):+.2f}" for a in inflow)
    if outflow:
        lines.append("<b>Top salidas:</b>")
        lines.extend(f"  ▼ {a.get('ticker', '?')} {a.get('score', 0.0):+.2f}" for a in outflow)

    # Rotación sectorial destacada (la más fuerte)
    rotations = snapshot.get("rotations") or []
    if rotations:
        r = rotations[0]
        lines.append(
            f"\n<b>Rotación:</b> {r.get('from', '?')} → {r.get('to', '?')} "
            f"(fuerza {r.get('strength', 0.0):.2f})"
        )

    # Una línea de la narrativa más reciente
    if narrative:
        snippet = narrative.strip().splitlines()[0].strip() if narrative.strip() else ""
        if snippet:
            if len(snippet) > 220:
                snippet = snippet[:217] + "…"
            lines.append(f"\n<i>{snippet}</i>")

    lines.append(f"\n{_DISCLAIMER}")
    return "\n".join(lines)


def build_intraday_digest(
    analysis: dict,
    context: dict | None = None,
    moment: str = "Sesión USA",
) -> str:
    """
    Compone el resumen del ciclo INTRADÍA a partir del resultado del análisis
    intradía (movimientos / strong_inflow / strong_outflow) y el contexto
    DXY/VIX. ``moment`` describe el momento del día (apertura/media/tarde).
    """
    analysis = analysis or {}
    movements = analysis.get("movements") or []
    strong_in = analysis.get("strong_inflow") or []
    strong_out = analysis.get("strong_outflow") or []

    # Preliminar si no hay ningún movimiento con confianza 'ok'
    has_ok = any(m.get("confidence") == "ok" for m in movements)
    preliminary = not has_ok

    lines = [f"📡 <b>MAREA — {moment} (intradía)</b>"]
    if preliminary:
        lines.append(_COLD_NOTICE)

    if strong_in or strong_out:
        if strong_in:
            lines.append(f"\n▲ <b>Entradas fuertes:</b> {', '.join(strong_in)}")
        if strong_out:
            lines.append(f"▼ <b>Salidas fuertes:</b> {', '.join(strong_out)}")
    else:
        lines.append("\nSin movimientos intradía fuertes.")

    # Contexto DXY / VIX (1 línea)
    ctx = context or {}
    dxy, vix = ctx.get("dxy"), ctx.get("vix")
    if dxy is not None or vix is not None:
        dxy_s = f"{dxy:+.2f}" if dxy is not None else "n/d"
        vix_s = f"{vix:+.2f}" if vix is not None else "n/d"
        lines.append(f"\nContexto: DXY {dxy_s} · VIX {vix_s} (score intradía)")

    lines.append(f"\n{_DISCLAIMER}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Envío — leen estado, componen y envían (nunca lanzan)
# ══════════════════════════════════════════════════════════════════════════════

def _digest_enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "digest_enabled", True))


def _resolve_db(db):
    if db is not None:
        return db
    from app.db import get_db
    return get_db()


def _send(text: str, send_fn=None) -> bool:
    """Envía vía Telegram reutilizando el cliente existente. send_fn inyectable en tests."""
    if send_fn is not None:
        return send_fn(text)
    from app.alerts.telegram import send_message
    from app.config import settings
    return send_message(
        text,
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )


def send_daily_digest(db=None, now_label: str = "Cierre de mercado", send_fn=None) -> dict:
    """
    Compone y envía el resumen del ciclo diario. Devuelve un dict de resultado
    con ``ok=True`` SIEMPRE (un fallo de envío no debe tumbar el ciclo): los
    problemas se registran en ``errors`` (errores blandos).
    """
    result = {"kind": "daily", "enabled": _digest_enabled(), "sent": False, "errors": []}
    if not result["enabled"]:
        logger.info("DIGEST_ENABLED=false — resumen diario omitido")
        result["ok"] = True
        return result

    try:
        from app.narrative.snapshot import build_snapshot

        rdb = _resolve_db(db)
        snapshot = build_snapshot(rdb)
        narrative = _latest_narrative(rdb)
        text = build_daily_digest(snapshot, narrative=narrative, now_label=now_label)
        ok = _send(text, send_fn)
        result["sent"] = bool(ok)
        if not ok:
            result["errors"].append("telegram_send_failed")
            logger.warning("Resumen diario: Telegram no aceptó el mensaje")
        else:
            logger.info("Resumen diario enviado a Telegram")
    except Exception as e:  # noqa: BLE001 — nunca tumbar el ciclo por el resumen
        logger.error("Resumen diario falló al componer/enviar: %s", e)
        result["errors"].append(str(e))

    result["ok"] = True
    return result


def send_intraday_digest(db=None, analysis: dict | None = None, hour_utc: int | None = None, send_fn=None) -> dict:
    """
    Compone y envía el resumen del ciclo intradía. ``analysis`` es el resultado
    en memoria del análisis intradía recién ejecutado. Igual que el diario:
    ``ok=True`` siempre; los fallos se registran como errores blandos.
    """
    result = {"kind": "intraday", "enabled": _digest_enabled(), "sent": False, "errors": []}
    if not result["enabled"]:
        logger.info("DIGEST_ENABLED=false — resumen intradía omitido")
        result["ok"] = True
        return result

    try:
        rdb = _resolve_db(db)
        moment = _intraday_moment(hour_utc)
        context = _intraday_context(rdb)
        text = build_intraday_digest(analysis or {}, context=context, moment=moment)
        ok = _send(text, send_fn)
        result["sent"] = bool(ok)
        if not ok:
            result["errors"].append("telegram_send_failed")
            logger.warning("Resumen intradía: Telegram no aceptó el mensaje")
        else:
            logger.info("Resumen intradía enviado a Telegram")
    except Exception as e:  # noqa: BLE001
        logger.error("Resumen intradía falló al componer/enviar: %s", e)
        result["errors"].append(str(e))

    result["ok"] = True
    return result


# ── Helpers de lectura ────────────────────────────────────────────────────────

def _latest_narrative(db) -> str | None:
    try:
        resp = (
            db.table("narratives")
            .select("text")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("text") if rows else None
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer narrativa para el resumen: %s", e)
        return None


def _intraday_moment(hour_utc: int | None = None) -> str:
    """
    Deriva el momento del día desde la hora UTC del ciclo.
    Cron (verano): 13:30→apertura, 16:00→media sesión, 18:00→tarde.
    (En invierno serían 12/15/17 UTC, que caen en los mismos tramos.)
    """
    if hour_utc is None:
        from datetime import datetime, timezone
        hour_utc = datetime.now(timezone.utc).hour
    if hour_utc <= 14:
        return "Apertura USA"
    if hour_utc <= 16:
        return "Media sesión USA"
    return "Tarde USA"


def _intraday_context(db) -> dict:
    """Lee el último flow score intradía (4h) de DXY y VIX como contexto macro."""
    ctx: dict = {"dxy": None, "vix": None}
    try:
        from app.config import settings

        resp = (
            db.table("flow_scores_intraday")
            .select("score,ts,interval,win,assets(ticker)")
            .eq("win", "4h")
            .eq("interval", settings.intraday_interval)
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
        for row in (resp.data or []):
            tk = (row.get("assets") or {}).get("ticker")
            if tk == _DXY_TICKER and ctx["dxy"] is None:
                ctx["dxy"] = round(float(row.get("score") or 0.0), 3)
            elif tk == _VIX_TICKER and ctx["vix"] is None:
                ctx["vix"] = round(float(row.get("score") or 0.0), 3)
            if ctx["dxy"] is not None and ctx["vix"] is not None:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer contexto DXY/VIX intradía: %s", e)
    return ctx
