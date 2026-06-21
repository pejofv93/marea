"""
Tests de MAREA Sesión 4 — Motor de flow scores.
"""

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_series(values: list[float], start: str = "2024-01-01") -> pd.Series:
    """Serie temporal diaria con los valores dados."""
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


def _rows_from_series(series: pd.Series, field: str = "close") -> list[dict]:
    """Convierte pd.Series en lista de dicts simulando raw_snapshots."""
    return [
        {"ts": str(ts.date()) + "T00:00:00+00:00", field: float(v), "extra": {}}
        for ts, v in series.items()
    ]


def _rows_with_extra(series: pd.Series, extra_key: str) -> list[dict]:
    """Filas donde el valor va en extra en vez de en close."""
    return [
        {"ts": str(ts.date()) + "T00:00:00+00:00", "close": None,
         "extra": {extra_key: float(v)}}
        for ts, v in series.items()
    ]


# ── S4-1: zscore.py — matemática ─────────────────────────────────────────────

class TestRollingZscore:
    def test_zscore_known_values(self):
        """z-score del último valor de una serie conocida."""
        from app.scoring.zscore import rolling_zscore
        # Serie: [1, 2, 3, 4, 5] — ventana 5
        # media=3, std=1.581, z(5) = (5-3)/1.581 ≈ 1.265
        s = _make_series([1.0, 2.0, 3.0, 4.0, 5.0])
        zr = rolling_zscore(s, window=5, min_obs=3)
        assert zr.zscore is not None
        assert abs(zr.zscore - (5.0 - 3.0) / s.std(ddof=1)) < 1e-9

    def test_clip_positive(self):
        """Z-score > 1 queda clipeado a 1.0."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series([1.0] * 10 + [100.0])  # outlier extremo
        zr = rolling_zscore(s, window=11, min_obs=5)
        assert zr.score == 1.0
        assert zr.zscore > 1.0   # sin clipear sigue siendo > 1

    def test_clip_negative(self):
        """Z-score < -1 queda clipeado a -1.0."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series([100.0] * 10 + [1.0])  # outlier negativo
        zr = rolling_zscore(s, window=11, min_obs=5)
        assert zr.score == -1.0
        assert zr.zscore < -1.0

    def test_cold_start_low_confidence(self):
        """Menos de MIN_OBS observaciones → confidence='low'."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series([1.0, 2.0, 3.0])   # solo 3 obs
        zr = rolling_zscore(s, window=7, min_obs=10)
        assert zr.confidence == "low"
        assert zr.score is not None  # score existe igualmente (puede usarse)

    def test_sufficient_obs_ok_confidence(self):
        """Con >= MIN_OBS observaciones → confidence='ok'."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series(list(range(1, 16)))   # 15 obs
        zr = rolling_zscore(s, window=15, min_obs=10)
        assert zr.confidence == "ok"

    def test_flat_series_score_zero(self):
        """Serie constante → std=0 → z-score=0, no divide por cero."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series([5.0] * 15)
        zr = rolling_zscore(s, window=15, min_obs=5)
        assert zr.score == 0.0
        assert zr.zscore == 0.0

    def test_single_point_returns_low(self):
        """Solo 1 observación → no se puede calcular std → confidence='low'."""
        from app.scoring.zscore import rolling_zscore
        s = _make_series([42.0])
        zr = rolling_zscore(s, window=7, min_obs=3)
        assert zr.score is None
        assert zr.confidence == "low"

    def test_score_in_range(self):
        """score siempre está en [-1, +1]."""
        from app.scoring.zscore import rolling_zscore
        rng = np.random.default_rng(42)
        for _ in range(50):
            vals = rng.normal(0, 1, size=40).tolist()
            s = _make_series(vals)
            zr = rolling_zscore(s, window=30, min_obs=5)
            if zr.score is not None:
                assert -1.0 <= zr.score <= 1.0


class TestSeriesFromSnapshots:
    def test_close_field(self):
        from app.scoring.zscore import series_from_snapshots
        rows = _rows_from_series(_make_series([1.0, 2.0, 3.0]))
        s = series_from_snapshots(rows, "close")
        assert len(s) == 3
        assert list(s.values) == [1.0, 2.0, 3.0]

    def test_extra_field_fallback(self):
        """Si el campo no está en raíz, busca en extra."""
        from app.scoring.zscore import series_from_snapshots
        rows = _rows_with_extra(_make_series([10.0, 20.0]), extra_key="volume_24h")
        s = series_from_snapshots(rows, "volume_24h")
        assert len(s) == 2
        assert list(s.values) == [10.0, 20.0]

    def test_empty_rows(self):
        from app.scoring.zscore import series_from_snapshots
        s = series_from_snapshots([], "close")
        assert s.empty

    def test_duplicate_ts_kept_last(self):
        from app.scoring.zscore import series_from_snapshots
        rows = [
            {"ts": "2024-01-01T00:00:00+00:00", "close": 1.0, "extra": {}},
            {"ts": "2024-01-01T00:00:00+00:00", "close": 2.0, "extra": {}},
        ]
        s = series_from_snapshots(rows, "close")
        assert len(s) == 1
        assert s.iloc[0] == 2.0


# ── S4-2: Estrategias ─────────────────────────────────────────────────────────

class TestVolumeFlowStrategy:
    def test_inflow_high_volume_price_up(self):
        """Volumen alto + precio subiendo → score positivo."""
        from app.scoring.strategies import VolumeFlowStrategy
        prices = [100.0 + i for i in range(20)]
        vols   = [1_000.0] * 19 + [5_000.0]   # volumen alto hoy
        rows = [
            {"ts": f"2024-01-{i+1:02d}T00:00:00+00:00",
             "close": p, "volume": v, "extra": {}}
            for i, (p, v) in enumerate(zip(prices, vols))
        ]
        sr = VolumeFlowStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score > 0

    def test_outflow_high_volume_price_down(self):
        """Volumen alto + precio cayendo → score negativo."""
        from app.scoring.strategies import VolumeFlowStrategy
        prices = [120.0 - i for i in range(20)]
        vols   = [1_000.0] * 19 + [5_000.0]
        rows = [
            {"ts": f"2024-01-{i+1:02d}T00:00:00+00:00",
             "close": p, "volume": v, "extra": {}}
            for i, (p, v) in enumerate(zip(prices, vols))
        ]
        sr = VolumeFlowStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score < 0

    def test_score_range(self):
        from app.scoring.strategies import VolumeFlowStrategy
        rows = _rows_from_series(_make_series([float(i) for i in range(1, 31)]), "volume")
        for r in rows:
            r["close"] = float(r.get("volume") or 1)
            r["extra"] = {}
        sr = VolumeFlowStrategy().compute(rows, window=30, min_obs=5)
        if sr.score is not None:
            assert -1.0 <= sr.score <= 1.0


class TestBondYieldStrategy:
    def test_yield_up_gives_negative_score(self):
        """Rendimiento sube → outflow de bonos → score negativo."""
        from app.scoring.strategies import BondYieldStrategy
        # Rendimiento subiendo monotónicamente
        yields = [3.0 + i * 0.05 for i in range(20)]
        rows = _rows_from_series(_make_series(yields))
        sr = BondYieldStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score < 0, f"Esperado negativo, got {sr.score}"

    def test_yield_down_gives_positive_score(self):
        """Rendimiento baja → precio bono sube → inflow → score positivo."""
        from app.scoring.strategies import BondYieldStrategy
        yields = [5.0 - i * 0.05 for i in range(20)]
        rows = _rows_from_series(_make_series(yields))
        sr = BondYieldStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score > 0, f"Esperado positivo, got {sr.score}"

    def test_proxy_name_correct(self):
        from app.scoring.strategies import BondYieldStrategy
        rows = _rows_from_series(_make_series([4.0] * 15))
        sr = BondYieldStrategy().compute(rows, window=15, min_obs=5)
        assert "inverted" in sr.proxy_used


class TestStablecoinSupplyStrategy:
    def test_supply_mint_positive(self):
        """
        Hoy hay un mint anómalo (cambio grande y positivo vs baseline pequeño).
        El z-score de los cambios debe ser positivo → score positivo.
        """
        from app.scoring.strategies import StablecoinSupplyStrategy
        # Baseline: cambios pequeños (+0.1e9/día), luego hoy +5e9 (anomalía)
        base_supply = [100e9 + i * 0.1e9 for i in range(19)]
        spike_today = base_supply[-1] + 5e9
        supply = base_supply + [spike_today]
        rows = _rows_from_series(_make_series(supply))
        sr = StablecoinSupplyStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score > 0, f"Esperado positivo (mint anómalo), got {sr.score}"

    def test_supply_burn_negative(self):
        """
        Hoy hay un burn anómalo (cambio grande y negativo vs baseline pequeño).
        El z-score de los cambios debe ser negativo → score negativo.
        """
        from app.scoring.strategies import StablecoinSupplyStrategy
        # Baseline: cambios positivos pequeños, luego hoy -5e9 (anomalía)
        base_supply = [100e9 + i * 0.1e9 for i in range(19)]
        burn_today = base_supply[-1] - 5e9
        supply = base_supply + [burn_today]
        rows = _rows_from_series(_make_series(supply))
        sr = StablecoinSupplyStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score < 0, f"Esperado negativo (burn anómalo), got {sr.score}"

    def test_insufficient_data(self):
        """Solo 1 observación → no se puede calcular diff → low."""
        from app.scoring.strategies import StablecoinSupplyStrategy
        rows = _rows_from_series(_make_series([100e9]))
        sr = StablecoinSupplyStrategy().compute(rows, window=7, min_obs=5)
        assert sr.confidence == "low"


class TestFearGreedStrategy:
    def test_extreme_fear_maps_to_minus_one(self):
        from app.scoring.strategies import FearGreedStrategy
        rows = _rows_from_series(_make_series([0.0] * 15))
        sr = FearGreedStrategy().compute(rows, window=7, min_obs=5)
        assert sr.score == pytest.approx(-1.0)

    def test_extreme_greed_maps_to_plus_one(self):
        from app.scoring.strategies import FearGreedStrategy
        rows = _rows_from_series(_make_series([100.0] * 15))
        sr = FearGreedStrategy().compute(rows, window=7, min_obs=5)
        assert sr.score == pytest.approx(1.0)

    def test_neutral_maps_to_zero(self):
        from app.scoring.strategies import FearGreedStrategy
        rows = _rows_from_series(_make_series([50.0] * 15))
        sr = FearGreedStrategy().compute(rows, window=7, min_obs=5)
        assert sr.score == pytest.approx(0.0)

    def test_arbitrary_value_in_range(self):
        from app.scoring.strategies import FearGreedStrategy
        rows = _rows_from_series(_make_series([72.0] * 15))
        sr = FearGreedStrategy().compute(rows, window=7, min_obs=5)
        assert sr.score is not None
        assert -1.0 <= sr.score <= 1.0


class TestVIXStrategy:
    def test_high_vix_gives_negative_score(self):
        """VIX sube → miedo → risk-off → score negativo."""
        from app.scoring.strategies import VIXStrategy
        vix_vals = [15.0 + i * 0.5 for i in range(20)]
        rows = _rows_from_series(_make_series(vix_vals))
        sr = VIXStrategy().compute(rows, window=20, min_obs=5)
        assert sr.score is not None
        assert sr.score < 0


class TestGetStrategy:
    def test_index_returns_volume(self):
        from app.scoring.strategies import get_strategy, VolumeFlowStrategy
        assert isinstance(get_strategy("index", None), VolumeFlowStrategy)

    def test_etf_returns_volume(self):
        from app.scoring.strategies import get_strategy, VolumeFlowStrategy
        assert isinstance(get_strategy("etf", "broad_market"), VolumeFlowStrategy)

    def test_stock_returns_volume(self):
        from app.scoring.strategies import get_strategy, VolumeFlowStrategy
        assert isinstance(get_strategy("stock", None), VolumeFlowStrategy)

    def test_macro_rates_returns_bond(self):
        from app.scoring.strategies import get_strategy, BondYieldStrategy
        assert isinstance(get_strategy("macro", "rates"), BondYieldStrategy)

    def test_macro_volatility_returns_vix(self):
        from app.scoring.strategies import get_strategy, VIXStrategy
        assert isinstance(get_strategy("macro", "volatility"), VIXStrategy)

    def test_macro_fng_returns_feargreed(self):
        from app.scoring.strategies import get_strategy, FearGreedStrategy
        assert isinstance(get_strategy("macro", "crypto_sentiment"), FearGreedStrategy)

    def test_onchain_returns_stablecoin(self):
        from app.scoring.strategies import get_strategy, StablecoinSupplyStrategy
        assert isinstance(get_strategy("onchain", "stablecoin"), StablecoinSupplyStrategy)

    def test_crypto_returns_crypto(self):
        from app.scoring.strategies import get_strategy, CryptoVolumeStrategy
        assert isinstance(get_strategy("crypto", "l1"), CryptoVolumeStrategy)


# ── S4-3: ScoreEngine — integración con BD mock ───────────────────────────────

def _make_engine_db(assets: list[dict], snapshots: list[dict]) -> MagicMock:
    """
    Mock del cliente Supabase para ScoreEngine.
    assets_table: .select().eq("is_active", True).execute().data = assets
    snapshots_table: .select().eq().order().limit().execute().data = snapshots
    """
    mock_db = MagicMock()
    assets_mock = MagicMock()
    snapshots_mock = MagicMock()
    scores_mock = MagicMock()

    def _table(name):
        if name == "assets":
            return assets_mock
        if name == "raw_snapshots":
            return snapshots_mock
        return scores_mock   # flow_scores

    mock_db.table.side_effect = _table

    # assets query: .select().eq().execute()
    assets_mock.select.return_value.eq.return_value.execute.return_value.data = assets

    # snapshots query: .select().eq().order().limit().execute()
    (snapshots_mock.select.return_value
     .eq.return_value
     .order.return_value
     .limit.return_value
     .execute.return_value.data) = snapshots

    return mock_db, scores_mock


class TestScoreEngine:
    def test_scores_computed_for_active_assets(self):
        """Motor calcula 2 scores (7d + 30d) por cada asset activo."""
        from app.scoring.engine import ScoreEngine

        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": "broad_market"}]
        # 30 días de datos con volumen y close
        snaps = [
            {"ts": f"2024-{m:02d}-{d:02d}T00:00:00+00:00",
             "close": 400.0 + i, "volume": 10_000_000.0 + i * 100_000,
             "extra": {}}
            for i, (m, d) in enumerate(
                [(1, day) for day in range(1, 32)]
            )
        ]
        mock_db, scores_mock = _make_engine_db(assets, snaps)
        result = ScoreEngine(db=mock_db, min_obs=5).run_sync()

        assert result["scores_computed"] == 2    # 7d + 30d
        assert "SPY" in result["by_asset"]

    def test_low_confidence_counted(self):
        """Assets con pocos datos se cuentan en low_confidence."""
        from app.scoring.engine import ScoreEngine

        assets = [{"id": 1, "ticker": "NEWCOIN", "asset_class": "crypto", "sector": "l1"}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": float(d), "volume": float(d * 1000), "extra": {}}
            for d in range(1, 4)   # solo 3 días de datos
        ]
        mock_db, _ = _make_engine_db(assets, snaps)
        result = ScoreEngine(db=mock_db, min_obs=10).run_sync()

        assert result["low_confidence"] > 0

    def test_no_active_assets_returns_empty(self):
        """Sin assets activos → resultado vacío sin errores."""
        from app.scoring.engine import ScoreEngine

        mock_db, _ = _make_engine_db([], [])
        result = ScoreEngine(db=mock_db).run_sync()

        assert result["scores_computed"] == 0
        assert result["errors"] == []

    def test_upsert_called_on_scores_table(self):
        """Motor hace upsert en flow_scores con conflict on (asset_id, ts, window)."""
        from app.scoring.engine import ScoreEngine

        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": None}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": float(d * 10), "volume": float(d * 1_000_000), "extra": {}}
            for d in range(1, 32)
        ]
        mock_db, scores_mock = _make_engine_db(assets, snaps)
        ScoreEngine(db=mock_db, min_obs=5).run_sync()

        scores_mock.upsert.assert_called()
        call_kwargs = scores_mock.upsert.call_args
        # segundo arg es on_conflict
        assert "asset_id,ts,win" in str(call_kwargs)

    def test_asset_error_does_not_abort_others(self):
        """Si un asset falla, los demás continúan procesándose."""
        from app.scoring.engine import ScoreEngine

        assets = [
            {"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": None},
            {"id": 2, "ticker": "QQQ", "asset_class": "etf", "sector": None},
        ]

        mock_db = MagicMock()
        assets_mock = MagicMock()
        snapshots_mock = MagicMock()
        scores_mock = MagicMock()

        def _table(name):
            if name == "assets":
                return assets_mock
            if name == "raw_snapshots":
                return snapshots_mock
            return scores_mock

        mock_db.table.side_effect = _table
        assets_mock.select.return_value.eq.return_value.execute.return_value.data = assets

        snaps_ok = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": float(d * 10), "volume": float(d * 1e6), "extra": {}}
            for d in range(1, 32)
        ]

        call_count = [0]
        def _snap_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("BD caída")  # SPY falla
            return MagicMock(data=snaps_ok)   # QQQ OK

        (snapshots_mock.select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.side_effect) = _snap_side_effect

        result = ScoreEngine(db=mock_db, min_obs=5).run_sync()

        assert any("SPY" in e for e in result["errors"])
        assert "QQQ" in result["by_asset"]

    def test_idempotent_upsert(self):
        """Llamar run_sync dos veces no duplica filas (on_conflict maneja idempotencia)."""
        from app.scoring.engine import ScoreEngine

        assets = [{"id": 1, "ticker": "GLD", "asset_class": "etf", "sector": "commodities"}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": float(d * 2), "volume": float(d * 500_000), "extra": {}}
            for d in range(1, 32)
        ]
        mock_db, scores_mock = _make_engine_db(assets, snaps)
        engine = ScoreEngine(db=mock_db, min_obs=5)
        engine.run_sync()
        engine.run_sync()

        # Ambas llamadas hacen upsert — la BD maneja el conflicto
        # Aquí verificamos que no lanza excepciones y que upsert se llamó
        assert scores_mock.upsert.call_count >= 2
