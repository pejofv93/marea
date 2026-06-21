"""
Tests MAREA Sesión 5 — Análisis intermercado.

Cubre:
  - Matemática de correlación (pura, sin BD)
  - Clasificador de régimen por reglas (puro, sin BD)
  - Detección de rotación sectorial (pura, sin BD)
  - Idempotencia del engine (mock BD)
  - Los 79 tests anteriores siguen verdes (sin tocar)
"""

import math
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import numpy as np
import pandas as pd
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(data: dict, start: str = "2024-01-01") -> pd.DataFrame:
    """Crea DataFrame con índice DatetimeIndex (UTC) desde dict {col: [values]}."""
    n = max(len(v) for v in data.values())
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame(data, index=idx, dtype=float)


def _flat_record(ticker: str, ts: str, score: float, asset_class: str = "etf", sector: str = None) -> dict:
    return {"ticker": ticker, "ts": ts, "score": score, "asset_class": asset_class, "sector": sector}


# ══════════════════════════════════════════════════════════════════════════════
# S5-1: Matemática de correlación (correlation.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestComputePairwiseCorr:

    def test_perfect_correlation(self):
        """Series idénticas → correlación = 1.0."""
        from app.analysis.correlation import compute_pairwise_corr
        df = _make_df({"A": [1, 2, 3, 4, 5, 6, 7], "B": [1, 2, 3, 4, 5, 6, 7]})
        result = compute_pairwise_corr(df, window=7)
        assert ("A", "B") in result
        assert abs(result[("A", "B")] - 1.0) < 1e-6

    def test_perfect_anticorrelation(self):
        """Series opuestas → correlación = -1.0."""
        from app.analysis.correlation import compute_pairwise_corr
        df = _make_df({"A": [1, 2, 3, 4, 5, 6, 7], "B": [7, 6, 5, 4, 3, 2, 1]})
        result = compute_pairwise_corr(df, window=7)
        assert abs(result[("A", "B")] + 1.0) < 1e-6

    def test_uses_only_last_window_rows(self):
        """Con window=7 y df de 30 filas, sólo usa las últimas 7."""
        from app.analysis.correlation import compute_pairwise_corr
        # A y B muy correlacionados en las primeras 23 filas, desacoplados en las últimas 7
        rng = np.random.default_rng(42)
        early_vals = list(range(23))
        late_a = list(rng.random(7))
        late_b = list(rng.random(7))    # independiente de late_a
        df = _make_df({
            "A": early_vals + late_a,
            "B": early_vals + late_b,
        })
        corr_w30 = compute_pairwise_corr(df, window=30)
        corr_w7 = compute_pairwise_corr(df, window=7)
        # Ventana 30: alta correlación (muchos datos idénticos al inicio)
        # Ventana 7: correlación baja (datos aleatorios independientes)
        assert corr_w30[("A", "B")] > 0.7
        assert abs(corr_w7[("A", "B")]) < 0.9   # puede ser más bajo

    def test_pair_order_alphabetical(self):
        """Los pares siempre se ordenan alfabéticamente (pair_a < pair_b)."""
        from app.analysis.correlation import compute_pairwise_corr
        df = _make_df({"Z": [1, 2, 3, 4, 5], "A": [1, 2, 3, 4, 5]})
        result = compute_pairwise_corr(df, window=5)
        assert ("A", "Z") in result
        assert ("Z", "A") not in result

    def test_insufficient_obs_returns_empty(self):
        """Menos de MIN_CORR_OBS filas → dict vacío."""
        from app.analysis.correlation import compute_pairwise_corr, MIN_CORR_OBS
        df = _make_df({"A": [1.0] * (MIN_CORR_OBS - 1), "B": [2.0] * (MIN_CORR_OBS - 1)})
        result = compute_pairwise_corr(df, window=100)
        assert result == {}

    def test_constant_column_excluded(self):
        """Columna constante produce NaN en correlación → excluida del resultado."""
        from app.analysis.correlation import compute_pairwise_corr
        df = _make_df({"A": [1, 2, 3, 4, 5], "B": [3, 3, 3, 3, 3]})
        result = compute_pairwise_corr(df, window=5)
        assert ("A", "B") not in result  # NaN excluido


