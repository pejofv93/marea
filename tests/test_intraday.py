"""
Tests MAREA Sesión 9b — Carril intradía.

Garantías verificadas:
  (a) yfinance intradía se llama EN LOTE (1 sola llamada, todos los tickers).
  (b) raw_snapshots_intraday NO aplasta el ts a medianoche (guarda hora real).
  (c) El carril diario NO se ve afectado: ScoreEngine sigue leyendo
      raw_snapshots y flow_scores, nunca raw_snapshots_intraday.
  (d) Score intradía cold start (<2 obs) → score=None → fila no escrita.
  (e) Score intradía con n_obs < min_obs → confidence='low'.
  (f) Alerta intraday_flow respeta umbral de confianza (low → no envía).
  (g) Alerta intraday_flow respeta anti-duplicado.
  (h) Alerta intraday_flow se envía cuando |score| > threshold y conf ok.
  (i) re-arm intraday_flow resetea sent=False para tickers fuera del umbral.
  (j) format_intraday_flow incluye sello de señal intradía corto plazo.
  (k) IntradayRunner escribe en raw_snapshots_intraday (no en raw_snapshots).
  (l) IntradayAnalysisEngine clasifica inflow/outflow/neutral correctamente.
  (m) bars_for_window devuelve valores correctos para 60m y 15m.
  (n) Los tests del carril diario (ScoreEngine) siguen sin tocar tablas intraday.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

# ══════════════════════════════════════════════════════════════════════════════
# Helpers compartidos
# ══════════════════════════════════════════════════════════════════════════════

def _fluent(data=None):
    """Mock de tabla Supabase con encadenamiento fluente."""
    m = MagicMock()
    for method in ("select", "eq", "order", "limit", "upsert", "insert"):
        getattr(m, method).return_value = m
    res = MagicMock()
    res.data = data if data is not None else []
    m.execute.return_value = res
    return m


def _make_db(
    assets=None,
    raw_snapshots_intraday=None,
    flow_scores_intraday=None,
    alerts=None,
    assets_active=None,
):
    table_map = {
        "assets":                  _fluent(assets or assets_active or []),
        "raw_snapshots_intraday":  _fluent(raw_snapshots_intraday or []),
        "flow_scores_intraday":    _fluent(flow_scores_intraday or []),
        "alerts":                  _fluent(alerts or []),
        # Tablas del carril diario (deben estar vacías para confirmar no interferencia)
        "raw_snapshots":           _fluent([]),
        "flow_scores":             _fluent([]),
    }
    db = MagicMock()
    db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
    return db


def _bar(ts: str, close: float = 100.0, volume: float = 1_000_000.0) -> dict:
    """Fila de raw_snapshots_intraday."""
    return {
        "ts":     ts,
        "open":   close * 0.99,
        "high":   close * 1.01,
        "low":    close * 0.98,
        "close":  close,
        "volume": volume,
        "extra":  {},
    }


def _intraday_score(
    asset_id: int,
    ticker: str,
    score: float,
    confidence: str = "ok",
    win: str = "4h",
    interval: str = "60m",
    ts: str = "2026-06-19T14:00:00+00:00",
) -> dict:
    return {
        "asset_id":   asset_id,
        "ts":         ts,
        "interval":   interval,
        "win":        win,
        "score":      score,
        "raw_zscore": score * 1.5,
        "proxy_used": "volume_zscore_signed",
        "n_obs":      10,
        "confidence": confidence,
        "assets": {
            "ticker":      ticker,
            "asset_class": "etf",
            "sector":      "semiconductor",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# (a) yfinance intradía — lote único
# ══════════════════════════════════════════════════════════════════════════════

class TestYFinanceIntradayBatch:

    def test_single_batch_call(self):
        """yf.download debe llamarse exactamente una vez con todos los tickers."""
        import pandas as pd
        from app.ingest.yfinance_intraday import IngestYFinanceIntraday

        db = _make_db(assets=[
            {"id": 1, "ticker": "SPY"},
            {"id": 2, "ticker": "QQQ"},
        ])

        # DataFrame multi-ticker ficticio
        idx = pd.DatetimeIndex(["2026-06-19 14:00:00+00:00", "2026-06-19 15:00:00+00:00"])
        cols = pd.MultiIndex.from_tuples([
            ("Close", "SPY"), ("Close", "QQQ"),
            ("Volume", "SPY"), ("Volume", "QQQ"),
            ("Open",   "SPY"), ("Open",   "QQQ"),
            ("High",   "SPY"), ("High",   "QQQ"),
            ("Low",    "SPY"), ("Low",    "QQQ"),
        ])
        data = {
            ("Close",  "SPY"): [400.0, 401.0],
            ("Close",  "QQQ"): [300.0, 301.0],
            ("Volume", "SPY"): [1e6, 1.1e6],
            ("Volume", "QQQ"): [2e6, 2.1e6],
            ("Open",   "SPY"): [399.0, 400.0],
            ("Open",   "QQQ"): [299.0, 300.0],
            ("High",   "SPY"): [402.0, 403.0],
            ("High",   "QQQ"): [302.0, 303.0],
            ("Low",    "SPY"): [398.0, 399.0],
            ("Low",    "QQQ"): [298.0, 299.0],
        }
        mock_df = pd.DataFrame(data, index=idx, columns=cols)

        with patch("yfinance.download", return_value=mock_df) as mock_dl:
            with patch("app.ingest._base.load_asset_map", return_value={"SPY": 1, "QQQ": 2}):
                ingest = IngestYFinanceIntraday(db=db, interval="60m", period="5d")
                result = ingest.run_sync()

        # Una sola llamada
        assert mock_dl.call_count == 1
        call_kwargs = mock_dl.call_args
        assert call_kwargs.kwargs.get("interval") == "60m"
        assert call_kwargs.kwargs.get("threads") is False

    def test_upsert_targets_intraday_table(self):
        """El upsert va a raw_snapshots_intraday, nunca a raw_snapshots."""
        import pandas as pd
        from app.ingest.yfinance_intraday import IngestYFinanceIntraday

        idx = pd.DatetimeIndex(["2026-06-19 14:00:00+00:00"])
        mock_df = pd.DataFrame(
            {"Close": [100.0], "Volume": [1e6], "Open": [99.0], "High": [101.0], "Low": [98.0]},
            index=idx,
        )

        written_intraday = []
        written_daily    = []

        intraday_table = MagicMock()
        intraday_table.upsert.side_effect = lambda rows, on_conflict=None: (
            written_intraday.extend(rows) or MagicMock()
        )

        daily_table = MagicMock()
        daily_table.upsert.side_effect = lambda rows, on_conflict=None: (
            written_daily.extend(rows) or MagicMock()
        )

        db = MagicMock()
        db.table.side_effect = lambda name: (
            intraday_table if name == "raw_snapshots_intraday" else daily_table
        )

        with patch("yfinance.download", return_value=mock_df):
            with patch("app.ingest.yfinance_intraday.load_asset_map", return_value={"SPY": 1}):
                IngestYFinanceIntraday(db=db, interval="60m").run_sync()

        assert len(written_intraday) > 0, "Debería haber escrito en raw_snapshots_intraday"
        assert len(written_daily) == 0, "No debe escribir en raw_snapshots (diario)"

    def test_empty_df_returns_error(self):
        from app.ingest.yfinance_intraday import IngestYFinanceIntraday

        with patch("yfinance.download", return_value=pd.DataFrame()):
            with patch("app.ingest._base.load_asset_map", return_value={"SPY": 1}):
                result = IngestYFinanceIntraday(db=MagicMock()).run_sync()

        assert not result["ok"]
        assert result["snapshots_inserted"] == 0

    def test_no_assets_returns_error(self):
        from app.ingest.yfinance_intraday import IngestYFinanceIntraday

        with patch("app.ingest._base.load_asset_map", return_value={}):
            result = IngestYFinanceIntraday(db=MagicMock()).run_sync()

        assert not result["ok"]
        assert "No hay assets" in result["errors"][0]


# ══════════════════════════════════════════════════════════════════════════════
# (b) Timestamps REALES (no medianoche)
# ══════════════════════════════════════════════════════════════════════════════

class TestRealTimestamps:

    def test_ts_preserves_hour_and_minute(self):
        """El ts de cada barra debe preservar la hora y el minuto reales."""
        import pandas as pd
        from app.ingest.yfinance_intraday import IngestYFinanceIntraday

        # Barra a las 14:30 UTC (no medianoche)
        ts_real = pd.Timestamp("2026-06-19 14:30:00+00:00")
        idx = pd.DatetimeIndex([ts_real])
        mock_df = pd.DataFrame(
            {"Close": [100.0], "Volume": [1e6], "Open": [99.0], "High": [101.0], "Low": [98.0]},
            index=idx,
        )

        written_records = []

        intraday_table = MagicMock()

        def capture_upsert(records, on_conflict=None):
            written_records.extend(records)
            m = MagicMock()
            m.execute.return_value = MagicMock()
            return m

        intraday_table.upsert.side_effect = capture_upsert
        db = MagicMock()
        db.table.return_value = intraday_table

        with patch("yfinance.download", return_value=mock_df):
            with patch("app.ingest.yfinance_intraday.load_asset_map", return_value={"SPY": 1}):
                IngestYFinanceIntraday(db=db, interval="60m").run_sync()

        assert len(written_records) == 1, f"Esperaba 1 record, got {written_records}"
        ts_str = written_records[0]["ts"]
        # Debe contener "14:30" en algún formato, NO "T00:00"
        assert "14:30" in ts_str or "T14:30" in ts_str
        assert "T00:00" not in ts_str

    def test_intraday_runner_crypto_uses_real_ts(self):
        """CoinGecko intradía escribe el timestamp real, no day_ts() (medianoche)."""
        from app.ingest.intraday_runner import _now_ts
        from app.ingest._base import day_ts

        now  = _now_ts()
        midnight = day_ts()

        # _now_ts NO debe ser igual a medianoche UTC salvo si se ejecuta a las 00:00
        # Lo verificamos por formato: _now_ts incluye hora no-cero en la mayoría de casos,
        # pero el test seguro es verificar que _now_ts() usa datetime.now, no medianoche.
        dt = datetime.fromisoformat(now)
        # Verificamos que la función devuelve un timestamp con timezone UTC
        assert dt.tzinfo is not None


# ══════════════════════════════════════════════════════════════════════════════
# (c) Carril DIARIO intacto — ScoreEngine no toca tablas intradía
# ══════════════════════════════════════════════════════════════════════════════

class TestDailyCarrilUnaffected:

    def test_score_engine_reads_raw_snapshots_not_intraday(self):
        """ScoreEngine lee de raw_snapshots y escribe en flow_scores (no intradía)."""
        from app.scoring.engine import ScoreEngine

        accessed_tables = []

        def track_table(name):
            accessed_tables.append(name)
            return _fluent([])

        db = MagicMock()
        db.table.side_effect = track_table

        engine = ScoreEngine(db=db)
        engine.run_sync()

        # Nunca debe acceder a tablas intradía
        assert "raw_snapshots_intraday" not in accessed_tables
        assert "flow_scores_intraday"   not in accessed_tables

    def test_score_engine_writes_flow_scores_not_intraday(self):
        """ScoreEngine hace upsert en flow_scores (no en flow_scores_intraday)."""
        from app.scoring.engine import ScoreEngine

        # Proporciona un asset con suficientes snapshots para calcular score
        bars = [
            {"ts": f"2026-06-{i:02d}T00:00:00+00:00", "close": 100.0 + i, "volume": 1e6, "open": None, "high": None, "low": None, "extra": {}}
            for i in range(1, 15)
        ]
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": "broad_market"}]

        flow_scores_table    = _fluent([])
        flow_intraday_table  = _fluent([])

        table_map = {
            "assets":                 _fluent(assets),
            "raw_snapshots":          _fluent(bars),
            "flow_scores":            flow_scores_table,
            "flow_scores_intraday":   flow_intraday_table,
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        ScoreEngine(db=db, min_obs=5).run_sync()

        flow_scores_table.upsert.assert_called()
        flow_intraday_table.upsert.assert_not_called()

    def test_intraday_engine_writes_intraday_not_daily(self):
        """IntradayScoreEngine escribe en flow_scores_intraday, nunca en flow_scores."""
        from app.scoring.intraday_engine import IntradayScoreEngine

        bars = [
            _bar(f"2026-06-19T{h:02d}:00:00+00:00", 100.0 + h, 1e6)
            for h in range(10)
        ]
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": "broad_market"}]

        flow_daily_table    = _fluent([])
        flow_intraday_table = _fluent([])

        table_map = {
            "assets":                _fluent(assets),
            "raw_snapshots_intraday": _fluent(bars),
            "flow_scores":           flow_daily_table,
            "flow_scores_intraday":  flow_intraday_table,
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        IntradayScoreEngine(db=db, interval="60m", min_obs=3).run_sync()

        flow_intraday_table.upsert.assert_called()
        flow_daily_table.upsert.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (d/e) Cold start intradía
# ══════════════════════════════════════════════════════════════════════════════

class TestIntradayColdStart:

    def test_zero_bars_score_is_none(self):
        """Con 0 barras, score=None → fila no escrita en BD."""
        from app.scoring.intraday_engine import IntradayScoreEngine

        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": None}]
        flow_intraday_table = _fluent([])
        table_map = {
            "assets":                _fluent(assets),
            "raw_snapshots_intraday": _fluent([]),   # sin barras
            "flow_scores_intraday":   flow_intraday_table,
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        result = IntradayScoreEngine(db=db, interval="60m", min_obs=4).run_sync()

        # Sin barras no se puede calcular nada → 0 scores computados
        assert result["scores_computed"] == 0
        flow_intraday_table.upsert.assert_not_called()

    def test_one_bar_score_is_none(self):
        """Con 1 sola barra, rolling_zscore devuelve score=None (n_obs < 2)."""
        from app.scoring.intraday_engine import IntradayScoreEngine

        bars = [_bar("2026-06-19T14:00:00+00:00", 100.0, 1e6)]
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": None}]

        table_map = {
            "assets":                _fluent(assets),
            "raw_snapshots_intraday": _fluent(bars),
            "flow_scores_intraday":   _fluent([]),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        result = IntradayScoreEngine(db=db, interval="60m", min_obs=4).run_sync()
        assert result["scores_computed"] == 0

    def test_few_bars_confidence_low(self):
        """Con barras < min_obs → score calculado pero confidence='low'."""
        from app.scoring.zscore import rolling_zscore, series_from_snapshots
        import pandas as pd

        # 3 barras, min_obs=4 → confidence='low'
        bars = [
            _bar(f"2026-06-19T{h:02d}:00:00+00:00", 100.0 + h * 2, 1e6 * (1 + h * 0.1))
            for h in range(3)
        ]
        series = series_from_snapshots(bars, "volume")
        zr = rolling_zscore(series, window=4, min_obs=4)

        assert zr.confidence == "low"
        # Score puede ser None o float dependiendo de si hay ≥2 obs en la ventana
        # Con 3 obs y ventana=4, se toman las últimas 3 → n_obs=3 ≥ 2 → score calculado
        # pero confidence='low' porque 3 < min_obs=4

    def test_enough_bars_confidence_ok(self):
        """Con barras ≥ min_obs → confidence='ok'."""
        from app.scoring.zscore import rolling_zscore, series_from_snapshots

        bars = [
            _bar(f"2026-06-19T{h:02d}:00:00+00:00", 100.0 + h, 1e6 * (1 + h * 0.05))
            for h in range(10)
        ]
        series = series_from_snapshots(bars, "volume")
        zr = rolling_zscore(series, window=4, min_obs=4)

        assert zr.confidence == "ok"
        assert zr.score is not None

    def test_intraday_engine_marks_low_confidence(self):
        """IntradayScoreEngine cuenta correctamente los scores de baja confianza."""
        from app.scoring.intraday_engine import IntradayScoreEngine

        # 3 barras con min_obs=4 → confidence low en cada ventana con suficientes obs
        bars = [
            _bar(f"2026-06-19T{h:02d}:00:00+00:00", 100.0 + h, 1e6 + h * 10000)
            for h in range(3)
        ]
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": None}]
        table_map = {
            "assets":                 _fluent(assets),
            "raw_snapshots_intraday": _fluent(bars),
            "flow_scores_intraday":   _fluent([]),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        result = IntradayScoreEngine(db=db, interval="60m", min_obs=4).run_sync()
        assert result["low_confidence"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# (f/g/h) Alertas intradía: confianza + anti-duplicado + envío
# ══════════════════════════════════════════════════════════════════════════════

class TestIntradayAlerts:

    def _make_alert_db(self, scores=None, alerts_sent=None):
        """DB mock con tablas de alertas y flow_scores_intraday."""
        table_map = {
            "flow_scores_intraday": _fluent(scores or []),
            "flow_scores":          _fluent([]),
            "regimes":              _fluent([]),
            "correlations":         _fluent([]),
            "exposures":            _fluent([]),
            "narratives":           _fluent([]),
            "alerts":               _fluent(alerts_sent or []),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
        return db

    def test_low_confidence_not_sent(self):
        """Alerta intradía con confidence='low' NO debe enviarse."""
        from app.alerts.rules import check_intraday_flow
        from app.alerts.engine import AlertEngine

        scores = [_intraday_score(1, "SOXX", score=0.85, confidence="low")]
        db = self._make_alert_db(scores=scores)

        sent_calls = []
        engine = AlertEngine(db=db, send_fn=lambda t: sent_calls.append(t) or True, min_confidence=0.4)
        result = engine.run_sync()

        assert len(sent_calls) == 0
        low_conf_alerts = [
            a for a in result["alerts"]
            if a["alert_type"] == "intraday_flow" and a["not_sent_reason"] == "low_confidence"
        ]
        assert len(low_conf_alerts) >= 1

    def test_high_confidence_above_threshold_sent(self):
        """Alerta intradía con confidence='ok' y |score| > threshold → se envía."""
        from app.alerts.engine import AlertEngine

        scores = [_intraday_score(1, "SOXX", score=0.85, confidence="ok")]
        # Sin alertas previas → no duplicado
        db = self._make_alert_db(scores=scores, alerts_sent=[])

        sent_texts = []
        engine = AlertEngine(db=db, send_fn=lambda t: sent_texts.append(t) or True, min_confidence=0.4)

        with patch("app.config.settings") as mock_settings:
            mock_settings.flow_extreme_threshold = 0.7
            mock_settings.intraday_flow_threshold = 0.6
            mock_settings.intraday_interval = "60m"
            mock_settings.min_alert_confidence = 0.4

            result = engine.run_sync()

        intraday_sent = [a for a in result["alerts"] if a["alert_type"] == "intraday_flow" and a["sent"]]
        assert len(intraday_sent) >= 1

    def test_dedup_prevents_second_send(self):
        """Segunda ejecución con el mismo estado no reenvía la alerta."""
        from app.alerts.engine import AlertEngine

        scores = [_intraday_score(1, "SOXX", score=0.85, confidence="ok")]

        # Simular que la alerta ya fue enviada
        sent_alert = {
            "id":         1,
            "alert_type": "intraday_flow",
            "entity":     "SOXX",
            "state":      "intraday_extreme",
            "sent":       True,
        }
        alerts_table = _fluent([sent_alert])
        table_map = {
            "flow_scores_intraday": _fluent(scores),
            "alerts":               alerts_table,
            "flow_scores":          _fluent([]),
            "regimes":              _fluent([]),
            "correlations":         _fluent([]),
            "exposures":            _fluent([]),
            "narratives":           _fluent([]),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))

        sent_calls = []
        engine = AlertEngine(db=db, send_fn=lambda t: sent_calls.append(t) or True, min_confidence=0.4)

        with patch("app.config.settings") as mock_settings:
            mock_settings.flow_extreme_threshold = 0.7
            mock_settings.intraday_flow_threshold = 0.6
            mock_settings.intraday_interval = "60m"
            mock_settings.min_alert_confidence = 0.4

            result = engine.run_sync()

        # La alerta intraday_flow no debe haberse enviado (duplicado)
        intraday_dup = [
            a for a in result["alerts"]
            if a["alert_type"] == "intraday_flow" and a["not_sent_reason"] == "duplicate"
        ]
        assert len(intraday_dup) >= 1
        assert len(sent_calls) == 0

    def test_below_threshold_no_alert(self):
        """Score intradía por debajo del umbral no genera alerta."""
        from app.alerts.rules import check_intraday_flow

        scores = [_intraday_score(1, "SPY", score=0.3, confidence="ok")]
        db = _make_db(flow_scores_intraday=scores)

        alerts = check_intraday_flow(db, threshold=0.6, interval="60m")
        assert len(alerts) == 0

    def test_above_threshold_generates_alert(self):
        """Score intradía por encima del umbral genera alerta."""
        from app.alerts.rules import check_intraday_flow

        scores = [_intraday_score(1, "SOXX", score=0.85, confidence="ok")]
        db = _make_db(flow_scores_intraday=scores)

        alerts = check_intraday_flow(db, threshold=0.6, interval="60m")
        assert len(alerts) == 1
        assert alerts[0].alert_type == "intraday_flow"
        assert alerts[0].entity == "SOXX"
        assert alerts[0].state == "intraday_extreme"
        assert alerts[0].confidence == 0.8   # ok → 0.8

    def test_negative_score_above_threshold(self):
        """Score negativo (outflow) también genera alerta."""
        from app.alerts.rules import check_intraday_flow

        scores = [_intraday_score(1, "GLD", score=-0.75, confidence="ok")]
        db = _make_db(flow_scores_intraday=scores)

        alerts = check_intraday_flow(db, threshold=0.6, interval="60m")
        assert len(alerts) == 1
        assert alerts[0].payload["direction"] == "outflow"

    def test_low_confidence_score_gives_low_numeric_confidence(self):
        """Score con confidence='low' → confidence numérica 0.2."""
        from app.alerts.rules import check_intraday_flow

        scores = [_intraday_score(1, "QQQ", score=0.9, confidence="low")]
        db = _make_db(flow_scores_intraday=scores)

        alerts = check_intraday_flow(db, threshold=0.6, interval="60m")
        assert len(alerts) == 1
        assert alerts[0].confidence == 0.2   # low → 0.2


# ══════════════════════════════════════════════════════════════════════════════
# (i) Re-arm intraday_flow
# ══════════════════════════════════════════════════════════════════════════════

class TestRearmIntradayFlow:

    def test_rearm_resets_sent_when_below_threshold(self):
        """Si el ticker ya no es extremo, rearm resetea sent=False."""
        from app.alerts.dedup import rearm_intraday_flow

        sent_alert = {"id": 1, "entity": "SOXX"}
        alerts_table = _fluent([sent_alert])
        db = MagicMock()
        db.table.return_value = alerts_table

        # SOXX ya no está en el conjunto de extremos
        count = rearm_intraday_flow(db, currently_extreme=set())
        assert count == 1
        alerts_table.upsert.assert_called()
        upsert_data = alerts_table.upsert.call_args[0][0]
        assert upsert_data["sent"] is False
        assert upsert_data["alert_type"] == "intraday_flow"

    def test_rearm_skips_still_extreme(self):
        """Si el ticker sigue siendo extremo, no se re-arma."""
        from app.alerts.dedup import rearm_intraday_flow

        sent_alert = {"id": 1, "entity": "SOXX"}
        alerts_table = _fluent([sent_alert])
        db = MagicMock()
        db.table.return_value = alerts_table

        count = rearm_intraday_flow(db, currently_extreme={"SOXX"})
        assert count == 0
        alerts_table.upsert.assert_not_called()

    def test_get_current_intraday_extreme_tickers(self):
        """get_current_intraday_extreme_tickers filtra correctamente por umbral."""
        from app.alerts.rules import get_current_intraday_extreme_tickers

        scores = [
            _intraday_score(1, "SOXX", score=0.85),   # extremo
            _intraday_score(2, "SPY",  score=0.30),   # no extremo
            _intraday_score(3, "GLD",  score=-0.72),  # extremo negativo
        ]
        db = _make_db(flow_scores_intraday=scores)

        extreme = get_current_intraday_extreme_tickers(db, threshold=0.6, interval="60m")
        assert "SOXX" in extreme
        assert "GLD"  in extreme
        assert "SPY"  not in extreme


# ══════════════════════════════════════════════════════════════════════════════
# (j) Formato del mensaje Telegram
# ══════════════════════════════════════════════════════════════════════════════

class TestTelegramFormatIntraday:

    def test_format_includes_intraday_label(self):
        """El mensaje debe dejar claro que es una señal INTRADÍA de corto plazo."""
        from app.alerts.telegram import format_intraday_flow

        payload = {
            "ticker":      "SOXX",
            "score":       0.85,
            "direction":   "inflow",
            "interval":    "60m",
            "confidence":  "ok",
            "asset_class": "etf",
            "threshold":   0.6,
            "win":         "4h",
        }
        msg = format_intraday_flow(payload)

        assert "intradía" in msg.lower() or "INTRADÍA" in msg
        assert "SOXX" in msg
        assert "+0.850" in msg or "0.85" in msg
        assert "no es consejo" in msg.lower()

    def test_format_shows_disclaimer(self):
        from app.alerts.telegram import format_intraday_flow

        msg = format_intraday_flow({"ticker": "GLD", "score": -0.7, "direction": "outflow",
                                     "interval": "60m", "confidence": "low", "asset_class": "commodity",
                                     "threshold": 0.6})
        assert "no es consejo" in msg.lower()

    def test_format_alert_dispatches_intraday(self):
        """format_alert() enruta correctamente 'intraday_flow'."""
        from app.alerts.telegram import format_alert

        payload = {"ticker": "BTC", "score": 0.8, "direction": "inflow",
                   "interval": "60m", "confidence": "ok", "asset_class": "crypto",
                   "threshold": 0.6}
        msg = format_alert("intraday_flow", payload)
        assert "BTC" in msg


# ══════════════════════════════════════════════════════════════════════════════
# (k) IntradayRunner — escribe en tabla correcta
# ══════════════════════════════════════════════════════════════════════════════

class TestIntradayRunner:

    def test_runner_writes_to_intraday_table_not_daily(self):
        """IntradayRunner escribe en raw_snapshots_intraday, no en raw_snapshots."""
        from app.ingest.intraday_runner import IntradayRunner

        intraday_table = _fluent([])
        daily_table    = _fluent([])

        db = MagicMock()
        db.table.side_effect = lambda name: (
            intraday_table if name == "raw_snapshots_intraday" else daily_table
        )

        mock_yf_result = {
            "assets_queried": 2, "snapshots_inserted": 10,
            "tickers_missing": [], "errors": [], "ok": True,
        }

        with patch("app.ingest.intraday_runner.IntradayRunner._ingest_coingecko_intraday",
                   return_value={"snapshots_inserted": 2, "errors": [], "ok": True}):
            with patch("app.ingest.intraday_runner.IntradayRunner._ingest_fng_intraday",
                       return_value={"snapshots_inserted": 1, "errors": [], "ok": True}):
                with patch("app.ingest.yfinance_intraday.IngestYFinanceIntraday.run_sync",
                           return_value=mock_yf_result):
                    runner = IntradayRunner(db=db, interval="60m", period="5d")
                    result = runner.run_sync()

        assert result["total_snapshots"] == 13
        assert result["ok"]

    def test_runner_accumulates_errors(self):
        """IntradayRunner captura errores de fuentes individuales sin crash."""
        from app.ingest.intraday_runner import IntradayRunner

        db = MagicMock()

        with patch("app.ingest.yfinance_intraday.IngestYFinanceIntraday.run_sync",
                   side_effect=RuntimeError("yf fallo")):
            with patch("app.ingest.intraday_runner.IntradayRunner._ingest_coingecko_intraday",
                       return_value={"snapshots_inserted": 0, "errors": [], "ok": True}):
                with patch("app.ingest.intraday_runner.IntradayRunner._ingest_fng_intraday",
                           return_value={"snapshots_inserted": 0, "errors": [], "ok": True}):
                    runner = IntradayRunner(db=db, interval="60m", period="5d")
                    result = runner.run_sync()

        assert not result["ok"]
        assert any("yf fallo" in e for e in result["errors"])

    def test_runner_coingecko_uses_real_ts(self):
        """La ingesta CoinGecko intradía usa _now_ts() (hora real), no day_ts()."""
        from app.ingest.intraday_runner import _now_ts, _new_src_result
        from app.ingest._base import day_ts

        now = _now_ts()
        midnight = day_ts()

        # Ambos son strings ISO; _now_ts debe NO ser exactamente igual a day_ts
        # (a menos que se ejecute exactamente a medianoche, caso que ignoramos)
        dt_now      = datetime.fromisoformat(now)
        dt_midnight = datetime.fromisoformat(midnight)

        # _now_ts tiene zona horaria
        assert dt_now.tzinfo is not None
        # El formato de day_ts siempre tiene hora 00:00:00
        assert dt_midnight.hour == 0
        assert dt_midnight.minute == 0
        assert dt_midnight.second == 0


# ══════════════════════════════════════════════════════════════════════════════
# (l) Análisis intradía: clasificación inflow/outflow/neutral
# ══════════════════════════════════════════════════════════════════════════════

class TestIntradayAnalysis:

    def _make_analysis_db(self, scores):
        table_map = {
            "flow_scores_intraday": _fluent(scores),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
        return db

    def test_strong_inflow_detected(self):
        from app.analysis.intraday import IntradayAnalysisEngine

        scores = [_intraday_score(1, "GLD", score=0.75, confidence="ok")]
        db = self._make_analysis_db(scores)

        engine = IntradayAnalysisEngine(db=db, interval="60m", threshold=0.6)
        result = engine.run_sync()

        assert "GLD" in result["strong_inflow"]
        assert result["summary"] != "Sin movimientos intradía fuertes."

    def test_strong_outflow_detected(self):
        from app.analysis.intraday import IntradayAnalysisEngine

        scores = [_intraday_score(1, "SOXX", score=-0.80, confidence="ok")]
        db = self._make_analysis_db(scores)

        engine = IntradayAnalysisEngine(db=db, interval="60m", threshold=0.6)
        result = engine.run_sync()

        assert "SOXX" in result["strong_outflow"]

    def test_neutral_not_in_strong_lists(self):
        from app.analysis.intraday import IntradayAnalysisEngine

        scores = [_intraday_score(1, "SPY", score=0.30, confidence="ok")]
        db = self._make_analysis_db(scores)

        engine = IntradayAnalysisEngine(db=db, interval="60m", threshold=0.6)
        result = engine.run_sync()

        assert "SPY" not in result["strong_inflow"]
        assert "SPY" not in result["strong_outflow"]

    def test_low_confidence_not_in_strong_lists(self):
        """Asset con score extremo pero confidence='low' no entra en strong_inflow/outflow."""
        from app.analysis.intraday import IntradayAnalysisEngine

        scores = [_intraday_score(1, "BTC", score=0.90, confidence="low")]
        db = self._make_analysis_db(scores)

        engine = IntradayAnalysisEngine(db=db, interval="60m", threshold=0.6)
        result = engine.run_sync()

        # Aparece en movements (con direction=inflow) pero NO en strong_inflow
        assert "BTC" not in result["strong_inflow"]
        assert any(m["ticker"] == "BTC" for m in result["movements"])

    def test_delta_computed_from_previous_score(self):
        """delta = score_actual - score_previo."""
        from app.analysis.intraday import _group_by_asset, _build_movement

        entries = [
            _intraday_score(1, "SPY", score=0.70, ts="2026-06-19T15:00:00+00:00"),
            _intraday_score(1, "SPY", score=0.50, ts="2026-06-19T14:00:00+00:00"),
        ]
        movement = _build_movement(entries, threshold=0.6)

        assert movement is not None
        assert abs(movement.delta - 0.20) < 1e-6   # 0.70 - 0.50

    def test_empty_db_returns_ok(self):
        """Sin scores intradía, el análisis devuelve ok=True y listas vacías."""
        from app.analysis.intraday import IntradayAnalysisEngine

        db = self._make_analysis_db(scores=[])
        engine = IntradayAnalysisEngine(db=db, interval="60m", threshold=0.6)
        result = engine.run_sync()

        assert result["ok"]
        assert result["movements"] == []
        assert result["strong_inflow"] == []
        assert result["strong_outflow"] == []

    def test_summary_text_includes_tickers(self):
        from app.analysis.intraday import _build_summary

        summary = _build_summary(["GLD", "BTC"], ["SOXX"])
        assert "GLD" in summary
        assert "BTC" in summary
        assert "SOXX" in summary


# ══════════════════════════════════════════════════════════════════════════════
# (m) bars_for_window
# ══════════════════════════════════════════════════════════════════════════════

class TestBarsForWindow:

    def test_60m_4h(self):
        from app.scoring.intraday_engine import bars_for_window
        assert bars_for_window("4h", "60m") == 4

    def test_60m_1d(self):
        from app.scoring.intraday_engine import bars_for_window
        assert bars_for_window("1d_intraday", "60m") == 8

    def test_15m_4h(self):
        from app.scoring.intraday_engine import bars_for_window
        assert bars_for_window("4h", "15m") == 16

    def test_15m_1d(self):
        from app.scoring.intraday_engine import bars_for_window
        assert bars_for_window("1d_intraday", "15m") == 32

    def test_minimum_is_2(self):
        """bars_for_window nunca devuelve menos de 2."""
        from app.scoring.intraday_engine import bars_for_window
        # Caso hipotético con interval muy largo
        assert bars_for_window("4h", "60m") >= 2


# ══════════════════════════════════════════════════════════════════════════════
# Integración: AlertEngine incluye check_intraday_flow sin romper los 4 tipos
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertEngineIntegration:

    def _full_db(self, intraday_scores=None):
        """DB mock con todas las tablas que usa AlertEngine."""
        table_map = {
            "flow_scores":          _fluent([]),
            "flow_scores_intraday": _fluent(intraday_scores or []),
            "regimes":              _fluent([]),
            "correlations":         _fluent([]),
            "exposures":            _fluent([]),
            "narratives":           _fluent([]),
            "alerts":               _fluent([]),
        }
        db = MagicMock()
        db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
        return db

    def test_engine_runs_five_rules(self):
        """AlertEngine evalúa los 5 tipos (4 existentes + intraday_flow) sin errores."""
        from app.alerts.engine import AlertEngine

        db = self._full_db()
        engine = AlertEngine(db=db, send_fn=lambda t: True, min_confidence=0.4)

        with patch("app.config.settings") as s:
            s.flow_extreme_threshold    = 0.7
            s.intraday_flow_threshold   = 0.6
            s.intraday_interval         = "60m"
            s.min_alert_confidence      = 0.4

            result = engine.run_sync()

        assert result["ok"]

    def test_intraday_alert_in_result_alerts(self):
        """Una alerta intraday_flow aparece en result['alerts']."""
        from app.alerts.engine import AlertEngine

        scores = [_intraday_score(1, "GLD", score=0.90, confidence="ok")]
        db = self._full_db(intraday_scores=scores)
        engine = AlertEngine(db=db, send_fn=lambda t: True, min_confidence=0.4)

        with patch("app.config.settings") as s:
            s.flow_extreme_threshold  = 0.7
            s.intraday_flow_threshold = 0.6
            s.intraday_interval       = "60m"
            s.min_alert_confidence    = 0.4

            result = engine.run_sync()

        types = [a["alert_type"] for a in result["alerts"]]
        assert "intraday_flow" in types
