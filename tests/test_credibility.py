"""
Tests MAREA — Bloque 2: capa de CREDIBILIDAD del flujo.

Garantías verificadas:
  (a) Volumen+precio coherentes → credibilidad alta, score intacto.
  (b) Volumen sin confirmación de precio → credibilidad baja, score penalizado.
  (c) Persistencia: bajo umbral de obs no influye; sobre umbral, pico aislado
      penaliza y flujo sostenido no.
  (d) Etiqueta correcta (confirmado/dudoso/fogonazo) según el caso.
  (e) La credibilidad NO aplica a termómetros/contexto (VIX/FNG/DXY/bono/stable).
  (f) Score bruto y penalizado se guardan AMBOS.
  (g) Auto-activación de persistencia degrada con elegancia (sin histórico).
  (h) Credibilidad ⟂ confianza (cold start): ejes distintos.
"""

from unittest.mock import MagicMock

import pytest

from app.scoring.credibility import (
    assess_credibility,
    penalized_score,
    LABEL_CONFIRMED,
    LABEL_DOUBTFUL,
    LABEL_SPIKE,
    CONFIRM_THRESHOLD,
    DOUBT_THRESHOLD,
)


def _rows(closes, vols):
    return [
        {"ts": f"2026-06-{1+i:02d}T00:00:00+00:00", "close": c, "volume": v, "extra": {}}
        for i, (c, v) in enumerate(zip(closes, vols))
    ]


# ══════════════════════════════════════════════════════════════════════════════
# (a)(b)(d) Confirmación de precio (día 1, sin histórico de persistencia)
# ══════════════════════════════════════════════════════════════════════════════

class TestPriceConfirmation:

    def test_volume_and_price_coherent_high_credibility(self):
        # Precio sube acompañando al inflow → confirmado, score intacto.
        rows = _rows([100, 101, 102, 103, 104, 108], [1e6] * 6)
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=99)
        assert cred.label == LABEL_CONFIRMED
        assert cred.credibility >= CONFIRM_THRESHOLD
        assert penalized_score(0.9, cred) == pytest.approx(0.9)

    def test_volume_without_price_confirmation_penalized(self):
        # Precio plano pese a volumen → dudoso, score penalizado (persist inactiva).
        rows = _rows([100, 100, 100, 100, 100, 100.05], [1e6] * 6)
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=99)
        assert cred.label == LABEL_DOUBTFUL          # solo precio plano → 0.6
        assert cred.credibility < 1.0
        assert penalized_score(0.9, cred) < 0.9
        assert "plano" in cred.reason

    def test_price_against_flow_is_spike(self):
        # Crypto: volumen alto (score +) pero precio CAE → fogonazo.
        rows = _rows([100, 99, 98, 97, 96, 92], [1e6] * 6)
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=99)
        assert cred.label == LABEL_SPIKE
        assert cred.credibility < DOUBT_THRESHOLD
        assert "contra" in cred.reason

    def test_negative_flow_with_price_down_is_confirmed(self):
        # Outflow (score −) con precio bajando → coherente (confirmado).
        rows = _rows([108, 106, 104, 102, 100, 96], [1e6] * 6)
        cred = assess_credibility(rows, score_raw=-0.8, window=5, persist_min_obs=99)
        assert cred.label == LABEL_CONFIRMED
        assert penalized_score(-0.8, cred) == pytest.approx(-0.8)


# ══════════════════════════════════════════════════════════════════════════════
# (c)(g) Persistencia auto-activada
# ══════════════════════════════════════════════════════════════════════════════

