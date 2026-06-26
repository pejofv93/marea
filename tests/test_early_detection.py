"""
Tests MAREA Bloque 4 — Detección temprana (desacoples + volumen anómalo).

Garantías verificadas (entregables):
  (a) DESACOPLE real: par con correlación alta y estable que cae → se detecta y
      CIERRA EL CÍRCULO (nombra los dos lados y qué hace cada uno).
  (b) Par NUNCA correlacionado → sin falso positivo.
  (c) VOLUMEN anómalo vs la línea base del propio activo → detectado; normal → no.
  (d) AUTO-ACTIVACIÓN crítica: sin línea base suficiente, NINGUNA señal y
      baseline_ready=False ("estableciendo línea base").
  (e) Usa el score PENALIZADO por credibilidad (lo que load_scores lee de
      flow_scores.score) para la dirección de cada lado.
  (f) Excluye termómetros de sentimiento (^VIX, CRYPTO_FNG) de ambas detecciones.
  (g) Integración en el digest: bloques 🔗 Desacoples / 📊 Volumen anómalo.
  (h) NINGÚN test hace llamadas reales (ni Telegram, ni BD).
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.alerts.digest import (
    build_daily_digest,
    render_decouple_block,
    render_volume_block,
)
from app.analysis import early_detection as ed
from app.analysis.early_detection import (
    Decouple,
    EarlyDetectionResult,
    VolumeAnomaly,
    build_ticker_pivot,
    detect_decouples,
    detect_volume_anomalies,
    evaluate_early_detection,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _decoupling_pivot(a_tkr="GC=F", b_tkr="SI=F", n_hist=23, n_recent=7):
    """
    Pivot de un par que iba de la mano (histórico correlado) y se desacopla en la
    ventana reciente (anti-correlado).
    """
    ts = pd.date_range("2026-04-01", periods=n_hist + n_recent, freq="D", tz="UTC")
    hist = np.linspace(-1.0, 1.0, n_hist)
    a = list(hist) + [0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9][:n_recent]
    b = list(hist * 0.98 + 0.01) + [0.2, 0.0, -0.2, -0.4, -0.6, -0.8, -0.9][:n_recent]
    return pd.DataFrame({a_tkr: a, b_tkr: b}, index=ts)


def _flat(ts, ticker, score, asset_class="commodity", sector=None):
    """Registro aplanado tal como lo entrega CorrelationBuilder.load_scores."""
    return {"ts": ts, "ticker": ticker, "score": score,
            "asset_class": asset_class, "sector": sector, "confidence": "ok"}


def _records_from_pivot(pivot, asset_class="commodity"):
    """Convierte un pivot ts×ticker a la lista de registros plana (para evaluate)."""
    records = []
    for ts, row in pivot.iterrows():
        for ticker, score in row.items():
            if pd.notna(score):
                records.append(_flat(ts.isoformat(), ticker, float(score), asset_class))
    return records


# ══════════════════════════════════════════════════════════════════════════════
# build_ticker_pivot
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildTickerPivot:

    def test_pivot_shape_and_columns(self):
        records = [
            _flat("2026-06-01T00:00:00+00:00", "GC=F", 0.5),
            _flat("2026-06-01T00:00:00+00:00", "SI=F", 0.4),
            _flat("2026-06-02T00:00:00+00:00", "GC=F", 0.6),
        ]
        pivot = build_ticker_pivot(records)
        assert set(pivot.columns) == {"GC=F", "SI=F"}
        assert len(pivot) == 2

    def test_empty_records(self):
        assert build_ticker_pivot([]).empty


# ══════════════════════════════════════════════════════════════════════════════
# (a)(b) Desacoples
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectDecouples:

    def test_real_decouple_detected_and_closes_circle(self):
        pivot = _decoupling_pivot()
        latest = {"GC=F": 0.80, "SI=F": -0.50}
        decouples, ready, n_watched = detect_decouples(pivot, latest, min_obs=15)

        assert ready is True
        assert n_watched >= 1
        d = next(x for x in decouples if {x.ticker_a, x.ticker_b} == {"GC=F", "SI=F"})
        # base alta, reciente caída
        assert abs(d.base_corr) >= ed.BASE_CORR_THRESHOLD
        assert abs(d.base_corr - d.recent_corr) >= ed.DECOUPLE_DROP_THRESHOLD
        # Cierra el círculo: scores de AMBOS lados presentes (lo que hace cada uno)
        assert d.score_a == 0.80 and d.score_b == -0.50
        assert d.source == "classic"

    def test_uncorrelated_pair_no_false_positive(self):
        ts = pd.date_range("2026-04-01", periods=30, freq="D", tz="UTC")
        rng = np.random.default_rng(0)
        pivot = pd.DataFrame(
            {"^GSPC": rng.normal(0, 1, 30), "^IXIC": rng.normal(0, 1, 30)}, index=ts
        )
        decouples, ready, _ = detect_decouples(pivot, {"^GSPC": 0.3, "^IXIC": 0.2}, min_obs=15)
        # Hubo histórico (ready) pero el par nunca estuvo correlacionado → sin señal
        assert ready is True
        assert all({d.ticker_a, d.ticker_b} != {"^GSPC", "^IXIC"} for d in decouples)

    def test_pair_stays_correlated_no_decouple(self):
        # Par correlado que SIGUE correlado en la ventana reciente → no se desacopla.
        ts = pd.date_range("2026-04-01", periods=30, freq="D", tz="UTC")
        base = np.linspace(-1, 1, 30)
        pivot = pd.DataFrame({"SOXX": base, "SMH": base * 0.99 + 0.005}, index=ts)
        decouples, ready, _ = detect_decouples(pivot, {"SOXX": 0.5, "SMH": 0.5}, min_obs=15)
        assert ready is True
        assert decouples == []

    def test_discovers_stable_pair_beyond_classics(self):
        # Un par NO clásico, fuertemente correlado y luego desacoplado, se descubre.
        pivot = _decoupling_pivot(a_tkr="AAA", b_tkr="BBB")
        decouples, ready, _ = detect_decouples(
            pivot, {"AAA": 0.7, "BBB": -0.6}, classic_pairs=[], min_obs=15,
        )
        d = next(x for x in decouples if {x.ticker_a, x.ticker_b} == {"AAA", "BBB"})
        assert d.source == "discovered"

    def test_excludes_sentiment_thermometers(self):
        # Un "desacople" que involucra al VIX NO debe emitirse.
        pivot = _decoupling_pivot(a_tkr="^VIX", b_tkr="GC=F")
        decouples, _, _ = detect_decouples(
            pivot, {"^VIX": 0.9, "GC=F": 0.5},
            classic_pairs=[("^VIX", "GC=F")], min_obs=15,
        )
        assert decouples == []

    def test_autoactivation_short_history_no_signal(self):
        # Histórico corto: no hay base fiable → ninguna señal y baseline NO listo.
        pivot = _decoupling_pivot().iloc[-10:]
        decouples, ready, _ = detect_decouples(pivot, {"GC=F": 0.8, "SI=F": -0.5}, min_obs=15)
        assert ready is False
        assert decouples == []

    def test_empty_pivot_degrades(self):
        decouples, ready, n = detect_decouples(pd.DataFrame(), {}, min_obs=15)
        assert decouples == [] and ready is False and n == 0


# ══════════════════════════════════════════════════════════════════════════════
# (c) Volumen anómalo
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectVolumeAnomalies:

    def _history(self):
        # Media ~1.05e6, σ ~5e4 (variación realista).
        return [1.0e6] * 10 + [1.1e6] * 10

    def test_spike_detected_normal_not(self):
        hist = self._history()
        vol = {
            "XLF": hist + [1.5e6],   # spike claro (≈9σ)
            "XLK": hist + [1.06e6],  # dentro de rango → no anómalo
        }
        classes = {"XLF": "etf", "XLK": "etf"}
        scores = {"XLF": 0.7, "XLK": -0.05}
        anomalies, ready = detect_volume_anomalies(vol, classes, scores, min_obs=20)

        assert ready is True
        tickers = {a.ticker for a in anomalies}
        assert "XLF" in tickers
        assert "XLK" not in tickers
        a = next(x for x in anomalies if x.ticker == "XLF")
        assert a.sigma >= ed.VOLUME_ANOMALY_SIGMA

    def test_direction_from_penalized_score(self):
        hist = self._history()
        vol = {"GLD": hist + [1.6e6], "GDX": hist + [1.6e6], "XME": hist + [1.6e6]}
        classes = {"GLD": "etf", "GDX": "etf", "XME": "etf"}
        scores = {"GLD": 0.8, "GDX": -0.7, "XME": 0.02}   # inflow / outflow / neutral
        anomalies, _ = detect_volume_anomalies(vol, classes, scores, min_obs=20)
        by = {a.ticker: a.direction for a in anomalies}
        assert by["GLD"] == "inflow"
        assert by["GDX"] == "outflow"
        assert by["XME"] == "neutral"

    def test_autoactivation_short_history_no_signal(self):
        vol = {"XLF": [1e6, 1.1e6, 5e6]}   # solo 3 obs < min_obs
        anomalies, ready = detect_volume_anomalies(vol, {"XLF": "etf"}, {"XLF": 0.7}, min_obs=20)
        assert anomalies == []
        assert ready is False

    def test_excludes_sentiment_thermometers(self):
        hist = self._history()
        vol = {"^VIX": hist + [9e6], "CRYPTO_FNG": hist + [9e6]}
        anomalies, ready = detect_volume_anomalies(
            vol, {"^VIX": "macro", "CRYPTO_FNG": "crypto"},
            {"^VIX": 0.9, "CRYPTO_FNG": -0.9}, min_obs=20,
        )
        assert anomalies == []
        # Eran los únicos tickers y están excluidos → ni siquiera hay base.
        assert ready is False

    def test_flat_volume_no_anomaly(self):
        vol = {"SPY": [2.0e6] * 21}   # σ=0 → no se puede anomalizar
        anomalies, ready = detect_volume_anomalies(vol, {"SPY": "etf"}, {"SPY": 0.1}, min_obs=20)
        assert anomalies == []
        assert ready is True   # hubo histórico suficiente, simplemente no es anómalo


# ══════════════════════════════════════════════════════════════════════════════
# (g) Render — bloques del digest
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderBlocks:

    def test_decouple_block_closes_circle(self):
        result = EarlyDetectionResult(decouples=[
            Decouple("GC=F", "SI=F", base_corr=0.92, recent_corr=0.10,
                     score_a=0.80, score_b=-0.50, source="classic"),
        ])
        text = "\n".join(render_decouple_block(result))
        assert "🔗 <b>Desacoples:</b>" in text
        assert "Oro" in text and "Plata" in text          # nombres reales, los dos lados
        assert "desacoplado" in text
        assert "entra en Oro (+0.80)" in text             # qué hace cada lado
        assert "sale de Plata (-0.50)" in text
        assert "rota de uno a otro" in text               # cola condicional (signos opuestos)

    def test_decouple_block_same_direction_no_rota_tail(self):
        result = EarlyDetectionResult(decouples=[
            Decouple("SOXX", "SMH", 0.90, 0.20, score_a=0.6, score_b=0.3, source="classic"),
        ])
        text = "\n".join(render_decouple_block(result))
        assert "rota de uno a otro" not in text           # ambos entran → sin cola de rotación

    def test_decouple_block_empty(self):
        assert render_decouple_block(EarlyDetectionResult()) == []

    def test_volume_block_direction_and_name(self):
        result = EarlyDetectionResult(anomalies=[
            VolumeAnomaly("XLF", "etf", sigma=4.2, direction="inflow", score=0.70),
            VolumeAnomaly("GDX", "etf", sigma=3.1, direction="outflow", score=-0.55),
            VolumeAnomaly("XME", "etf", sigma=2.8, direction="neutral", score=0.03),
        ])
        text = "\n".join(render_volume_block(result))
        assert "📊 <b>Volumen anómalo:</b>" in text
        assert "Financieras (bancos)" in text and "ENTRADA" in text
        assert "SALIDA" in text
        assert "solo atención" in text                    # neutral: solo atención

    def test_volume_block_empty(self):
        assert render_volume_block(EarlyDetectionResult()) == []


# ══════════════════════════════════════════════════════════════════════════════
# build_daily_digest — splice de los bloques de detección temprana
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildDailyIntegration:

    def _state(self):
        return {
            "assets": [{"ticker": "GC=F", "name": None, "asset_class": "commodity",
                        "sector": None, "score": 0.8, "confidence": "ok"}],
            "regime": {"name": "risk_on", "confidence": 0.8, "signals": []},
            "cold_start": False, "rotations": [],
        }

    def test_includes_decouple_and_volume_blocks(self):
        text = build_daily_digest(
            self._state(),
            decouple_lines=["🔗 <b>Desacoples:</b>", "  • Oro y Plata…"],
            volume_lines=["📊 <b>Volumen anómalo:</b>", "  • Financieras…"],
        )
        assert "🔗 <b>Desacoples:</b>" in text
        assert "📊 <b>Volumen anómalo:</b>" in text

    def test_absent_when_no_signal(self):
        text = build_daily_digest(self._state())
        assert "🔗 <b>Desacoples:</b>" not in text
        assert "📊 <b>Volumen anómalo:</b>" not in text


# ══════════════════════════════════════════════════════════════════════════════
# evaluate_early_detection — fachada de BD (mockeada)
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateEndToEnd:

    def test_detects_decouple_and_anomaly_with_penalized_scores(self):
        # flow_scores con score PENALIZADO (≠ score_raw) → el desacople usa 'score'.
        pivot = _decoupling_pivot()
        records = _records_from_pivot(pivot)
        # Inyecta scores recientes claros para cerrar el círculo.
        latest_a = [r for r in records if r["ticker"] == "GC=F"][-1]
        latest_b = [r for r in records if r["ticker"] == "SI=F"][-1]
        latest_a["score"], latest_b["score"] = 0.80, -0.50

        hist = [1.0e6] * 10 + [1.1e6] * 10
        vol_history = {"GC=F": hist + [1.6e6]}   # spike en oro
        classes = {"GC=F": "commodity"}

        db = MagicMock()
        with patch("app.analysis.correlation.CorrelationBuilder.load_scores", return_value=records), \
             patch("app.analysis.early_detection._load_volume_history",
                   return_value=(vol_history, classes)):
            result = evaluate_early_detection(db, corr_min_obs=15, vol_min_obs=20)

        assert any({d.ticker_a, d.ticker_b} == {"GC=F", "SI=F"} for d in result.decouples)
        d = next(x for x in result.decouples if {x.ticker_a, x.ticker_b} == {"GC=F", "SI=F"})
        assert d.score_a == 0.80 and d.score_b == -0.50   # score penalizado, no bruto
        assert any(a.ticker == "GC=F" for a in result.anomalies)
        assert result.corr_baseline_ready and result.vol_baseline_ready

    def test_empty_db_degrades_no_signal(self):
        db = MagicMock()
        with patch("app.analysis.correlation.CorrelationBuilder.load_scores", return_value=[]), \
             patch("app.analysis.early_detection._load_volume_history", return_value=({}, {})):
            result = evaluate_early_detection(db, corr_min_obs=15, vol_min_obs=20)
        assert result.decouples == [] and result.anomalies == []
        assert result.corr_baseline_ready is False
        assert result.vol_baseline_ready is False

    def test_db_error_never_raises(self):
        db = MagicMock()
        with patch("app.analysis.correlation.CorrelationBuilder.load_scores",
                   side_effect=RuntimeError("boom")), \
             patch("app.analysis.early_detection._load_volume_history",
                   side_effect=RuntimeError("kaboom")):
            result = evaluate_early_detection(db, corr_min_obs=15, vol_min_obs=20)
        assert result.decouples == [] and result.anomalies == []
        assert any("boom" in e for e in result.errors)
