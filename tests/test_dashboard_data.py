"""
Tests unitarios para dashboard/data.py.

Testean las funciones _fetch_* (lógica pura de lectura y transformación)
con clientes Supabase completamente mockeados.
No requieren red, no requieren Supabase real.
"""
import pytest
import pandas as pd
from unittest.mock import MagicMock


# ── Helpers de mock ───────────────────────────────────────────────────────────

def _make_chain(responses: list) -> MagicMock:
    """
    Crea una cadena mock de Supabase que devuelve `responses[i]` en la i-ésima
    llamada a .execute().
    Todos los métodos del query builder (select, eq, order, limit, in_) devuelven
    la propia cadena para soportar fluent API.
    """
    chain = MagicMock()
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.in_.return_value = chain
    if len(responses) == 1:
        chain.execute.return_value = MagicMock(data=responses[0])
    else:
        chain.execute.side_effect = [MagicMock(data=r) for r in responses]
    return chain


def _make_db(**table_responses: list) -> MagicMock:
    """
    Construye un mock del cliente Supabase.

    Uso:
        db = _make_db(
            regimes=[row1, row2],
            flow_scores=[ [ts_row], [score_row1, score_row2] ],
        )

    Si el valor es una lista de listas (list[list]), se interpreta como
    múltiples llamadas consecutivas a esa tabla (side_effect).
    Si el valor es una lista de dicts, es una sola llamada.
    """
    table_chains: dict[str, MagicMock] = {}
    for table, value in table_responses.items():
        if value and isinstance(value[0], list):
            # múltiples llamadas
            table_chains[table] = _make_chain(value)
        else:
            # una sola llamada
            table_chains[table] = _make_chain([value])

    db = MagicMock()
    db.table.side_effect = lambda name: table_chains.get(name, _make_chain([[]]))
    return db


# ── Tests: régimen ────────────────────────────────────────────────────────────

class TestFetchRegimeCurrent:
    def test_empty_returns_none(self):
        from dashboard.data import _fetch_regime_current
        db = _make_db(regimes=[])
        assert _fetch_regime_current(db) is None

    def test_returns_first_row(self):
        from dashboard.data import _fetch_regime_current
        row = {"ts": "2024-01-01", "win": "7d", "regime": "risk_on", "confidence": 0.85, "signals": ["crypto_inflow"]}
        db = _make_db(regimes=[row])
        result = _fetch_regime_current(db)
        assert result["regime"] == "risk_on"
        assert result["confidence"] == 0.85

    def test_returns_signals(self):
        from dashboard.data import _fetch_regime_current
        row = {"ts": "2024-01-02", "regime": "risk_off", "confidence": 0.6, "signals": ["crypto_outflow", "equity_outflow"]}
        db = _make_db(regimes=[row])
        result = _fetch_regime_current(db)
        assert "crypto_outflow" in result["signals"]


class TestFetchRegimeHistory:
    def test_empty_returns_empty_list(self):
        from dashboard.data import _fetch_regime_history
        db = _make_db(regimes=[])
        assert _fetch_regime_history(db, 10) == []

    def test_returns_all_rows(self):
        from dashboard.data import _fetch_regime_history
        rows = [
            {"ts": "2024-01-03", "regime": "risk_on", "confidence": 0.7},
            {"ts": "2024-01-02", "regime": "neutral", "confidence": 0.0},
        ]
        db = _make_db(regimes=rows)
        result = _fetch_regime_history(db, 10)
        assert len(result) == 2
        assert result[0]["regime"] == "risk_on"


# ── Tests: narrativa ──────────────────────────────────────────────────────────

class TestFetchLatestNarrative:
    def test_empty_returns_none(self):
        from dashboard.data import _fetch_latest_narrative
        db = _make_db(narratives=[])
        assert _fetch_latest_narrative(db) is None

    def test_returns_narrative(self):
        from dashboard.data import _fetch_latest_narrative
        row = {
            "ts": "2024-01-01T00:00:00",
            "regime_at_ts": "risk_on",
            "confidence": 0.75,
            "text": "Los datos muestran flujos positivos hacia crypto.",
            "llm_engine": "groq",
        }
        db = _make_db(narratives=[row])
        result = _fetch_latest_narrative(db)
        assert "flujos positivos" in result["text"]
        assert result["confidence"] == 0.75

    def test_confidence_preserved(self):
        from dashboard.data import _fetch_latest_narrative
        row = {"ts": "2024-01-01T00:00:00", "regime_at_ts": "neutral",
               "confidence": 0.2, "text": "Datos preliminares.", "llm_engine": "groq"}
        db = _make_db(narratives=[row])
        result = _fetch_latest_narrative(db)
        assert result["confidence"] == 0.2


# ── Tests: flow scores ────────────────────────────────────────────────────────