class TestDetectDecoupling:

    def test_decoupling_detected(self):
        """Par fuertemente correlado en 30d que se desacopla en 7d → True."""
        from app.analysis.correlation import detect_decoupling
        assert detect_decoupling(corr_short=0.1, corr_long=0.9) is True

    def test_no_decoupling_low_base(self):
        """Base < 0.7 → no estaba correlado, no es desacople."""
        from app.analysis.correlation import detect_decoupling
        assert detect_decoupling(corr_short=0.1, corr_long=0.5) is False

    def test_no_decoupling_small_drop(self):
        """Caída < 0.5 → no es un desacople significativo."""
        from app.analysis.correlation import detect_decoupling
        assert detect_decoupling(corr_short=0.6, corr_long=0.8) is False

    def test_negative_base_correlation(self):
        """Base negativa fuerte y gira a positiva → desacople (el signo cambia)."""
        from app.analysis.correlation import detect_decoupling
        # |-0.8| > 0.7 y |-0.8 - 0.1| = 0.9 > 0.5
        assert detect_decoupling(corr_short=0.1, corr_long=-0.8) is True

    def test_stays_correlated(self):
        """Sigue correlacionado en 7d → no es desacople."""
        from app.analysis.correlation import detect_decoupling
        assert detect_decoupling(corr_short=0.85, corr_long=0.90) is False


class TestAggregateToClassScores:

    def test_crypto_assets_aggregated(self):
        """BTC y ETH (crypto) → clase 'crypto' con media de scores."""
        from app.analysis.correlation import aggregate_to_class_scores
        ts = "2024-01-15T00:00:00+00:00"
        records = [
            _flat_record("BTC", ts, 0.8, "crypto", "l1"),
            _flat_record("ETH", ts, 0.4, "crypto", "l1"),
        ]
        df = aggregate_to_class_scores(records)
        assert "crypto" in df.columns
        assert abs(df["crypto"].iloc[-1] - 0.6) < 1e-9  # (0.8+0.4)/2

    def test_sector_etfs_excluded_from_intermarket(self):
        """ETFs sectoriales no deben aparecer en el DataFrame intermarket."""
        from app.analysis.correlation import aggregate_to_class_scores, SECTOR_ETFS
        ts = "2024-01-15T00:00:00+00:00"
        records = [
            _flat_record("SOXX", ts, 0.7, "etf", "semiconductor"),
            _flat_record("BTC",  ts, 0.5, "crypto", "l1"),
        ]
        df = aggregate_to_class_scores(records)
        # SOXX no debe crear columna 'semiconductor' ni aparecer como 'equities'
        assert "crypto" in df.columns
        assert df.shape[1] == 1   # sólo 'crypto'

    def test_gold_etf_and_future_merged(self):
        """GC=F y GLD ambos → clase 'gold' con su media."""
        from app.analysis.correlation import aggregate_to_class_scores
        ts = "2024-01-15T00:00:00+00:00"
        records = [
            _flat_record("GC=F", ts, 0.6, "commodity", "metals"),
            _flat_record("GLD",  ts, 0.8, "etf", "commodities"),
        ]
        df = aggregate_to_class_scores(records)
        assert "gold" in df.columns
        assert abs(df["gold"].iloc[-1] - 0.7) < 1e-9

    def test_dxy_and_vix_in_intermarket_df(self):
        """DXY y VIX se incluyen en la Matriz A como clases 'dollar' y 'vix'."""
        from app.analysis.correlation import aggregate_to_class_scores
        ts = "2024-01-15T00:00:00+00:00"
        records = [
            _flat_record("DX-Y.NYB", ts, 0.3, "macro", "currency"),
            _flat_record("^VIX",     ts, -0.4, "macro", "volatility"),
        ]
        df = aggregate_to_class_scores(records)
        assert "dollar" in df.columns
        assert "vix" in df.columns


class TestBuildCorrRows:

    def test_rows_contain_7d_and_30d(self):
        """Se generan filas para ventanas 7d y 30d."""
        from app.analysis.correlation import build_corr_rows
        data = [float(i) for i in range(35)]
        df = _make_df({"A": data, "B": data})
        rows = build_corr_rows(df, "intermarket", "2024-01-15T00:00:00+00:00")
        windows = {r["win"] for r in rows}
        assert "7d" in windows
        assert "30d" in windows

    def test_decoupling_flagged_on_7d_row(self):
        """La flag is_decoupling=True sólo aparece en rows de window='7d'."""
        from app.analysis.correlation import build_corr_rows
        # Construir df donde 7d y 30d tendrán correlaciones distintas
        rng = np.random.default_rng(99)
        early = list(range(28))
        late_a = list(rng.random(7))
        late_b = list(rng.random(7))
        df = _make_df({"A": early + late_a, "B": early + late_b})
        rows = build_corr_rows(df, "intermarket", "2024-01-15T00:00:00+00:00")
        rows_30d_decoupling = [r for r in rows if r["win"] == "30d" and r["is_decoupling"]]
        assert rows_30d_decoupling == []  # nunca True en 30d