class TestPersistence:

    def test_below_min_obs_persistence_inactive(self):
        # 3 obs < min 10 → persistencia no influye; credibilidad solo de precio.
        rows = _rows([100, 102, 108], [1e6, 1e6, 3e6])
        cred = assess_credibility(rows, score_raw=0.9, window=3, persist_min_obs=10)
        assert cred.persistence_active is False
        assert cred.persistence is None
        assert "sostenido" not in (cred.reason or "")
        assert "aislado" not in (cred.reason or "")

    def test_isolated_spike_penalized_when_active(self):
        # Persistencia activa: solo la última barra con volumen elevado → pico aislado.
        rows = _rows([100, 101, 102, 103, 104, 109], [1e6, 1e6, 1e6, 1e6, 1e6, 3e6])
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=4)
        assert cred.persistence_active is True
        assert cred.persistence < 1.0
        assert "aislado" in cred.reason

    def test_sustained_flow_not_penalized_by_persistence(self):
        # Varias barras recientes con volumen elevado → sostenido, persistencia=1.0.
        rows = _rows([100, 101, 102, 103, 104, 109], [1e6, 1e6, 2.5e6, 2.7e6, 2.9e6, 3e6])
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=4)
        assert cred.persistence_active is True
        assert cred.persistence == pytest.approx(1.0)
        assert cred.label == LABEL_CONFIRMED


# ══════════════════════════════════════════════════════════════════════════════
# Casos límite y helper
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_no_flow_returns_none(self):
        rows = _rows([100, 101, 102], [1e6, 1e6, 1e6])
        assert assess_credibility(rows, score_raw=0.03, window=3, persist_min_obs=4) is None
        assert assess_credibility(rows, score_raw=None, window=3, persist_min_obs=4) is None

    def test_missing_price_does_not_penalize(self):
        # Sin precio no se puede confirmar → no se penaliza (beneficio de la duda).
        rows = [{"ts": f"2026-06-0{i+1}T00:00:00+00:00", "volume": 1e6, "extra": {}} for i in range(6)]
        cred = assess_credibility(rows, score_raw=0.9, window=5, persist_min_obs=99)
        assert cred.price_factor == pytest.approx(1.0)

    def test_penalized_score_helper(self):
        assert penalized_score(0.8, None) == 0.8            # sin credibilidad → intacto
        assert penalized_score(None, None) is None
        cred = assess_credibility(_rows([100, 99, 98, 97, 96, 92], [1e6] * 6), 0.9, 5, persist_min_obs=99)
        assert penalized_score(0.9, cred) == pytest.approx(0.9 * cred.credibility, abs=1e-6)


# ══════════════════════════════════════════════════════════════════════════════
# (e) Qué estrategias reciben credibilidad
# ══════════════════════════════════════════════════════════════════════════════

class TestStrategyApplicability:

    def test_flow_strategies_apply_credibility(self):
        from app.scoring.strategies import VolumeFlowStrategy, CryptoVolumeStrategy
        assert VolumeFlowStrategy.applies_credibility is True
        assert CryptoVolumeStrategy.applies_credibility is True

    def test_thermometers_and_context_do_not(self):
        from app.scoring.strategies import (
            VIXStrategy, FearGreedStrategy, DollarIndexStrategy,
            BondYieldStrategy, StablecoinSupplyStrategy,
        )
        for cls in (VIXStrategy, FearGreedStrategy, DollarIndexStrategy,
                    BondYieldStrategy, StablecoinSupplyStrategy):
            assert getattr(cls, "applies_credibility", False) is False


# ══════════════════════════════════════════════════════════════════════════════
# (f)(h) Integración en el motor: guarda bruto + penalizado, separada de confidence
# ══════════════════════════════════════════════════════════════════════════════

def _engine_db(assets, snapshots):
    mock_db = MagicMock()
    assets_mock, snaps_mock, scores_mock = MagicMock(), MagicMock(), MagicMock()

    def _table(name):
        if name == "assets":
            return assets_mock
        if name == "raw_snapshots":
            return snaps_mock
        return scores_mock

    mock_db.table.side_effect = _table
    assets_mock.select.return_value.eq.return_value.execute.return_value.data = assets
    (snaps_mock.select.return_value.eq.return_value.order.return_value
     .limit.return_value.execute.return_value.data) = snapshots
    return mock_db, scores_mock


def _upserted_rows(scores_mock):
    rows = []
    for call in scores_mock.upsert.call_args_list:
        rows.extend(call.args[0])
    return rows