class TestFetchFlowScores:
    def test_no_scores_returns_empty_df(self):
        from dashboard.data import _fetch_flow_scores
        # Primer call a flow_scores devuelve vacío (sin ts)
        db = _make_db(flow_scores=[[]])
        result = _fetch_flow_scores(db)
        assert result.empty

    def test_basic_merge(self):
        from dashboard.data import _fetch_flow_scores
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        score_rows = [
            {"asset_id": 1, "score": 0.8, "confidence": "ok", "proxy_used": "vol", "n_obs": 30},
            {"asset_id": 2, "score": -0.5, "confidence": "low", "proxy_used": "price", "n_obs": 5},
        ]
        asset_rows = [
            {"id": 1, "ticker": "SPY", "name": "SPDR S&P 500", "asset_class": "etf", "sector": "broad_market"},
            {"id": 2, "ticker": "BTC", "name": "Bitcoin", "asset_class": "crypto", "sector": None},
        ]
        db = _make_db(
            flow_scores=[ts_row, score_rows],  # dos llamadas a flow_scores
            assets=[asset_rows],
        )
        df = _fetch_flow_scores(db)
        assert len(df) == 2
        spy = df[df["ticker"] == "SPY"].iloc[0]
        assert spy["score"] == pytest.approx(0.8)
        assert spy["confidence"] == "ok"
        btc = df[df["ticker"] == "BTC"].iloc[0]
        assert btc["confidence"] == "low"
        assert btc["score"] == pytest.approx(-0.5)

    def test_missing_asset_uses_placeholder(self):
        from dashboard.data import _fetch_flow_scores
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        score_rows = [{"asset_id": 99, "score": 0.3, "confidence": "ok", "proxy_used": "vol", "n_obs": 20}]
        db = _make_db(
            flow_scores=[ts_row, score_rows],
            assets=[[]],  # no assets
        )
        df = _fetch_flow_scores(db)
        assert df.iloc[0]["ticker"] == "?"

    def test_null_score_becomes_zero(self):
        from dashboard.data import _fetch_flow_scores
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        score_rows = [{"asset_id": 1, "score": None, "confidence": "low", "proxy_used": "x", "n_obs": 3}]
        asset_rows = [{"id": 1, "ticker": "X", "name": "X", "asset_class": "etf", "sector": None}]
        db = _make_db(flow_scores=[ts_row, score_rows], assets=[asset_rows])
        df = _fetch_flow_scores(db)
        assert df.iloc[0]["score"] == 0.0

    def test_score_column_is_numeric(self):
        from dashboard.data import _fetch_flow_scores
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        score_rows = [{"asset_id": 1, "score": 0.55, "confidence": "ok", "proxy_used": "v", "n_obs": 10}]
        asset_rows = [{"id": 1, "ticker": "GLD", "name": "Gold ETF", "asset_class": "etf", "sector": "commodities"}]
        db = _make_db(flow_scores=[ts_row, score_rows], assets=[asset_rows])
        df = _fetch_flow_scores(db)
        assert pd.api.types.is_float_dtype(df["score"])


# ── Tests: correlaciones ──────────────────────────────────────────────────────

class TestFetchCorrelations:
    def test_empty_returns_empty_df(self):
        from dashboard.data import _fetch_correlations
        db = _make_db(correlations=[[]])
        result = _fetch_correlations(db)
        assert result.empty

    def test_matrix_is_symmetric(self):
        from dashboard.data import _fetch_correlations
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        pair_rows = [
            {"pair_a": "crypto", "pair_b": "equity", "corr": 0.65, "is_decoupling": False},
            {"pair_a": "crypto", "pair_b": "gold", "corr": -0.3, "is_decoupling": False},
            {"pair_a": "equity", "pair_b": "gold", "corr": 0.1, "is_decoupling": False},
        ]
        db = _make_db(correlations=[ts_row, pair_rows])
        matrix = _fetch_correlations(db)
        assert not matrix.empty
        assert matrix.loc["crypto", "equity"] == pytest.approx(0.65)
        assert matrix.loc["equity", "crypto"] == pytest.approx(0.65)
        assert matrix.loc["crypto", "gold"] == pytest.approx(-0.3)

    def test_diagonal_is_one(self):
        from dashboard.data import _fetch_correlations
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        pair_rows = [{"pair_a": "A", "pair_b": "B", "corr": 0.5, "is_decoupling": False}]
        db = _make_db(correlations=[ts_row, pair_rows])
        matrix = _fetch_correlations(db)
        assert matrix.loc["A", "A"] == pytest.approx(1.0)
        assert matrix.loc["B", "B"] == pytest.approx(1.0)

    def test_decoupling_in_attrs(self):
        from dashboard.data import _fetch_correlations
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        pair_rows = [
            {"pair_a": "crypto", "pair_b": "equity", "corr": 0.1, "is_decoupling": True},
            {"pair_a": "gold", "pair_b": "bonds", "corr": 0.8, "is_decoupling": False},
        ]
        db = _make_db(correlations=[ts_row, pair_rows])
        matrix = _fetch_correlations(db)
        dp = matrix.attrs.get("decoupling_pairs", set())
        assert ("crypto", "equity") in dp
        assert ("equity", "crypto") in dp
        assert ("gold", "bonds") not in dp

    def test_no_decoupling_gives_empty_set(self):
        from dashboard.data import _fetch_correlations
        ts_row = [{"ts": "2024-01-01T00:00:00+00:00"}]
        pair_rows = [{"pair_a": "A", "pair_b": "B", "corr": 0.9, "is_decoupling": False}]
        db = _make_db(correlations=[ts_row, pair_rows])
        matrix = _fetch_correlations(db)
        assert matrix.attrs.get("decoupling_pairs") == set()