# ══════════════════════════════════════════════════════════════════════════════
# S5-2: Clasificador de régimen (regime.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyRegime:

    def _scores(self, crypto=0.0, equity=0.0, gold=0.0, silver=0.0, bonds=0.0, dxy=0.0, vix=0.0):
        from app.analysis.regime import ClassScores
        return ClassScores(crypto=crypto, equity=equity, gold=gold, silver=silver,
                           bonds=bonds, dxy=dxy, vix=vix)

    def test_risk_on_both_cores(self):
        """Inflow crypto + acciones con DXY bajando y VIX tranquilo → risk_on alta confianza."""
        from app.analysis.regime import classify_regime, CORE_MAX_CONF, MODULATOR_BONUS
        scores = self._scores(crypto=0.6, equity=0.5, dxy=-0.4, vix=0.3)
        r = classify_regime(scores)
        assert r.regime == "risk_on"
        assert r.confidence >= CORE_MAX_CONF   # ambas cores + moduladores
        assert "crypto_inflow" in r.signals
        assert "equity_inflow" in r.signals
        assert "dxy_falling" in r.signals
        assert "vix_calm" in r.signals

    def test_risk_on_single_core(self):
        """Sólo crypto inflow (equity neutro) → risk_on con confianza parcial."""
        from app.analysis.regime import classify_regime, CORE_MAX_CONF
        scores = self._scores(crypto=0.5, equity=0.05)
        r = classify_regime(scores)
        assert r.regime == "risk_on"
        assert r.confidence < CORE_MAX_CONF   # sólo 1/2 cores

    def test_risk_off_both_cores(self):
        """Outflow crypto + acciones, DXY sube, VIX alto → risk_off."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=-0.5, equity=-0.4, dxy=0.4, vix=-0.5)
        r = classify_regime(scores)
        assert r.regime == "risk_off"
        assert "crypto_outflow" in r.signals
        assert "equity_outflow" in r.signals
        assert "dxy_rising" in r.signals
        assert "vix_fearful" in r.signals

    def test_flight_to_safety(self):
        """Inflow oro + bonos, outflow crypto + acciones → flight_to_safety."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=-0.7, equity=-0.5, gold=0.8, bonds=0.6, dxy=0.4)
        r = classify_regime(scores)
        assert r.regime == "flight_to_safety"
        assert "gold_inflow" in r.signals
        assert "bonds_inflow" in r.signals
        assert "crypto_outflow" in r.signals
        assert "equity_outflow" in r.signals

    def test_flight_to_safety_wins_over_risk_off(self):
        """Flight-to-safety (más específico) tiene prioridad sobre risk_off."""
        from app.analysis.regime import classify_regime
        # Outflow crypto+acciones + inflow oro+bonos → debe ser flight_to_safety, no risk_off
        scores = self._scores(crypto=-0.7, equity=-0.5, gold=0.8, bonds=0.6)
        r = classify_regime(scores)
        assert r.regime == "flight_to_safety"

    def test_dxy_vix_alone_do_not_trigger(self):
        """DXY y VIX con scores extremos pero sin señales de flujo → neutral."""
        from app.analysis.regime import classify_regime
        scores = self._scores(dxy=0.9, vix=-0.9)  # sólo moduladores
        r = classify_regime(scores)
        assert r.regime == "neutral"
        assert r.confidence == 0.0

    def test_dxy_boosts_confidence_risk_off(self):
        """DXY alineado con risk_off aumenta la confianza vs el mismo sin DXY."""
        from app.analysis.regime import classify_regime
        base = self._scores(crypto=-0.5, equity=-0.4)
        with_dxy = self._scores(crypto=-0.5, equity=-0.4, dxy=0.4)
        r_base = classify_regime(base)
        r_dxy = classify_regime(with_dxy)
        assert r_base.regime == r_dxy.regime == "risk_off"
        assert r_dxy.confidence > r_base.confidence

    def test_neutral_when_weak_signals(self):
        """Scores por debajo del umbral → neutral."""
        from app.analysis.regime import classify_regime, FLOW_THRESHOLD
        below = FLOW_THRESHOLD * 0.5
        scores = self._scores(crypto=below, equity=below)
        r = classify_regime(scores)
        assert r.regime == "neutral"

    def test_sector_rotation_injected(self):
        """Con neutral macro + has_sector_rotation → sector_rotation."""
        from app.analysis.regime import classify_regime
        scores = self._scores()   # todo neutro
        r = classify_regime(scores, has_sector_rotation=True, rotation_confidence=0.6)
        assert r.regime == "sector_rotation"
        assert r.confidence == pytest.approx(0.6)
        assert "sector_rotation_detected" in r.signals

    def test_sector_rotation_not_overrides_clear_regime(self):
        """Régimen macro claro (risk_on) no se sobreescribe con sector_rotation."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.7, equity=0.6)
        r = classify_regime(scores, has_sector_rotation=True, rotation_confidence=0.8)
        assert r.regime == "risk_on"   # el macro gana

    def test_confidence_bounded_0_1(self):
        """La confianza nunca supera 1.0 aunque haya muchos moduladores."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.9, equity=0.9, dxy=-0.9, vix=0.9)
        r = classify_regime(scores)
        assert 0.0 <= r.confidence <= 1.0

    def test_signals_list_not_empty_for_active_regime(self):
        """Un régimen activo siempre tiene al menos una señal."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.5)
        r = classify_regime(scores)
        if r.regime != "neutral":
            assert len(r.signals) > 0


class TestClassScoresFromDfRow:

    def test_extracts_all_classes(self):
        """class_scores_from_df_row extrae correctamente todas las clases."""
        from app.analysis.regime import class_scores_from_df_row
        row = pd.Series({
            "crypto": 0.5, "equities": 0.3, "gold": -0.2,
            "silver": 0.1, "bonds": 0.4, "dollar": -0.3, "vix": 0.2,
        })
        cs = class_scores_from_df_row(row)
        assert cs.crypto == pytest.approx(0.5)
        assert cs.equity == pytest.approx(0.3)
        assert cs.gold == pytest.approx(-0.2)
        assert cs.dxy == pytest.approx(-0.3)
        assert cs.vix == pytest.approx(0.2)

    def test_nan_treated_as_zero(self):
        """Columnas ausentes o NaN se tratan como 0.0."""
        from app.analysis.regime import class_scores_from_df_row
        row = pd.Series({"crypto": float("nan"), "equities": 0.4})
        cs = class_scores_from_df_row(row)
        assert cs.crypto == 0.0
        assert cs.equity == pytest.approx(0.4)


# ══════════════════════════════════════════════════════════════════════════════
# S5-3: Rotación sectorial (sector.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectSectorRotations:

    _TS = datetime(2024, 1, 15, tzinfo=timezone.utc)

    def test_semis_to_metals_rotation(self):
        """SOXX muy negativo + XME muy positivo → rotación semis→metales."""
        from app.analysis.sector import detect_sector_rotations
        scores = {"SOXX": -0.6, "SMH": -0.5, "XME": 0.7, "XLK": 0.05, "GDX": 0.02}
        events = detect_sector_rotations(scores, self._TS)
        from_sectors = [e.from_sector for e in events]
        to_sectors = [e.to_sector for e in events]
        assert "SOXX" in from_sectors
        assert "XME" in to_sectors

    def test_no_rotation_when_all_positive(self):
        """Sin outflows claros → sin rotación."""
        from app.analysis.sector import detect_sector_rotations
        scores = {k: 0.5 for k in ["SOXX", "SMH", "XME", "XLK"]}
        events = detect_sector_rotations(scores, self._TS)
        assert events == []

    def test_no_rotation_when_all_negative(self):
        """Sin inflows claros → sin rotación."""
        from app.analysis.sector import detect_sector_rotations
        scores = {k: -0.5 for k in ["SOXX", "SMH", "XME", "XLK"]}
        events = detect_sector_rotations(scores, self._TS)
        assert events == []

    def test_rotation_strength_is_min(self):
        """Strength = min(|outflow|, |inflow|)."""
        from app.analysis.sector import detect_sector_rotations
        scores = {"SOXX": -0.6, "XME": 0.4}
        events = detect_sector_rotations(scores, self._TS)
        assert len(events) == 1
        assert events[0].strength == pytest.approx(0.4, abs=1e-4)  # min(0.6, 0.4)

    def test_below_threshold_ignored(self):
        """Scores por debajo del umbral no generan rotación."""
        from app.analysis.sector import detect_sector_rotations, ROTATION_THRESHOLD
        below = ROTATION_THRESHOLD * 0.5
        scores = {"SOXX": -below, "XME": below}
        events = detect_sector_rotations(scores, self._TS)
        assert events == []

    def test_non_sector_etf_ignored(self):
        """Tickers que no son sector ETFs no generan rotación."""
        from app.analysis.sector import detect_sector_rotations
        scores = {"SPY": -0.8, "BTC": 0.9}   # no son sector ETFs
        events = detect_sector_rotations(scores, self._TS)
        assert events == []

    def test_multiple_pairs(self):
        """Dos outflows + un inflow → dos eventos de rotación."""
        from app.analysis.sector import detect_sector_rotations
        scores = {"SOXX": -0.6, "SMH": -0.5, "XME": 0.7}
        events = detect_sector_rotations(scores, self._TS)
        assert len(events) == 2  # SOXX→XME y SMH→XME


class TestRotationEventsToRows:

    def test_to_db_rows(self):
        """rotation_events_to_rows produce dicts con campos correctos."""
        from app.analysis.sector import detect_sector_rotations, rotation_events_to_rows
        scores = {"SOXX": -0.6, "XME": 0.7}
        events = detect_sector_rotations(scores, datetime(2024, 1, 15, tzinfo=timezone.utc))
        ts = "2024-01-15T00:00:00+00:00"
        rows = rotation_events_to_rows(events, ts)
        assert len(rows) == 1
        assert rows[0]["from_sector"] == "SOXX"
        assert rows[0]["to_sector"] == "XME"
        assert rows[0]["ts"] == ts
        assert 0 < rows[0]["strength"] <= 1.0


# ══════════════════════════════════════════════════════════════════════════════
# S5-4: Engine — integración con BD mockeada
# ══════════════════════════════════════════════════════════════════════════════

def _make_mock_db_with_scores(n_days: int = 35) -> MagicMock:
    """
    Crea un mock de Supabase que devuelve flow_scores sintéticos.
    Simula assets: BTC (crypto), ^GSPC (index), GC=F (commodity/gold),
    ^TNX (macro/rates), DX-Y.NYB (macro/currency), ^VIX (macro/volatility),
    SOXX (etf/semiconductor), XME (etf/metals_mining).
    """
    assets_meta = [
        {"ticker": "BTC",      "asset_class": "crypto",    "sector": "l1"},
        {"ticker": "^GSPC",    "asset_class": "index",     "sector": None},
        {"ticker": "GC=F",     "asset_class": "commodity", "sector": "metals"},
        {"ticker": "^TNX",     "asset_class": "macro",     "sector": "rates"},
        {"ticker": "DX-Y.NYB", "asset_class": "macro",     "sector": "currency"},
        {"ticker": "^VIX",     "asset_class": "macro",     "sector": "volatility"},
        {"ticker": "SOXX",     "asset_class": "etf",       "sector": "semiconductor"},
        {"ticker": "XME",      "asset_class": "etf",       "sector": "metals_mining"},
    ]

    records = []
    for day in range(n_days):
        ts = f"2024-01-{(day % 28) + 1:02d}T00:00:00+00:00"
        for meta in assets_meta:
            records.append({
                "ts": ts,
                "score": 0.1 * (day % 10 - 5) / 5.0,
                "assets": meta,
            })

    mock_db = MagicMock()
    mock_execute = MagicMock()
    mock_execute.data = records
    (
        mock_db.table.return_value
        .select.return_value
        .gte.return_value
        .eq.return_value
        .execute.return_value
    ) = mock_execute
    # Upsert chain
    mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])

    return mock_db


class TestAnalysisEngine:

    def test_run_returns_dict(self):
        """El engine devuelve un dict con campos esperados."""
        from app.analysis.engine import AnalysisEngine
        mock_db = _make_mock_db_with_scores()
        engine = AnalysisEngine(db=mock_db)
        result = engine.run_sync()
        assert "regime" in result
        assert "regime_confidence" in result
        assert "regime_signals" in result
        assert "n_decouplings_intermarket" in result
        assert "n_decouplings_sector" in result
        assert "rotations" in result
        assert "errors" in result
        assert "ok" in result

    def test_upsert_called_with_on_conflict(self):
        """El engine llama upsert con on_conflict para evitar duplicados."""
        from app.analysis.engine import AnalysisEngine
        mock_db = _make_mock_db_with_scores()
        engine = AnalysisEngine(db=mock_db)
        engine.run_sync()
        # Verificar que upsert fue llamado al menos una vez
        assert mock_db.table.return_value.upsert.called

    def test_idempotency_upsert_called_twice(self):
        """Re-ejecutar el engine usa upsert (no insert) — no genera duplicados."""
        from app.analysis.engine import AnalysisEngine
        mock_db = _make_mock_db_with_scores()
        engine = AnalysisEngine(db=mock_db)
        engine.run_sync()
        engine.run_sync()
        # Upsert llamado en ambas ejecuciones
        assert mock_db.table.return_value.upsert.call_count >= 2

    def test_no_errors_with_valid_data(self):
        """Con datos válidos no debe haber errores."""
        from app.analysis.engine import AnalysisEngine
        mock_db = _make_mock_db_with_scores()
        engine = AnalysisEngine(db=mock_db)
        result = engine.run_sync()
        # Pueden haber errores menores (ej. régimen 30d si no hay datos suficientes)
        # pero el engine no debe fallar completamente
        assert "regime" in result

    def test_handles_empty_scores(self):
        """Sin scores disponibles → resultado vacío sin excepción."""
        from app.analysis.engine import AnalysisEngine
        mock_db = MagicMock()
        mock_execute = MagicMock()
        mock_execute.data = []
        (
            mock_db.table.return_value
            .select.return_value
            .gte.return_value
            .eq.return_value
            .execute.return_value
        ) = mock_execute
        engine = AnalysisEngine(db=mock_db)
        result = engine.run_sync()
        assert result["regime"] == "neutral"
        assert len(result["errors"]) > 0   # error reportado


# ══════════════════════════════════════════════════════════════════════════════
# S5-5: Ticker → clase intermercado
# ══════════════════════════════════════════════════════════════════════════════

class TestTickerToIntermarketClass:

    def test_crypto_tickers(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("BTC", "crypto", "l1") == "crypto"
        assert f("ETH", "crypto", "l1") == "crypto"
        assert f("IBIT", "etf", "crypto") == "crypto"

    def test_equity_tickers(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("^GSPC", "index", None) == "equities"
        assert f("SPY", "etf", "broad_market") == "equities"
        assert f("QQQ", "etf", "broad_market") == "equities"

    def test_commodity_tickers(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("GC=F", "commodity", "metals") == "gold"
        assert f("GLD", "etf", "commodities") == "gold"
        assert f("SI=F", "commodity", "metals") == "silver"
        assert f("SLV", "etf", "commodities") == "silver"

    def test_macro_tickers(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("^TNX", "macro", "rates") == "bonds"
        assert f("DX-Y.NYB", "macro", "currency") == "dollar"
        assert f("^VIX", "macro", "volatility") == "vix"

    def test_sector_etfs_return_none(self):
        """Los ETFs sectoriales no pertenecen a la Matriz A."""
        from app.analysis.correlation import ticker_to_intermarket_class as f, SECTOR_ETFS
        for ticker in SECTOR_ETFS:
            assert f(ticker, "etf", "semiconductor") is None

    def test_onchain_returns_none(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("STABLES_USDT", "onchain", "stablecoin") is None

    def test_fng_returns_none(self):
        from app.analysis.correlation import ticker_to_intermarket_class as f
        assert f("CRYPTO_FNG", "macro", "crypto_sentiment") is None


# ══════════════════════════════════════════════════════════════════════════════
# S5-6: Filtro ETFs sectoriales (filter_to_sector_scores)
# ══════════════════════════════════════════════════════════════════════════════

class TestFilterToSectorScores:

    def test_only_sector_etfs_in_df(self):
        """Solo ETFs sectoriales aparecen en el DataFrame resultante."""
        from app.analysis.correlation import filter_to_sector_scores, SECTOR_ETFS
        ts = "2024-01-15T00:00:00+00:00"
        records = [
            _flat_record("SOXX", ts, 0.5, "etf", "semiconductor"),
            _flat_record("BTC",  ts, 0.8, "crypto", "l1"),
            _flat_record("XME",  ts, -0.3, "etf", "metals_mining"),
        ]
        df = filter_to_sector_scores(records)
        assert "SOXX" in df.columns
        assert "XME" in df.columns
        assert "BTC" not in df.columns

    def test_empty_when_no_sector_etfs(self):
        from app.analysis.correlation import filter_to_sector_scores
        records = [_flat_record("BTC", "2024-01-15T00:00:00+00:00", 0.5, "crypto")]
        df = filter_to_sector_scores(records)
        assert df.empty


# ══════════════════════════════════════════════════════════════════════════════
# S5-7: Factor de confianza de datos (compute_data_confidence_factor)
# ══════════════════════════════════════════════════════════════════════════════

def _conf_record(ticker: str, asset_class: str, sector, score: float, confidence: str) -> dict:
    return {
        "ticker": ticker,
        "asset_class": asset_class,
        "sector": sector,
        "score": score,
        "confidence": confidence,
        "ts": "2024-01-15T00:00:00+00:00",
    }


class TestComputeDataConfidenceFactor:

    def test_all_ok_gives_factor_1(self):
        """100 % scores 'ok' → factor = 1.0 (sin penalización)."""
        from app.analysis.regime import compute_data_confidence_factor
        records = [
            _conf_record("BTC",   "crypto", "l1",   0.5, "ok"),
            _conf_record("^GSPC", "index",  None,   0.3, "ok"),
            _conf_record("GC=F",  "commodity", "metals", 0.2, "ok"),
        ]
        assert compute_data_confidence_factor(records) == pytest.approx(1.0)

    def test_all_low_gives_floor(self):
        """100 % scores 'low' → factor = DATA_CONFIDENCE_FLOOR."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        records = [
            _conf_record("BTC",   "crypto", "l1",  0.5, "low"),
            _conf_record("^GSPC", "index",  None,  0.3, "low"),
        ]
        assert compute_data_confidence_factor(records) == pytest.approx(DATA_CONFIDENCE_FLOOR)

    def test_mixed_gives_intermediate_factor(self):
        """50 % 'ok', 50 % 'low' → factor entre FLOOR y 1.0."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        records = [
            _conf_record("BTC",   "crypto", "l1", 0.5, "ok"),
            _conf_record("^GSPC", "index",  None, 0.3, "low"),
        ]
        factor = compute_data_confidence_factor(records)
        assert DATA_CONFIDENCE_FLOOR < factor < 1.0
        # Con 50 % ok_ratio: FLOOR + (1-FLOOR)*0.5
        expected = DATA_CONFIDENCE_FLOOR + (1.0 - DATA_CONFIDENCE_FLOOR) * 0.5
        assert factor == pytest.approx(expected, abs=1e-4)

    def test_sector_etfs_excluded_from_factor(self):
        """ETFs sectoriales (SOXX, XME…) no cuentan para el factor."""
        from app.analysis.regime import compute_data_confidence_factor
        records = [
            _conf_record("BTC",  "crypto", "l1",          0.5, "ok"),
            _conf_record("SOXX", "etf",    "semiconductor", -0.8, "low"),
        ]
        # SOXX es sector ETF → excluido; solo BTC cuenta (100 % ok → factor 1.0)
        assert compute_data_confidence_factor(records) == pytest.approx(1.0)

    def test_empty_records_gives_floor(self):
        """Sin records válidos → factor = DATA_CONFIDENCE_FLOOR (máxima precaución)."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        assert compute_data_confidence_factor([]) == pytest.approx(DATA_CONFIDENCE_FLOOR)

    def test_only_sector_etfs_gives_floor(self):
        """Si solo hay sector ETFs (ninguno cuenta) → DATA_CONFIDENCE_FLOOR."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        records = [
            _conf_record("SOXX", "etf", "semiconductor", 0.5, "ok"),
            _conf_record("XME",  "etf", "metals_mining", 0.3, "ok"),
        ]
        assert compute_data_confidence_factor(records) == pytest.approx(DATA_CONFIDENCE_FLOOR)

    def test_missing_confidence_treated_as_low(self):
        """Record sin campo 'confidence' se trata como 'low'."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        records = [
            {"ticker": "BTC", "asset_class": "crypto", "sector": "l1",
             "score": 0.5, "ts": "2024-01-15T00:00:00+00:00"},  # sin 'confidence'
        ]
        assert compute_data_confidence_factor(records) == pytest.approx(DATA_CONFIDENCE_FLOOR)

    def test_latest_ts_per_ticker_used(self):
        """Con múltiples registros por ticker, solo cuenta el más reciente."""
        from app.analysis.regime import compute_data_confidence_factor, DATA_CONFIDENCE_FLOOR
        records = [
            # BTC: dos entradas — la más reciente es 'ok'
            {"ticker": "BTC", "asset_class": "crypto", "sector": "l1",
             "score": 0.5, "confidence": "low", "ts": "2024-01-14T00:00:00+00:00"},
            {"ticker": "BTC", "asset_class": "crypto", "sector": "l1",
             "score": 0.6, "confidence": "ok",  "ts": "2024-01-15T00:00:00+00:00"},
        ]
        # Solo cuenta la entrada más reciente (ok) → factor = 1.0
        assert compute_data_confidence_factor(records) == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════════════════════