class TestEngineIntegration:

    def test_flow_asset_stores_raw_and_penalized(self):
        from app.scoring.engine import ScoreEngine
        # SPY (etf) con volumen disparado en la última barra pero precio plano:
        # credibilidad < 1 → score penalizado, ambos guardados.
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": "broad_market"}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": 400.0, "volume": 1e6 if d < 31 else 5e6, "extra": {}}
            for d in range(1, 32)
        ]
        mock_db, scores_mock = _engine_db(assets, snaps)
        ScoreEngine(db=mock_db, min_obs=5, persist_min_obs=5).run_sync()

        rows = _upserted_rows(scores_mock)
        assert rows, "se esperaban filas de flow_scores"
        for r in rows:
            assert "score_raw" in r and "credibility" in r
            assert r["credibility_label"] in ("confirmado", "dudoso", "fogonazo")
            # score = score_raw × credibility
            assert r["score"] == pytest.approx(round(r["score_raw"] * r["credibility"], 6), abs=1e-6)
        # precio plano → al menos una ventana penalizada (score < bruto en magnitud)
        assert any(abs(r["score"]) < abs(r["score_raw"]) for r in rows)

    def test_thermometer_asset_no_credibility(self):
        from app.scoring.engine import ScoreEngine
        # ^VIX (macro/volatility) → sin credibilidad: campos None, score == bruto.
        assets = [{"id": 9, "ticker": "^VIX", "asset_class": "macro", "sector": "volatility"}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00", "close": 15.0 + d, "volume": 0.0, "extra": {}}
            for d in range(1, 32)
        ]
        mock_db, scores_mock = _engine_db(assets, snaps)
        ScoreEngine(db=mock_db, min_obs=5).run_sync()

        rows = _upserted_rows(scores_mock)
        assert rows
        for r in rows:
            assert r["credibility"] is None
            assert r["credibility_label"] is None
            assert r["score"] == r["score_raw"]   # sin penalización

    def test_credibility_independent_of_confidence(self):
        from app.scoring.engine import ScoreEngine
        # Histórico AMPLIO (confidence ok) pero precio plano con pico de volumen:
        # credibilidad baja aunque la confianza sea alta → ejes distintos.
        assets = [{"id": 1, "ticker": "SPY", "asset_class": "etf", "sector": "broad_market"}]
        snaps = [
            {"ts": f"2024-01-{d:02d}T00:00:00+00:00",
             "close": 400.0, "volume": 1e6 if d < 31 else 6e6, "extra": {}}
            for d in range(1, 32)
        ]
        mock_db, scores_mock = _engine_db(assets, snaps)
        ScoreEngine(db=mock_db, min_obs=5, persist_min_obs=5).run_sync()
        rows = _upserted_rows(scores_mock)
        # confianza 'ok' (muchas obs) y aun así credibilidad reducida en alguna ventana
        assert any(r["confidence"] == "ok" for r in rows)
        assert any(r["credibility"] is not None and r["credibility"] < 1.0 for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
# Reflejo en el digest: etiqueta solo para dudoso/fogonazo
# ══════════════════════════════════════════════════════════════════════════════

class TestDigestReflection:

    def test_cred_tag_marks_spike_and_doubt(self):
        from app.alerts.digest import _cred_tag
        assert "fogonazo" in _cred_tag({"credibility_label": "fogonazo", "credibility_reason": "precio en contra del flujo"})
        assert "sin confirmar" in _cred_tag({"credibility_label": "dudoso"})
        assert _cred_tag({"credibility_label": "confirmado"}) == ""   # discreto
        assert _cred_tag({}) == ""

    def test_daily_digest_shows_spike_label(self):
        from app.alerts.digest import build_daily_digest
        state = {
            "assets": [
                {"ticker": "BTC", "score": 0.4, "asset_class": "crypto", "confidence": "ok",
                 "credibility_label": "fogonazo", "credibility_reason": "precio en contra del flujo"},
            ],
            "regime": None, "cold_start": False, "rotations": [],
        }
        text = build_daily_digest(state)
        assert "posible fogonazo" in text
        assert "precio en contra del flujo" in text

    def test_confirmed_flow_is_discreet(self):
        from app.alerts.digest import build_daily_digest
        state = {
            "assets": [
                {"ticker": "GLD", "score": 0.7, "asset_class": "etf", "confidence": "ok",
                 "credibility_label": "confirmado", "credibility_reason": "precio confirma"},
            ],
            "regime": None, "cold_start": False, "rotations": [],
        }
        text = build_daily_digest(state)
        assert "fogonazo" not in text
        assert "sin confirmar" not in text
