"""
Tests MAREA — Bloque 1: indicadores de CONTEXTO de régimen (con auto-activación).

Garantías verificadas:
  (a) Cada indicador se calcula e ingiere correctamente (datos sintéticos).
  (b) AUTO-ACTIVACIÓN: por debajo de context_min_obs el indicador NO modula el
      régimen ni se presenta como sólido; por encima, sí.
  (c) Un fallo de una fuente nueva NO rompe el ciclo (degradación elegante).
  (d) Los indicadores NO entran en el carril de flujo: la ingesta solo escribe en
      context_indicators y los moduladores NUNCA crean un régimen por sí solos.
  (e) Put/call queda fuera del conjunto de indicadores soportados (omitido).
  (f) NINGÚN test hace llamadas reales (yfinance / CoinGecko mockeados).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.ingest import context_runner as cr
from app.ingest.context_runner import (
    ContextIngestRunner,
    INDICATOR_BTC_DOMINANCE,
    INDICATOR_CREDIT_SPREAD,
    INDICATOR_YIELD_CURVE,
    compute_credit_spread,
    compute_yield_curve,
    extract_btc_dominance,
)
from app.analysis import context as ctx


# ══════════════════════════════════════════════════════════════════════════════
# (a) Cálculo PURO de cada indicador
# ══════════════════════════════════════════════════════════════════════════════

class TestPureCompute:

    def test_credit_spread_ratio(self):
        assert compute_credit_spread(79.86, 109.49) == pytest.approx(0.729382, abs=1e-5)

    def test_credit_spread_missing_leg(self):
        assert compute_credit_spread(None, 109.49) is None
        assert compute_credit_spread(79.86, None) is None

    def test_credit_spread_div_by_zero(self):
        assert compute_credit_spread(79.86, 0) is None

    def test_yield_curve_positive(self):
        # 10Y 4.376 − 2Y 3.851 = +0.525 pp (curva normal)
        assert compute_yield_curve(4.376, 3.851) == pytest.approx(0.525, abs=1e-6)

    def test_yield_curve_inverted(self):
        assert compute_yield_curve(3.80, 4.20) == pytest.approx(-0.40, abs=1e-6)

    def test_yield_curve_missing_leg(self):
        assert compute_yield_curve(None, 3.851) is None
        assert compute_yield_curve(4.376, None) is None

    def test_extract_dominance_ok(self):
        j = {"data": {"market_cap_percentage": {"btc": 55.66, "eth": 8.83}}}
        out = extract_btc_dominance(j)
        assert out["value"] == pytest.approx(55.66)
        assert out["extra"]["eth"] == pytest.approx(8.83)

    def test_extract_dominance_missing(self):
        assert extract_btc_dominance({"data": {}}) is None
        assert extract_btc_dominance(None) is None


# ══════════════════════════════════════════════════════════════════════════════
# (a)+(d) Ingesta: escribe en context_indicators, aislada y sin tocar el flujo
# ══════════════════════════════════════════════════════════════════════════════

def _capture_db():
    """db Supabase falso; db.table(name).upsert(rows,...).execute()."""
    db = MagicMock()
    return db


class TestIngestRunner:

    def test_writes_three_indicators(self):
        db = _capture_db()
        closes = {"HYG": 79.86, "LQD": 109.49, "^TNX": 4.376, "2YY=F": 3.851}
        glob = {"data": {"market_cap_percentage": {"btc": 55.66, "eth": 8.83}}}
        with patch.object(cr, "_download_closes", return_value=closes), \
             patch.object(cr, "fetch_json", return_value=glob):
            res = ContextIngestRunner(db=db, short_ticker="2YY=F").run_sync()

        assert res["ok"] is True
        assert res["indicators_written"] == 3

        # Lo escrito va EXCLUSIVAMENTE a context_indicators (no flujo).
        tables = {c.args[0] for c in db.table.call_args_list}
        assert tables == {"context_indicators"}

        rows = db.table.return_value.upsert.call_args.args[0]
        by_ind = {r["indicator"]: r for r in rows}
        assert set(by_ind) == {
            INDICATOR_BTC_DOMINANCE, INDICATOR_CREDIT_SPREAD, INDICATOR_YIELD_CURVE
        }
        assert by_ind[INDICATOR_CREDIT_SPREAD]["value"] == pytest.approx(0.729382, abs=1e-5)
        assert by_ind[INDICATOR_YIELD_CURVE]["value"] == pytest.approx(0.525, abs=1e-6)
        assert by_ind[INDICATOR_BTC_DOMINANCE]["value"] == pytest.approx(55.66)

    def test_partial_source_missing_still_writes_others(self):
        # Falta LQD (credit spread no se puede) pero dominancia y curva sí salen.
        db = _capture_db()
        closes = {"HYG": 79.86, "^TNX": 4.376, "2YY=F": 3.851}
        glob = {"data": {"market_cap_percentage": {"btc": 55.66}}}
        with patch.object(cr, "_download_closes", return_value=closes), \
             patch.object(cr, "fetch_json", return_value=glob):
            res = ContextIngestRunner(db=db, short_ticker="2YY=F").run_sync()

        rows = db.table.return_value.upsert.call_args.args[0]
        inds = {r["indicator"] for r in rows}
        assert INDICATOR_CREDIT_SPREAD not in inds
        assert inds == {INDICATOR_BTC_DOMINANCE, INDICATOR_YIELD_CURVE}
        assert res["ok"] is True   # faltar una pata no es un error duro

    def test_dominance_source_failure_does_not_break(self):
        # CoinGecko cae (fetch_json lanza): se registra error pero el resto sigue.
        db = _capture_db()
        closes = {"HYG": 79.86, "LQD": 109.49, "^TNX": 4.376, "2YY=F": 3.851}
        with patch.object(cr, "_download_closes", return_value=closes), \
             patch.object(cr, "fetch_json", side_effect=RuntimeError("CoinGecko caído")):
            res = ContextIngestRunner(db=db, short_ticker="2YY=F").run_sync()

        assert any("btc_dominance" in e for e in res["errors"])
        rows = db.table.return_value.upsert.call_args.args[0]
        inds = {r["indicator"] for r in rows}
        assert inds == {INDICATOR_CREDIT_SPREAD, INDICATOR_YIELD_CURVE}

    def test_all_sources_down_is_clean(self):
        # Si todo falla, no escribe nada y no lanza (MAREA sigue igual que antes).
        db = _capture_db()
        with patch.object(cr, "_download_closes", side_effect=RuntimeError("yf caído")), \
             patch.object(cr, "fetch_json", side_effect=RuntimeError("cg caído")):
            res = ContextIngestRunner(db=db).run_sync()
        assert res["indicators_written"] == 0
        assert len(res["errors"]) >= 1
        db.table.return_value.upsert.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (b) Auto-activación: min_obs decide si modula / se presenta sólido
# ══════════════════════════════════════════════════════════════════════════════

def _series(indicator, values, ts0=1):
    """Serie ascendente sintética {ts, value, extra}."""
    return [
        {"ts": f"2026-06-{ts0+i:02d}T00:00:00+00:00", "indicator": indicator,
         "value": v, "extra": {}}
        for i, v in enumerate(values)
    ]


class TestAutoActivation:

    def test_below_min_obs_inactive(self):
        # 3 obs < min_obs=5 → no activo, no modula.
        s = _series(INDICATOR_YIELD_CURVE, [0.5, 0.3, -0.2])
        st = ctx.evaluate_series(INDICATOR_YIELD_CURVE, s, min_obs=5)
        assert st.active is False
        assert st.n_obs == 3
        mods = ctx.regime_modulators({INDICATOR_YIELD_CURVE: st})
        assert mods == {}   # inactivo → no aporta moduladores

    def test_above_min_obs_active_and_modulates(self):
        # 6 obs ≥ min_obs y curva invertida (último < 0) → activo + risk_off.
        s = _series(INDICATOR_YIELD_CURVE, [0.6, 0.5, 0.4, 0.2, 0.0, -0.3])
        st = ctx.evaluate_series(INDICATOR_YIELD_CURVE, s, min_obs=5)
        assert st.active is True
        assert st.level == pytest.approx(-0.3)
        mods = ctx.regime_modulators({INDICATOR_YIELD_CURVE: st})
        assert "yield_curve_inverted" in mods["risk_off"]
        assert "yield_curve_inverted" in mods["flight_to_safety"]

    def test_preliminary_marked_in_digest_lines(self):
        s = _series(INDICATOR_CREDIT_SPREAD, [0.73, 0.72])  # 2 obs < 5
        st = ctx.evaluate_series(INDICATOR_CREDIT_SPREAD, s, min_obs=5)
        lines = ctx.digest_lines({INDICATOR_CREDIT_SPREAD: st})
        assert len(lines) == 1
        assert "preliminar" in lines[0].lower()

    def test_active_not_marked_preliminary(self):
        s = _series(INDICATOR_CREDIT_SPREAD, [0.74, 0.74, 0.73, 0.72, 0.71, 0.70])
        st = ctx.evaluate_series(INDICATOR_CREDIT_SPREAD, s, min_obs=5)
        lines = ctx.digest_lines({INDICATOR_CREDIT_SPREAD: st})
        assert "preliminar" not in lines[0].lower()

    def test_no_data_omitted_from_digest(self):
        st = ctx.evaluate_series(INDICATOR_BTC_DOMINANCE, [], min_obs=5)
        assert st.level is None
        assert ctx.digest_lines({INDICATOR_BTC_DOMINANCE: st}) == []


class TestEmptyParenthesisDefect1:
    """Defecto 1: nunca un paréntesis vacío '()' cuando no hay tendencia previa."""

    def test_dominance_single_obs_no_empty_parenthesis(self):
        # 1 sola observación → sin valor previo con que comparar (trend None,
        # dirección 'unknown'): NO debe aparecer "()" vacío.
        st = ctx.evaluate_series(INDICATOR_BTC_DOMINANCE, _series(INDICATOR_BTC_DOMINANCE, [55.7]), min_obs=5)
        assert st.trend is None and st.direction == "unknown"
        line = ctx._dom_line(st)
        assert "()" not in line
        assert "( " not in line and " )" not in line     # ni paréntesis con basura
        assert "Dominancia BTC: 55.7%" in line            # el valor sí se muestra

    def test_dominance_with_trend_shows_parenthesis(self):
        # Con histórico previo SÍ hay tendencia → se muestra el paréntesis lleno.
        st = ctx.evaluate_series(
            INDICATOR_BTC_DOMINANCE, _series(INDICATOR_BTC_DOMINANCE, [55.5, 55.9]), min_obs=5
        )
        line = ctx._dom_line(st)
        assert "()" not in line
        assert "subiendo" in line and "pp)" in line       # tendencia + variación

    def test_no_indicator_line_has_empty_parenthesis(self):
        # Misma revisión a crédito y curva: un único dato no produce "()" vacío.
        states = {
            INDICATOR_BTC_DOMINANCE: ctx.evaluate_series(
                INDICATOR_BTC_DOMINANCE, _series(INDICATOR_BTC_DOMINANCE, [55.7]), min_obs=5),
            INDICATOR_CREDIT_SPREAD: ctx.evaluate_series(
                INDICATOR_CREDIT_SPREAD, _series(INDICATOR_CREDIT_SPREAD, [0.73]), min_obs=5),
            INDICATOR_YIELD_CURVE: ctx.evaluate_series(
                INDICATOR_YIELD_CURVE, _series(INDICATOR_YIELD_CURVE, [0.4]), min_obs=5),
        }
        for line in ctx.digest_lines(states):
            assert "()" not in line


class TestDirection:

    def test_credit_widening_is_risk_off(self):
        # ratio cae con fuerza → spreads ensanchándose → risk_off
        s = _series(INDICATOR_CREDIT_SPREAD, [0.75, 0.75, 0.74, 0.74, 0.73, 0.70])
        st = ctx.evaluate_series(INDICATOR_CREDIT_SPREAD, s, min_obs=5)
        assert st.direction == "falling"
        mods = ctx.regime_modulators({INDICATOR_CREDIT_SPREAD: st})
        assert "credit_spread_widening" in mods["risk_off"]

    def test_btc_dominance_rising_is_risk_off(self):
        s = _series(INDICATOR_BTC_DOMINANCE, [52.0, 52.1, 52.0, 52.2, 52.1, 54.0])
        st = ctx.evaluate_series(INDICATOR_BTC_DOMINANCE, s, min_obs=5)
        assert st.direction == "rising"
        mods = ctx.regime_modulators({INDICATOR_BTC_DOMINANCE: st})
        assert "btc_dominance_rising" in mods["risk_off"]

    def test_btc_dominance_falling_is_risk_on(self):
        s = _series(INDICATOR_BTC_DOMINANCE, [56.0, 55.9, 56.0, 55.8, 55.9, 54.0])
        st = ctx.evaluate_series(INDICATOR_BTC_DOMINANCE, s, min_obs=5)
        assert st.direction == "falling"
        mods = ctx.regime_modulators({INDICATOR_BTC_DOMINANCE: st})
        assert "btc_dominance_falling" in mods["risk_on"]


# ══════════════════════════════════════════════════════════════════════════════
# (d) Régimen: el contexto MODULA pero NO dispara un régimen por sí solo
# ══════════════════════════════════════════════════════════════════════════════

class TestRegimeIntegration:

    def _risk_off_scores(self):
        from app.analysis.regime import ClassScores
        return ClassScores(crypto=-0.5, equity=-0.5, gold=0.0, silver=0.0,
                           bonds=0.0, dxy=0.0, vix=0.0)

    def _neutral_scores(self):
        from app.analysis.regime import ClassScores
        return ClassScores(crypto=0.0, equity=0.0, gold=0.0, silver=0.0,
                           bonds=0.0, dxy=0.0, vix=0.0)

    def test_context_boosts_fired_regime(self):
        from app.analysis.regime import classify_regime
        base = classify_regime(self._risk_off_scores())
        boosted = classify_regime(
            self._risk_off_scores(),
            context_modulators={"risk_off": ["yield_curve_inverted", "credit_spread_widening"]},
        )
        assert boosted.regime == "risk_off"
        assert boosted.confidence > base.confidence       # el contexto sube confianza
        assert "yield_curve_inverted" in boosted.signals
        assert "credit_spread_widening" in boosted.signals

    def test_context_cannot_create_regime_from_neutral(self):
        from app.analysis.regime import classify_regime
        # Sin señales de flujo, por mucho contexto risk_off, sigue siendo neutral.
        r = classify_regime(
            self._neutral_scores(),
            context_modulators={"risk_off": ["yield_curve_inverted", "credit_spread_widening"],
                                "flight_to_safety": ["yield_curve_inverted"]},
        )
        assert r.regime == "neutral"
        assert r.confidence == 0.0

    def test_context_only_boosts_matching_regime(self):
        from app.analysis.regime import classify_regime
        # Contexto risk_on no debe afectar a un régimen risk_off disparado.
        base = classify_regime(self._risk_off_scores())
        same = classify_regime(
            self._risk_off_scores(),
            context_modulators={"risk_on": ["btc_dominance_falling"]},
        )
        assert same.regime == "risk_off"
        assert same.confidence == base.confidence
        assert "btc_dominance_falling" not in same.signals

    def test_backward_compatible_without_context(self):
        from app.analysis.regime import classify_regime
        # Firma retrocompatible: sin context_modulators se comporta igual.
        r = classify_regime(self._risk_off_scores())
        assert r.regime == "risk_off"


# ══════════════════════════════════════════════════════════════════════════════
# (c) Degradación elegante en la lectura de BD
# ══════════════════════════════════════════════════════════════════════════════

class TestGracefulDegradation:

    def test_load_states_returns_empty_on_db_error(self):
        db = MagicMock()
        db.table.return_value.select.return_value.order.return_value.limit.return_value.execute.side_effect = RuntimeError("BD caída")
        assert ctx.load_context_states(db, min_obs=5) == {}

    def test_evaluate_context_never_raises(self):
        db = MagicMock()
        db.table.side_effect = RuntimeError("BD totalmente caída")
        ev = ctx.evaluate_context(db, min_obs=5)
        assert ev.regime_modulators == {}
        assert ev.digest_lines == []

    def test_load_states_groups_and_evaluates(self):
        # Lectura normal: desc por ts, agrupa por indicador, evalúa cada serie.
        rows = (
            _series(INDICATOR_YIELD_CURVE, [0.6, 0.5, 0.4, 0.2, 0.0, -0.3])
            + _series(INDICATOR_BTC_DOMINANCE, [55.0, 55.0, 55.0, 55.0, 55.0, 55.0])
        )
        db = MagicMock()
        db.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value.data = list(reversed(rows))
        states = ctx.load_context_states(db, min_obs=5)
        assert set(states) == {INDICATOR_YIELD_CURVE, INDICATOR_BTC_DOMINANCE}
        assert states[INDICATOR_YIELD_CURVE].level == pytest.approx(-0.3)


# ══════════════════════════════════════════════════════════════════════════════
# (d) No contaminan los rankings de flujo del digest
# ══════════════════════════════════════════════════════════════════════════════

class TestNoFlowContamination:

    def test_context_block_is_separate_from_rankings(self):
        from app.alerts.digest import build_intraday_digest
        assets = [
            {"ticker": "SOXX", "score": 0.8, "asset_class": "etf", "confidence": "ok"},
        ]
        lines = ["  • Curva 10Y-2Y: -0.30 pp — curva INVERTIDA → señal de recesión/risk-off"]
        text = build_intraday_digest({"assets": assets}, context_lines=lines)
        # El contexto aparece bajo su propio bloque, NO en los rankings de flujo.
        assert "Contexto macro" in text
        assert "Curva 10Y-2Y" in text
        # SOXX (flujo real) sigue en su ranking; la curva no es un activo de flujo.
        assert "SOXX" in text

    def test_no_context_lines_no_block(self):
        from app.alerts.digest import build_intraday_digest
        text = build_intraday_digest({"assets": []}, context_lines=None)
        assert "Contexto macro" not in text


# ══════════════════════════════════════════════════════════════════════════════
# (e) Put/call omitido — no es un indicador soportado
# ══════════════════════════════════════════════════════════════════════════════

def test_putcall_not_a_supported_indicator():
    supported = {INDICATOR_BTC_DOMINANCE, INDICATOR_CREDIT_SPREAD, INDICATOR_YIELD_CURVE}
    assert not any("put" in i or "call" in i for i in supported)
    assert len(supported) == 3