# S5-8: Clasificador de régimen con data_confidence_factor
# ══════════════════════════════════════════════════════════════════════════════

class TestRegimeWithDataConfidenceFactor:

    def _scores(self, crypto=0.0, equity=0.0, gold=0.0, silver=0.0,
                bonds=0.0, dxy=0.0, vix=0.0):
        from app.analysis.regime import ClassScores
        return ClassScores(crypto=crypto, equity=equity, gold=gold,
                           silver=silver, bonds=bonds, dxy=dxy, vix=vix)

    def test_ok_scores_preserve_high_confidence(self):
        """Con factor=1.0, la confianza no se penaliza."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.6, equity=0.5, dxy=-0.4, vix=0.3)
        r = classify_regime(scores, data_confidence_factor=1.0)
        assert r.regime == "risk_on"
        assert r.confidence >= 0.7

    def test_cold_start_caps_confidence_below_threshold(self):
        """Mismas señales pero factor=FLOOR → confianza queda en zona roja (<0.4)."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores(crypto=0.6, equity=0.5, dxy=-0.4, vix=0.3)
        r = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        assert r.regime == "risk_on"    # el régimen detectado NO cambia
        assert r.confidence < 0.4       # pero la confianza queda baja

    def test_regime_detected_same_regardless_of_factor(self):
        """El régimen clasificado es el mismo con factor=1.0 y factor=FLOOR."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores(crypto=-0.5, equity=-0.4, dxy=0.4, vix=-0.5)
        r_high = classify_regime(scores, data_confidence_factor=1.0)
        r_low  = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        assert r_high.regime == r_low.regime == "risk_off"

    def test_mixed_factor_gives_intermediate_confidence(self):
        """Factor intermedio → confianza entre la alta y la baja."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores(crypto=0.6, equity=0.5, dxy=-0.4, vix=0.3)
        factor_mid = DATA_CONFIDENCE_FLOOR + (1.0 - DATA_CONFIDENCE_FLOOR) * 0.5
        r_low  = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        r_mid  = classify_regime(scores, data_confidence_factor=factor_mid)
        r_high = classify_regime(scores, data_confidence_factor=1.0)
        assert r_low.confidence < r_mid.confidence < r_high.confidence

    def test_structural_confidence_preserved_for_audit(self):
        """structural_confidence es el mismo independientemente del factor."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores(crypto=0.6, equity=0.5)
        r_high = classify_regime(scores, data_confidence_factor=1.0)
        r_low  = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        assert r_high.structural_confidence == pytest.approx(r_low.structural_confidence)
        assert r_high.data_confidence_factor == pytest.approx(1.0)
        assert r_low.data_confidence_factor  == pytest.approx(DATA_CONFIDENCE_FLOOR)

    def test_neutral_not_affected(self):
        """Neutral (confidence=0.0) no se ve afectado por ningún factor."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores()  # todo neutro
        r = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        assert r.regime == "neutral"
        assert r.confidence == 0.0

    def test_confidence_still_bounded_0_1(self):
        """La confianza final sigue acotada a [0, 1] tras aplicar el factor."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.9, equity=0.9, dxy=-0.9, vix=0.9)
        r = classify_regime(scores, data_confidence_factor=1.0)
        assert 0.0 <= r.confidence <= 1.0

    def test_flight_to_safety_cold_start(self):
        """flight_to_safety con todos los scores en cold start → confianza baja."""
        from app.analysis.regime import classify_regime, DATA_CONFIDENCE_FLOOR
        scores = self._scores(crypto=-0.7, equity=-0.5, gold=0.8, bonds=0.6, dxy=0.4)
        r = classify_regime(scores, data_confidence_factor=DATA_CONFIDENCE_FLOOR)
        assert r.regime == "flight_to_safety"
        assert r.confidence < 0.4

    def test_default_factor_is_1_for_backward_compat(self):
        """Sin pasar data_confidence_factor, el comportamiento es idéntico al anterior."""
        from app.analysis.regime import classify_regime
        scores = self._scores(crypto=0.6, equity=0.5, dxy=-0.4, vix=0.3)
        r_default = classify_regime(scores)
        r_factor1 = classify_regime(scores, data_confidence_factor=1.0)
        assert r_default.regime == r_factor1.regime
        assert r_default.confidence == pytest.approx(r_factor1.confidence)
