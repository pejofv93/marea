"""
Cliente Telegram Bot API para alertas MAREA (Sesión 8).

Envía mensajes a TELEGRAM_CHAT_ID vía HTTP directo (sin librería pesada).
Reintentos con backoff en caso de error de red; no lanza excepción al caller.
parse_mode=HTML: evita tener que escapar caracteres especiales de MarkdownV2.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("marea.alerts.telegram")

_API_BASE = "https://api.telegram.org/bot"
_RETRY_DELAYS = [2, 5, 15]


def send_message(text: str, token: str, chat_id: str) -> bool:
    """
    Envía `text` al chat `chat_id` usando `token`. Devuelve True si OK.
    Reintenta hasta 3 veces con backoff; nunca lanza excepción.
    """
    if not token or not chat_id:
        logger.warning("Telegram: token o chat_id vacíos — mensaje no enviado")
        return False

    url = f"{_API_BASE}{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    for attempt, delay in enumerate(_RETRY_DELAYS, 1):
        try:
            resp = httpx.post(url, json=payload, timeout=10.0)
            if resp.status_code == 200:
                logger.info("Telegram: enviado OK (%d chars)", len(text))
                return True
            logger.warning(
                "Telegram: HTTP %s intento %d/%d — %s",
                resp.status_code, attempt, len(_RETRY_DELAYS), resp.text[:200],
            )
            return False
        except Exception as e:
            logger.error(
                "Telegram: error red intento %d/%d: %s", attempt, len(_RETRY_DELAYS), e
            )
            if attempt < len(_RETRY_DELAYS):
                time.sleep(delay)

    logger.error("Telegram: todos los reintentos fallaron")
    return False


# ── Formateo de mensajes ──────────────────────────────────────────────────────

_DISCLAIMER = "⚠️ Interpretación automática · no es consejo de inversión."

_CONFIDENCE_LABELS = {
    "confirmado_oficial": "✅ CONFIRMADO OFICIAL",
    "rumor_prensa":       "📰 RUMOR PRENSA",
    "especulacion":       "🔮 ESPECULACIÓN",
}


def format_flow_extreme(payload: dict) -> str:
    ticker    = payload.get("ticker", "?")
    score     = payload.get("score", 0.0)
    window    = payload.get("win", "7d")
    threshold = payload.get("threshold", 0.7)
    conf      = payload.get("confidence", "normal")
    aclass    = payload.get("asset_class", "")
    direction = "ENTRADA" if score > 0 else "SALIDA"
    return (
        f"⚠️ <b>FLOW EXTREMO — {ticker}</b>\n"
        f"Score: {score:+.3f} (umbral ±{threshold:.2f}) | {direction}\n"
        f"Clase: {aclass} | Ventana: {window} | Confianza datos: {conf.upper()}\n\n"
        f"{_DISCLAIMER}"
    )


def format_regime_change(payload: dict) -> str:
    prev     = payload.get("prev_regime", "desconocido")
    curr     = payload.get("curr_regime", "?")
    conf     = payload.get("curr_confidence", 0.0)
    signals  = payload.get("signals", [])
    summary  = payload.get("narrative_summary", "")
    sig_str  = ", ".join(signals) if signals else "—"
    lines = [
        f"🔄 <b>CAMBIO DE RÉGIMEN</b>",
        f"{prev} → <b>{curr}</b>",
        f"Confianza: {conf:.0%} | Señales: {sig_str}",
    ]
    if summary:
        lines.append(f"\n▶ <i>{summary}</i>")
    lines.append(f"\n{_DISCLAIMER}")
    return "\n".join(lines)


def format_decoupling(payload: dict) -> str:
    pair_a = payload.get("pair_a", "?")
    pair_b = payload.get("pair_b", "?")
    corr   = payload.get("corr", 0.0)
    mtype  = payload.get("matrix_type", "intermarket")
    window = payload.get("win", "7d")
    return (
        f"⚡ <b>DESACOPLE DETECTADO — {pair_a}/{pair_b}</b>\n"
        f"Correlación: {corr:.3f} | Tipo: {mtype} | Ventana: {window}\n\n"
        f"{_DISCLAIMER}"
    )


def format_exposure(payload: dict) -> str:
    entity   = payload.get("source_entity", "?")
    ticker   = payload.get("exposed_ticker", "?")
    etype    = payload.get("exposure_type", "?")
    conf     = payload.get("confidence", "especulacion")
    rel      = payload.get("relationship", "")
    sources  = payload.get("sources", [])

    conf_label = _CONFIDENCE_LABELS.get(conf, conf.upper())
    is_low = conf in ("rumor_prensa", "especulacion")

    src_str = "\n".join(f"• {s}" for s in sources[:3]) if sources else "—"
    lines = [
        f"🔗 <b>EXPOSICIÓN INDIRECTA — {entity} → {ticker}</b>",
        f"Tipo: {etype} | Nivel: {conf_label}",
    ]
    if rel:
        lines.append(f"Relación: {rel}")
    lines.append(f"Fuentes:\n{src_str}")
    if is_low:
        lines.append(
            "\n⚠️ <b>SIN VERIFICAR — hipótesis especulativa.</b> "
            "No ha sido verificada manualmente. No es consejo de inversión."
        )
    else:
        lines.append(f"\n{_DISCLAIMER}")
    return "\n".join(lines)


def format_intraday_flow(payload: dict) -> str:
    ticker    = payload.get("ticker", "?")
    score     = payload.get("score", 0.0)
    direction = payload.get("direction", "outflow" if score < 0 else "inflow").upper()
    interval  = payload.get("interval", "60m")
    threshold = payload.get("threshold", 0.6)
    conf      = payload.get("confidence", "low")
    aclass    = payload.get("asset_class", "")
    return (
        f"📡 <b>SEÑAL INTRADÍA — {ticker}</b>\n"
        f"Score: {score:+.3f} (umbral ±{threshold:.2f}) | {direction}\n"
        f"Ventana: 4h | Interval: {interval} | Clase: {aclass}\n"
        f"Confianza datos: {conf.upper()}\n\n"
        f"⏱ <i>Señal de CORTO PLAZO intradía — no refleja el régimen de fondo.</i>\n"
        f"{_DISCLAIMER}"
    )


_FORMATTERS = {
    "flow_extreme":   format_flow_extreme,
    "regime_change":  format_regime_change,
    "decoupling":     format_decoupling,
    "exposure":       format_exposure,
    "intraday_flow":  format_intraday_flow,
}


def format_alert(alert_type: str, payload: dict) -> str:
    fn = _FORMATTERS.get(alert_type)
    if fn is None:
        return f"Alerta {alert_type}: {payload}"
    return fn(payload)
