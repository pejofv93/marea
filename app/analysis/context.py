"""
Evaluación de los INDICADORES DE CONTEXTO de régimen (Bloque 1).

La ingesta (app/ingest/context_runner.py) solo guarda el valor del día en
context_indicators. ESTA capa lee la serie de cada indicador y decide:

  · AUTO-ACTIVACIÓN: un indicador solo se considera "encendido" (active=True)
    cuando acumula al menos `min_obs` observaciones propias. Por debajo de ese
    umbral NO modula el régimen y se presenta como "(preliminar)" o se omite.
    Así cada indicador se enciende solo a medida que MAREA acumula histórico,
    sin intervención manual (igual filosofía que el score_min_obs del scoring).

  · DIRECCIÓN: comparando el último valor con la media del histórico previo,
    clasifica el movimiento (subiendo/bajando/plano) con umbrales por indicador.

Con eso produce dos salidas:
  · regime_modulators(): {regime: [labels]} para classify_regime. Son MODULADORES
    de confianza, NO disparadores: solo refuerzan un régimen que YA encajó por
    flujos (no pueden crear un régimen por sí solos).
  · digest_lines(): líneas legibles para el bloque "Contexto macro" del parte,
    omitiendo lo que aún no tiene dato y marcando lo preliminar.

Nunca lanza hacia fuera: load_context_states captura los errores de BD y
devuelve {} (degradación elegante: si el contexto falta, el resto sigue igual).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.ingest.context_runner import (
    INDICATOR_BTC_DOMINANCE,
    INDICATOR_CREDIT_SPREAD,
    INDICATOR_YIELD_CURVE,
)

logger = logging.getLogger("marea.analysis.context")

# ── Umbrales de dirección (documentados) ──────────────────────────────────────
# Por debajo de estos cambios, el movimiento se considera "plano" (sin señal).
_DOM_EPS_PP = 0.3        # dominancia BTC: 0.3 puntos porcentuales de cambio
_CREDIT_EPS_REL = 0.003  # credit spread: 0.3% relativo sobre la media previa
_CURVE_EPS_PP = 0.05     # curva 10Y-2Y: 0.05 puntos porcentuales de cambio

_LOOKBACK_ROWS = 500     # filas a leer (≈ 160 días × 3 indicadores)


@dataclass
class IndicatorState:
    indicator: str
    active: bool                       # n_obs >= min_obs → modula y se presenta sólido
    n_obs: int
    level: Optional[float]             # último valor
    trend: Optional[float] = None      # último − media(histórico previo)
    direction: str = "unknown"         # 'rising' | 'falling' | 'flat' | 'unknown'
    extra: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# Evaluación PURA (testeable sin BD)
# ══════════════════════════════════════════════════════════════════════════════

def _direction(indicator: str, trend: float, mean_prior: float) -> str:
    if indicator == INDICATOR_BTC_DOMINANCE:
        if trend > _DOM_EPS_PP:
            return "rising"
        if trend < -_DOM_EPS_PP:
            return "falling"
        return "flat"
    if indicator == INDICATOR_CREDIT_SPREAD:
        rel = trend / mean_prior if mean_prior else 0.0
        if rel > _CREDIT_EPS_REL:
            return "rising"
        if rel < -_CREDIT_EPS_REL:
            return "falling"
        return "flat"
    if indicator == INDICATOR_YIELD_CURVE:
        if trend > _CURVE_EPS_PP:
            return "rising"      # steepening
        if trend < -_CURVE_EPS_PP:
            return "falling"     # flattening
        return "flat"
    return "unknown"


def evaluate_series(indicator: str, series: list[dict], min_obs: int) -> IndicatorState:
    """
    `series`: filas {ts, value, extra} de UN indicador, ascendentes por ts.
    Las filas con value=None se ignoran para la activación y la dirección.
    """
    vals = [s for s in series if s.get("value") is not None]
    n = len(vals)
    if n == 0:
        return IndicatorState(indicator, active=False, n_obs=0, level=None)

    level = float(vals[-1]["value"])
    latest_extra = vals[-1].get("extra") or {}
    active = n >= min_obs

    trend: Optional[float] = None
    direction = "unknown"
    if n >= 2:
        prior = [float(s["value"]) for s in vals[:-1]]
        mean_prior = sum(prior) / len(prior)
        trend = round(level - mean_prior, 6)
        direction = _direction(indicator, trend, mean_prior)

    return IndicatorState(indicator, active, n, round(level, 6), trend, direction, latest_extra)


def regime_modulators(states: dict[str, IndicatorState]) -> dict[str, list[str]]:
    """
    Traduce los estados ACTIVOS a moduladores de confianza por régimen.
    Solo cuentan los indicadores active=True (auto-activación): los preliminares
    no influyen en el régimen.
    """
    mods: dict[str, list[str]] = {"risk_off": [], "risk_on": [], "flight_to_safety": []}

    for st in states.values():
        if not st.active:
            continue

        if st.indicator == INDICATOR_CREDIT_SPREAD:
            if st.direction == "falling":          # HYG cae vs LQD → spreads ensanchándose
                mods["risk_off"].append("credit_spread_widening")
                mods["flight_to_safety"].append("credit_spread_widening")
            elif st.direction == "rising":         # high-yield aguanta → spreads estrechándose
                mods["risk_on"].append("credit_spread_tightening")

        elif st.indicator == INDICATOR_YIELD_CURVE:
            if st.level is not None and st.level < 0:   # curva invertida (nivel, no tendencia)
                mods["risk_off"].append("yield_curve_inverted")
                mods["flight_to_safety"].append("yield_curve_inverted")
            elif st.direction == "falling":             # aplanándose
                mods["risk_off"].append("yield_curve_flattening")
                mods["flight_to_safety"].append("yield_curve_flattening")
            elif st.direction == "rising":              # empinándose
                mods["risk_on"].append("yield_curve_steepening")

        elif st.indicator == INDICATOR_BTC_DOMINANCE:
            if st.direction == "rising":           # rotación a BTC = miedo en alts
                mods["risk_off"].append("btc_dominance_rising")
            elif st.direction == "falling":        # apetito por riesgo en crypto
                mods["risk_on"].append("btc_dominance_falling")

    return {k: v for k, v in mods.items() if v}


# ── Presentación (líneas del bloque "Contexto macro" del parte) ───────────────

_DIR_DOM = {"rising": "subiendo", "falling": "bajando", "flat": "estable"}
_DIR_CURVE = {"rising": "empinándose", "falling": "aplanándose", "flat": "estable"}


def _dom_line(st: IndicatorState) -> str:
    tag = "" if st.active else " <i>(preliminar)</i>"
    move = _DIR_DOM.get(st.direction, "")
    if st.direction == "rising":
        read = " — rotación hacia BTC (cautela en alts)"
    elif st.direction == "falling":
        read = " — apetito por riesgo en crypto"
    else:
        read = ""
    delta = f", {st.trend:+.2f} pp" if st.trend is not None else ""
    return f"  • Dominancia BTC: {st.level:.1f}% ({move}{delta}){read}{tag}"


def _credit_line(st: IndicatorState) -> str:
    tag = "" if st.active else " <i>(preliminar)</i>"
    if st.direction == "falling":
        read = "spreads ensanchándose → tono risk-off"
    elif st.direction == "rising":
        read = "spreads estrechándose → tono risk-on"
    else:
        read = "spreads estables"
    return f"  • Crédito (HYG/LQD): {st.level:.3f} — {read}{tag}"


def _curve_line(st: IndicatorState) -> str:
    tag = "" if st.active else " <i>(preliminar)</i>"
    if st.level is not None and st.level < 0:
        read = "curva INVERTIDA → señal de recesión/risk-off"
    else:
        move = _DIR_CURVE.get(st.direction, "estable")
        read = f"curva normal ({move})"
    return f"  • Curva 10Y-2Y: {st.level:+.2f} pp — {read}{tag}"


_LINE_BUILDERS = {
    INDICATOR_BTC_DOMINANCE: _dom_line,
    INDICATOR_CREDIT_SPREAD: _credit_line,
    INDICATOR_YIELD_CURVE: _curve_line,
}

# Orden estable de presentación
_PRESENT_ORDER = [INDICATOR_CREDIT_SPREAD, INDICATOR_YIELD_CURVE, INDICATOR_BTC_DOMINANCE]


def digest_lines(states: dict[str, IndicatorState]) -> list[str]:
    """
    Líneas legibles del bloque de contexto. Omite indicadores sin dato (level
    None); marca "(preliminar)" los que aún no alcanzan min_obs. Si no hay nada
    presentable, devuelve [] (el digest entonces no añade el bloque).
    """
    lines: list[str] = []
    for ind in _PRESENT_ORDER:
        st = states.get(ind)
        if st is None or st.level is None:
            continue
        lines.append(_LINE_BUILDERS[ind](st))
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Lectura de BD + fachada de alto nivel (best-effort, nunca lanza)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ContextEvaluation:
    states: dict[str, IndicatorState] = field(default_factory=dict)

    @property
    def regime_modulators(self) -> dict[str, list[str]]:
        return regime_modulators(self.states)

    @property
    def digest_lines(self) -> list[str]:
        return digest_lines(self.states)


def load_context_states(db, min_obs: int | None = None) -> dict[str, IndicatorState]:
    """
    Lee context_indicators, agrupa por indicador y evalúa cada serie.
    Ante cualquier error de BD devuelve {} (degradación elegante).
    """
    if min_obs is None:
        from app.config import settings
        min_obs = settings.context_min_obs

    try:
        resp = (
            db.table("context_indicators")
            .select("ts,indicator,value,extra")
            .order("ts", desc=True)
            .limit(_LOOKBACK_ROWS)
            .execute()
        )
        rows = resp.data if isinstance(resp.data, list) else []
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudieron leer context_indicators: %s", e)
        return {}

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        ind = r.get("indicator")
        if ind:
            grouped.setdefault(ind, []).append(r)

    states: dict[str, IndicatorState] = {}
    for ind, series in grouped.items():
        # Vienen desc por ts; evaluate_series espera ascendente.
        series_asc = sorted(series, key=lambda s: s.get("ts") or "")
        states[ind] = evaluate_series(ind, series_asc, min_obs)
    return states


def evaluate_context(db, min_obs: int | None = None) -> ContextEvaluation:
    """Fachada: estados + moduladores + líneas en un solo objeto. Nunca lanza."""
    return ContextEvaluation(states=load_context_states(db, min_obs=min_obs))
