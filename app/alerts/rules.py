"""
Reglas de disparo para alertas MAREA (Sesión 8).

Cuatro tipos con umbrales configurables:
  1. flow_extreme   — |score| > FLOW_EXTREME_THRESHOLD en cualquier asset.
  2. regime_change  — régimen actual distinto del último avisado.
  3. decoupling     — is_decoupling=True detectado en S5.
  4. exposure       — exposición indirecta nueva (S6), con confianza + fuentes.

Devuelve listas de PotentialAlert; no toca la tabla alerts ni envía nada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

# Termómetros de sentimiento / macro informativo (CRYPTO_FNG, ^VIX): NO son
# flujos de liquidez, así que no deben disparar alertas de inflow/outflow.
# Reutilizamos la MISMA lista que ya excluye el digest (fuente única de verdad).
from app.alerts.digest import SENTIMENT_TICKERS

logger = logging.getLogger("marea.alerts.rules")


@dataclass
class PotentialAlert:
    alert_type: str
    entity: str
    state: str
    payload: dict
    confidence: float


# ── Regla 1: flow score extremo ───────────────────────────────────────────────

def check_flow_extreme(db, threshold: float = 0.7) -> list[PotentialAlert]:
    """
    Devuelve un PotentialAlert por cada asset cuyo score 7d supera ±threshold.
    confidence viene del campo `confidence` de flow_scores ('normal'|'low').
    """
    try:
        resp = (
            db.table("flow_scores")
            .select(
                "asset_id,ts,win,score,raw_zscore,proxy_used,n_obs,confidence,"
                "assets(ticker,asset_class,sector)"
            )
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("check_flow_extreme: error consultando flow_scores: %s", e)
        return []

    # Deduplicar: conservar el score más reciente por asset_id
    seen: dict[int, dict] = {}
    for row in rows:
        aid = row.get("asset_id")
        if aid is not None and aid not in seen:
            seen[aid] = row

    alerts = []
    for row in seen.values():
        score = float(row.get("score") or 0.0)
        if abs(score) <= threshold:
            continue
        assets_info = row.get("assets") or {}
        ticker = assets_info.get("ticker") or str(row.get("asset_id", "?"))
        if ticker in SENTIMENT_TICKERS:
            continue  # termómetro de sentimiento, no flujo: no dispara alerta
        conf_str = row.get("confidence") or "normal"
        # La confidence numérica para el filtro de envío: low=0.2, normal=0.8
        conf_num = 0.2 if conf_str == "low" else 0.8
        alerts.append(PotentialAlert(
            alert_type="flow_extreme",
            entity=ticker,
            state="extreme",
            payload={
                "ticker":       ticker,
                "score":        score,
                "raw_zscore":   float(row.get("raw_zscore") or 0.0),
                "win":          row.get("win", "7d"),
                "confidence":   conf_str,
                "asset_class":  assets_info.get("asset_class", ""),
                "sector":       assets_info.get("sector"),
                "threshold":    threshold,
            },
            confidence=conf_num,
        ))
    return alerts


def get_current_extreme_tickers(db, threshold: float = 0.7) -> set[str]:
    """Devuelve el conjunto de tickers cuyo |score 7d| supera el umbral (para re-arm)."""
    try:
        resp = (
            db.table("flow_scores")
            .select("asset_id,score,assets(ticker)")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("get_current_extreme_tickers: %s", e)
        return set()

    seen: dict[int, dict] = {}
    for row in rows:
        aid = row.get("asset_id")
        if aid is not None and aid not in seen:
            seen[aid] = row

    result = set()
    for row in seen.values():
        score = float(row.get("score") or 0.0)
        if abs(score) > threshold:
            assets_info = row.get("assets") or {}
            ticker = assets_info.get("ticker")
            if ticker and ticker not in SENTIMENT_TICKERS:
                result.add(ticker)
    return result


# ── Regla 2: cambio de régimen ────────────────────────────────────────────────

def check_regime_change(db, last_sent_regime: str | None) -> list[PotentialAlert]:
    """
    Compara el régimen 7d actual con `last_sent_regime` (el último avisado).
    Devuelve alerta solo si son distintos.
    Si no hay datos de régimen disponibles, devuelve lista vacía.
    """
    try:
        resp = (
            db.table("regimes")
            .select("ts,win,regime,confidence,signals")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("check_regime_change: error consultando regimes: %s", e)
        return []

    if not rows:
        return []

    current = rows[0]
    curr_regime = current.get("regime", "neutral")

    if curr_regime == last_sent_regime:
        return []

    # Busca la narrativa más reciente para el resumen de 1 línea
    narrative_summary = _get_latest_narrative_summary(db)

    conf = float(current.get("confidence") or 0.0)
    signals = current.get("signals") or []
    return [PotentialAlert(
        alert_type="regime_change",
        entity="market",
        state=curr_regime,
        payload={
            "prev_regime":        last_sent_regime or "desconocido",
            "curr_regime":        curr_regime,
            "curr_confidence":    conf,
            "signals":            signals if isinstance(signals, list) else [],
            "win":                current.get("win", "7d"),
            "ts":                 current.get("ts", ""),
            "narrative_summary":  narrative_summary,
        },
        confidence=conf,
    )]


def get_current_regime(db) -> str | None:
    """Devuelve el régimen 7d actual o None si no hay datos."""
    try:
        resp = (
            db.table("regimes")
            .select("regime")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("regime") if rows else None
    except Exception as e:
        logger.error("get_current_regime: %s", e)
        return None


def _get_latest_narrative_summary(db) -> str:
    """Primera línea de la narrativa más reciente, o cadena vacía."""
    try:
        resp = (
            db.table("narratives")
            .select("text")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return ""
        text = (rows[0].get("text") or "").strip()
        return text.split("\n")[0][:160] if text else ""
    except Exception:
        return ""


# ── Regla 3: desacople anómalo ────────────────────────────────────────────────

def check_decoupling(db) -> list[PotentialAlert]:
    """
    Devuelve un PotentialAlert por cada par marcado como is_decoupling=True
    en la última pasada de correlaciones (ventana más reciente disponible).
    """
    try:
        resp = (
            db.table("correlations")
            .select("ts,win,matrix_type,pair_a,pair_b,corr,is_decoupling")
            .eq("is_decoupling", True)
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("check_decoupling: error consultando correlations: %s", e)
        return []

    alerts = []
    for row in rows:
        pair_a = row.get("pair_a", "?")
        pair_b = row.get("pair_b", "?")
        entity = f"{pair_a}/{pair_b}"
        alerts.append(PotentialAlert(
            alert_type="decoupling",
            entity=entity,
            state="decoupled",
            payload={
                "pair_a":      pair_a,
                "pair_b":      pair_b,
                "corr":        float(row.get("corr") or 0.0),
                "matrix_type": row.get("matrix_type", "intermarket"),
                "win":         row.get("win", "7d"),
                "ts":          row.get("ts", ""),
            },
            confidence=0.7,  # la detección de desacople ya pasó el filtro de S5
        ))
    return alerts


# ── Regla 5: flujo intradía extremo ──────────────────────────────────────────

def check_intraday_flow(
    db,
    threshold: float = 0.6,
    interval: str = "60m",
) -> list[PotentialAlert]:
    """
    Devuelve un PotentialAlert por cada asset cuyo score intradía 4h
    supera ±threshold. Usa flow_scores_intraday (carril intradía).
    Estado siempre 'intraday_extreme'; re-arm gestionado por dedup.
    """
    try:
        resp = (
            db.table("flow_scores_intraday")
            .select(
                "asset_id,ts,interval,win,score,n_obs,confidence,"
                "assets(ticker,asset_class,sector)"
            )
            .eq("win", "4h")
            .eq("interval", interval)
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("check_intraday_flow: error consultando flow_scores_intraday: %s", e)
        return []

    # Conservar el score más reciente por asset_id
    seen: dict[int, dict] = {}
    for row in rows:
        aid = row.get("asset_id")
        if aid is not None and aid not in seen:
            seen[aid] = row

    alerts = []
    for row in seen.values():
        score = float(row.get("score") or 0.0)
        if abs(score) <= threshold:
            continue
        assets_info = row.get("assets") or {}
        ticker   = assets_info.get("ticker") or str(row.get("asset_id", "?"))
        if ticker in SENTIMENT_TICKERS:
            continue  # termómetro de sentimiento, no flujo: no dispara alerta
        conf_str = row.get("confidence") or "low"
        conf_num = 0.2 if conf_str == "low" else 0.8
        direction = "inflow" if score > 0 else "outflow"
        alerts.append(PotentialAlert(
            alert_type="intraday_flow",
            entity=ticker,
            state="intraday_extreme",
            payload={
                "ticker":      ticker,
                "score":       score,
                "win":         "4h",
                "interval":    row.get("interval", interval),
                "direction":   direction,
                "confidence":  conf_str,
                "asset_class": assets_info.get("asset_class", ""),
                "sector":      assets_info.get("sector"),
                "threshold":   threshold,
                "ts":          row.get("ts", ""),
            },
            confidence=conf_num,
        ))
    return alerts


def get_current_intraday_extreme_tickers(
    db,
    threshold: float = 0.6,
    interval: str = "60m",
) -> set[str]:
    """Tickers cuyo |score intradía 4h| supera el umbral (para re-arm)."""
    try:
        resp = (
            db.table("flow_scores_intraday")
            .select("asset_id,score,assets(ticker)")
            .eq("win", "4h")
            .eq("interval", interval)
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("get_current_intraday_extreme_tickers: %s", e)
        return set()

    seen: dict[int, dict] = {}
    for row in rows:
        aid = row.get("asset_id")
        if aid is not None and aid not in seen:
            seen[aid] = row

    result = set()
    for row in seen.values():
        score = float(row.get("score") or 0.0)
        if abs(score) > threshold:
            assets_info = row.get("assets") or {}
            ticker = assets_info.get("ticker")
            if ticker and ticker not in SENTIMENT_TICKERS:
                result.add(ticker)
    return result


# ── Regla 4: exposición indirecta nueva ──────────────────────────────────────

def check_exposure(db) -> list[PotentialAlert]:
    """
    Devuelve un PotentialAlert por cada exposición cuya combinación
    (source_entity, exposed_ticker, confidence) no haya sido avisada aún.
    El state = nivel de confianza (permite re-alertar si el nivel mejora).
    La confidence numérica se asigna por nivel: oficial=0.9, prensa=0.5, especulacion=0.3.
    """
    try:
        resp = (
            db.table("exposures")
            .select(
                "source_entity,exposed_ticker,exposure_type,relationship,"
                "confidence,sources,llm_engine"
            )
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("check_exposure: error consultando exposures: %s", e)
        return []

    _conf_num = {
        "confirmado_oficial": 0.9,
        "rumor_prensa":       0.5,
        "especulacion":       0.3,
    }

    alerts = []
    for row in rows:
        source = row.get("source_entity", "?")
        ticker = row.get("exposed_ticker", "?")
        conf   = row.get("confidence", "especulacion")
        entity = f"{source}→{ticker}"
        alerts.append(PotentialAlert(
            alert_type="exposure",
            entity=entity,
            state=conf,
            payload={
                "source_entity":  source,
                "exposed_ticker": ticker,
                "exposure_type":  row.get("exposure_type", ""),
                "confidence":     conf,
                "relationship":   row.get("relationship", ""),
                "sources":        row.get("sources") or [],
                "llm_engine":     row.get("llm_engine", ""),
            },
            confidence=_conf_num.get(conf, 0.3),
        ))
    return alerts