# ── Tests: rotaciones ─────────────────────────────────────────────────────────

class TestFetchRotations:
    def test_empty_returns_empty_list(self):
        from dashboard.data import _fetch_rotations
        db = _make_db(rotations=[])
        assert _fetch_rotations(db, 10) == []

    def test_returns_rows(self):
        from dashboard.data import _fetch_rotations
        rows = [
            {"ts": "2024-01-01", "from_sector": "technology", "to_sector": "energy", "strength": 0.6},
        ]
        db = _make_db(rotations=rows)
        result = _fetch_rotations(db, 10)
        assert len(result) == 1
        assert result[0]["from_sector"] == "technology"


# ── Tests: exposiciones ───────────────────────────────────────────────────────

class TestFetchExposures:
    def test_empty_returns_empty_list(self):
        from dashboard.data import _fetch_exposures
        db = _make_db(exposures=[])
        assert _fetch_exposures(db) == []

    def test_returns_all_fields(self):
        from dashboard.data import _fetch_exposures
        row = {
            "source_entity": "OpenAI",
            "exposed_ticker": "MSFT",
            "exposure_type": "pre_ipo",
            "relationship": "inversión directa",
            "confidence": "confirmado_oficial",
            "sources": ["https://sec.gov/example"],
            "last_verified_at": "2024-01-01",
        }
        db = _make_db(exposures=[row])
        result = _fetch_exposures(db)
        assert result[0]["source_entity"] == "OpenAI"
        assert result[0]["confidence"] == "confirmado_oficial"

    def test_low_confidence_exposure_preserved(self):
        from dashboard.data import _fetch_exposures
        row = {
            "source_entity": "SpaceX",
            "exposed_ticker": "GOOGL",
            "exposure_type": "pre_ipo",
            "relationship": "especulado",
            "confidence": "especulacion",
            "sources": ["https://blog.example.com"],
            "last_verified_at": "2024-01-01",
        }
        db = _make_db(exposures=[row])
        result = _fetch_exposures(db)
        assert result[0]["confidence"] == "especulacion"


# ── Tests: alertas ────────────────────────────────────────────────────────────

class TestFetchAlerts:
    def test_empty_returns_empty_list(self):
        from dashboard.data import _fetch_alerts
        db = _make_db(alerts=[])
        assert _fetch_alerts(db, 10) == []

    def test_returns_sent_and_unsent(self):
        from dashboard.data import _fetch_alerts
        rows = [
            {"alert_type": "regime_change", "entity": "market", "state": "risk_off",
             "confidence": 0.7, "sent": True, "not_sent_reason": None,
             "ts": "2024-01-01T10:00:00", "sent_at": "2024-01-01T10:01:00"},
            {"alert_type": "flow_extreme", "entity": "BTC", "state": "extreme",
             "confidence": 0.2, "sent": False, "not_sent_reason": "low_confidence",
             "ts": "2024-01-01T09:00:00", "sent_at": None},
        ]
        db = _make_db(alerts=rows)
        result = _fetch_alerts(db, 10)
        assert len(result) == 2
        sent = [a for a in result if a["sent"]]
        unsent = [a for a in result if not a["sent"]]
        assert len(sent) == 1
        assert len(unsent) == 1
        assert unsent[0]["not_sent_reason"] == "low_confidence"

    def test_confidence_field_present(self):
        from dashboard.data import _fetch_alerts
        rows = [{"alert_type": "decoupling", "entity": "crypto/equity", "state": "decoupled",
                 "confidence": 0.55, "sent": True, "not_sent_reason": None,
                 "ts": "2024-01-01", "sent_at": "2024-01-01"}]
        db = _make_db(alerts=rows)
        result = _fetch_alerts(db, 10)
        assert result[0]["confidence"] == 0.55
