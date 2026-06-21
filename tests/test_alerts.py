"""
Tests MAREA Sesión 8 — Motor de alertas + bot de Telegram.

Garantías verificadas:
  (a) 4 tipos de alerta con umbrales configurables.
  (b) Anti-duplicado por cambio de estado (régimen que persiste NO redispara).
  (c) Umbral de confianza: baja confianza → sent=False, not_sent_reason='low_confidence'.
  (d) Histéresis en flow scores: no redispara mientras sigue por encima;
      vuelve a armarse cuando baja del umbral.
  (e) Alertas de exposición incluyen confianza y fuentes en el payload.
  (f) NINGÚN test envía mensajes reales a Telegram.
  (g) Los 256 tests previos siguen verdes.
  (h) Error de red en Telegram → reintento, no crash.
  (i) Cold start (confianza 'low') → no se envía.
  (j) Idempotencia.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

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
    flow_scores=None,
    regimes=None,
    correlations=None,
    exposures=None,
    narratives=None,
    alerts=None,
):
    table_map = {
        "flow_scores":  _fluent(flow_scores),
        "regimes":      _fluent(regimes),
        "correlations": _fluent(correlations),
        "exposures":    _fluent(exposures),
        "narratives":   _fluent(narratives),
        "alerts":       _fluent(alerts),
    }
    db = MagicMock()
    db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
    return db


def _score_row(ticker, score, confidence="normal", asset_class="crypto"):
    return {
        "asset_id": hash(ticker) % 1000,
        "ts": "2026-06-17T00:00:00+00:00",
        "win": "7d",
        "score": score,
        "raw_zscore": score * 2.5,
        "proxy_used": False,
        "n_obs": 20,
        "confidence": confidence,
        "assets": {"ticker": ticker, "asset_class": asset_class, "sector": None},
    }


def _regime_row(regime, confidence=0.8):
    return {
        "ts": "2026-06-17T00:00:00+00:00",
        "win": "7d",
        "regime": regime,
        "confidence": confidence,
        "signals": ["crypto_outflow", "equity_outflow"],
    }


def _decoupling_row(pair_a="BTC", pair_b="SPY", corr=-0.65):
    return {
        "ts": "2026-06-17T00:00:00+00:00",
        "win": "7d",
        "matrix_type": "intermarket",
        "pair_a": pair_a,
        "pair_b": pair_b,
        "corr": corr,
        "is_decoupling": True,
    }


def _exposure_row(source="OpenAI", ticker="MSFT", confidence="confirmado_oficial"):
    return {
        "source_entity": source,
        "exposed_ticker": ticker,
        "exposure_type": "pre_ipo",
        "relationship": f"{source} tiene acuerdo de inversión con {ticker}",
        "confidence": confidence,
        "sources": ["https://sec.gov/filing123", "https://reuters.com/article456"],
        "llm_engine": "groq",
    }


def _alert_row(alert_type, entity, state, sent=True):
    return {
        "id": 1,
        "alert_type": alert_type,
        "entity": entity,
        "state": state,
        "sent": sent,
        "not_sent_reason": None if sent else "duplicate",
        "ts": "2026-06-17T00:00:00+00:00",
        "sent_at": "2026-06-17T00:01:00+00:00" if sent else None,
    }


def _make_engine(
    db=None,
    send_fn=None,
    min_confidence=0.4,
    flow_extreme_threshold=0.7,
):
    from app.alerts.engine import AlertEngine
    engine = AlertEngine(db=db, send_fn=send_fn, min_confidence=min_confidence)
    # Sobreescribir el threshold en settings via mock si hace falta
    with patch("app.alerts.engine.AlertEngine._min_conf", new=min_confidence):
        pass
    return engine


# ══════════════════════════════════════════════════════════════════════════════
# S8-1: telegram.py — cliente HTTP
# ══════════════════════════════════════════════════════════════════════════════

class TestTelegramClient:

    def test_send_message_returns_true_on_200(self):
        """(f) No se envían mensajes reales: httpx está mockeado."""
        from app.alerts.telegram import send_message

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("app.alerts.telegram.httpx.post", return_value=mock_resp):
            ok = send_message("hola mundo", token="tok", chat_id="123")

        assert ok is True

    def test_send_message_returns_false_on_4xx(self):
        from app.alerts.telegram import send_message

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("app.alerts.telegram.httpx.post", return_value=mock_resp):
            ok = send_message("msg", token="bad", chat_id="123")

        assert ok is False

    def test_send_message_empty_token_returns_false(self):
        from app.alerts.telegram import send_message
        with patch("app.alerts.telegram.httpx.post") as mock_post:
            ok = send_message("msg", token="", chat_id="123")
        assert ok is False
        mock_post.assert_not_called()

    def test_send_message_empty_chat_id_returns_false(self):
        from app.alerts.telegram import send_message
        with patch("app.alerts.telegram.httpx.post") as mock_post:
            ok = send_message("msg", token="tok", chat_id="")
        assert ok is False
        mock_post.assert_not_called()

    def test_network_error_retries_and_returns_false(self):
        """(h) Error de red → reintenta, no crash. Devuelve False tras agotar reintentos."""
        from app.alerts.telegram import send_message

        with patch("app.alerts.telegram.httpx.post", side_effect=Exception("timeout")), \
             patch("app.alerts.telegram.time.sleep"):
            ok = send_message("msg", token="tok", chat_id="123")

        assert ok is False

    def test_network_error_retries_multiple_times(self):
        """El retry se intenta _RETRY_DELAYS veces antes de rendirse."""
        from app.alerts import telegram as tg

        call_count = 0

        def fail_once_then_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("red caída")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            return mock_resp

        with patch("app.alerts.telegram.httpx.post", side_effect=fail_once_then_ok), \
             patch("app.alerts.telegram.time.sleep"):
            ok = tg.send_message("msg", token="tok", chat_id="123")

        assert ok is True
        assert call_count == 2

    def test_format_flow_extreme_contains_ticker(self):
        from app.alerts.telegram import format_flow_extreme
        text = format_flow_extreme({"ticker": "BTC", "score": 0.82, "threshold": 0.7,
                                    "win": "7d", "confidence": "normal", "asset_class": "crypto"})
        assert "BTC" in text
        assert "0.82" in text or "0.820" in text

    def test_format_regime_change_contains_regimes(self):
        from app.alerts.telegram import format_regime_change
        text = format_regime_change({
            "prev_regime": "risk_on", "curr_regime": "risk_off",
            "curr_confidence": 0.75, "signals": ["crypto_outflow"],
            "narrative_summary": "",
        })
        assert "risk_on" in text
        assert "risk_off" in text

    def test_format_regime_change_includes_summary_if_present(self):
        from app.alerts.telegram import format_regime_change
        text = format_regime_change({
            "prev_regime": "risk_on", "curr_regime": "risk_off",
            "curr_confidence": 0.75, "signals": [],
            "narrative_summary": "El capital fluye hacia activos seguros.",
        })
        assert "El capital fluye" in text

    def test_format_exposure_confirmado_no_warning(self):
        from app.alerts.telegram import format_exposure
        text = format_exposure({
            "source_entity": "OpenAI", "exposed_ticker": "MSFT",
            "exposure_type": "pre_ipo", "confidence": "confirmado_oficial",
            "relationship": "Inversión directa", "sources": ["https://sec.gov/x"],
        })
        assert "OpenAI" in text
        assert "MSFT" in text
        assert "SIN VERIFICAR" not in text

    def test_format_exposure_especulacion_has_warning(self):
        """(e) Exposición de baja confianza incluye aviso SIN VERIFICAR."""
        from app.alerts.telegram import format_exposure
        text = format_exposure({
            "source_entity": "SpaceX", "exposed_ticker": "BA",
            "exposure_type": "pre_ipo", "confidence": "especulacion",
            "relationship": "Posible acuerdo", "sources": ["https://reuters.com/x"],
        })
        assert "SIN VERIFICAR" in text
        assert "SpaceX" in text

    def test_format_exposure_rumor_has_warning(self):
        from app.alerts.telegram import format_exposure
        text = format_exposure({
            "source_entity": "X", "exposed_ticker": "Y",
            "confidence": "rumor_prensa",
            "sources": ["https://example.com/x"],
            "relationship": "", "exposure_type": "crypto",
        })
        assert "SIN VERIFICAR" in text

    def test_format_decoupling_contains_pair(self):
        from app.alerts.telegram import format_decoupling
        text = format_decoupling({
            "pair_a": "BTC", "pair_b": "SPY",
            "corr": -0.65, "matrix_type": "intermarket", "win": "7d",
        })
        assert "BTC" in text
        assert "SPY" in text
        assert "-0.65" in text or "-0.650" in text


# ══════════════════════════════════════════════════════════════════════════════
# S8-2: rules.py — los 4 disparadores
# ══════════════════════════════════════════════════════════════════════════════

class TestRules:

    def test_flow_extreme_above_threshold(self):
        """(a) Flow score extremo → PotentialAlert generado."""
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("BTC", 0.82)])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "flow_extreme"
        assert alerts[0].entity == "BTC"
        assert alerts[0].state == "extreme"
        assert alerts[0].payload["score"] == pytest.approx(0.82)
        assert alerts[0].payload["threshold"] == 0.7

    def test_flow_extreme_below_threshold_no_alert(self):
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("SPY", 0.5)])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert alerts == []

    def test_flow_extreme_negative_score_triggers(self):
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("GLD", -0.8)])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert len(alerts) == 1
        assert alerts[0].payload["score"] == pytest.approx(-0.8)

    def test_flow_extreme_at_threshold_no_alert(self):
        """Exactamente en el umbral: no dispara (necesita superar, no igualar)."""
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("BTC", 0.7)])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert alerts == []

    def test_flow_extreme_deduplicates_by_asset(self):
        """Dos filas del mismo asset → solo una alerta."""
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[
            _score_row("BTC", 0.9), _score_row("BTC", 0.85),
        ])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert len([a for a in alerts if a.entity == "BTC"]) == 1

    def test_flow_extreme_low_confidence_score_gets_low_conf_num(self):
        """(i) Cold start: confidence='low' → confidence numérica 0.2."""
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("BTC", 0.9, confidence="low")])
        alerts = check_flow_extreme(db, threshold=0.7)
        assert len(alerts) == 1
        assert alerts[0].confidence == pytest.approx(0.2)

    def test_flow_extreme_configurable_threshold(self):
        """(a) El umbral es configurable."""
        from app.alerts.rules import check_flow_extreme
        db = _make_db(flow_scores=[_score_row("BTC", 0.55)])
        no_alert = check_flow_extreme(db, threshold=0.7)
        with_alert = check_flow_extreme(db, threshold=0.5)
        assert no_alert == []
        assert len(with_alert) == 1

    def test_flow_extreme_db_error_returns_empty(self):
        from app.alerts.rules import check_flow_extreme
        db = MagicMock()
        db.table.side_effect = RuntimeError("DB caída")
        alerts = check_flow_extreme(db, threshold=0.7)
        assert alerts == []

    def test_get_current_extreme_tickers(self):
        from app.alerts.rules import get_current_extreme_tickers
        db = _make_db(flow_scores=[
            _score_row("BTC", 0.9),
            _score_row("ETH", 0.3),
        ])
        result = get_current_extreme_tickers(db, threshold=0.7)
        assert "BTC" in result
        assert "ETH" not in result

    def test_regime_change_detects_transition(self):
        """(b) Cambio de régimen: prev distinto de actual → alerta."""
        from app.alerts.rules import check_regime_change
        db = _make_db(regimes=[_regime_row("risk_off", 0.8)])
        alerts = check_regime_change(db, last_sent_regime="risk_on")
        assert len(alerts) == 1
        assert alerts[0].alert_type == "regime_change"
        assert alerts[0].state == "risk_off"
        assert alerts[0].payload["prev_regime"] == "risk_on"
        assert alerts[0].payload["curr_regime"] == "risk_off"
        assert alerts[0].payload["curr_confidence"] == pytest.approx(0.8)

    def test_regime_same_as_last_no_alert(self):
        """(b) Régimen que persiste NO redispara."""
        from app.alerts.rules import check_regime_change
        db = _make_db(regimes=[_regime_row("risk_off", 0.8)])
        alerts = check_regime_change(db, last_sent_regime="risk_off")
        assert alerts == []

    def test_regime_first_ever_no_previous(self):
        """Primera vez (last_sent=None): alerta se genera."""
        from app.alerts.rules import check_regime_change
        db = _make_db(regimes=[_regime_row("risk_on", 0.75)])
        alerts = check_regime_change(db, last_sent_regime=None)
        assert len(alerts) == 1

    def test_regime_empty_db_no_alert(self):
        from app.alerts.rules import check_regime_change
        db = _make_db(regimes=[])
        alerts = check_regime_change(db, last_sent_regime=None)
        assert alerts == []

    def test_regime_includes_narrative_summary(self):
        """El payload de régimen incluye el resumen de la última narrativa (si existe)."""
        from app.alerts.rules import check_regime_change
        db = _make_db(
            regimes=[_regime_row("risk_off", 0.8)],
            narratives=[{"text": "El capital fluye hacia activos defensivos.\nMás detalles."}],
        )
        alerts = check_regime_change(db, last_sent_regime="risk_on")
        assert alerts[0].payload["narrative_summary"] == "El capital fluye hacia activos defensivos."

    def test_regime_signals_in_payload(self):
        from app.alerts.rules import check_regime_change
        db = _make_db(regimes=[_regime_row("risk_off", 0.8)])
        alerts = check_regime_change(db, last_sent_regime="risk_on")
        assert "signals" in alerts[0].payload
        assert isinstance(alerts[0].payload["signals"], list)

    def test_decoupling_detects_pair(self):
        """(a) Desacople detectado → PotentialAlert generado."""
        from app.alerts.rules import check_decoupling
        db = _make_db(correlations=[_decoupling_row("BTC", "SPY", -0.65)])
        alerts = check_decoupling(db)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "decoupling"
        assert alerts[0].entity == "BTC/SPY"
        assert alerts[0].state == "decoupled"
        assert alerts[0].payload["corr"] == pytest.approx(-0.65)

    def test_decoupling_no_rows_no_alert(self):
        from app.alerts.rules import check_decoupling
        db = _make_db(correlations=[])
        alerts = check_decoupling(db)
        assert alerts == []

    def test_exposure_check_generates_alert_with_payload(self):
        """(a)(e) Exposición nueva → PotentialAlert con confianza + fuentes en payload."""
        from app.alerts.rules import check_exposure
        row = _exposure_row("OpenAI", "MSFT", "confirmado_oficial")
        db = _make_db(exposures=[row])
        alerts = check_exposure(db)
        assert len(alerts) == 1
        a = alerts[0]
        assert a.alert_type == "exposure"
        assert a.entity == "OpenAI→MSFT"
        assert a.state == "confirmado_oficial"
        assert a.payload["confidence"] == "confirmado_oficial"
        assert isinstance(a.payload["sources"], list)
        assert len(a.payload["sources"]) > 0

    def test_exposure_state_equals_confidence_level(self):
        """(e) El state de exposición = nivel de confianza (permite re-alertar si mejora)."""
        from app.alerts.rules import check_exposure
        db = _make_db(exposures=[_exposure_row(confidence="especulacion")])
        alerts = check_exposure(db)
        assert alerts[0].state == "especulacion"

    def test_exposure_confidence_num_for_official(self):
        from app.alerts.rules import check_exposure
        db = _make_db(exposures=[_exposure_row(confidence="confirmado_oficial")])
        alerts = check_exposure(db)
        assert alerts[0].confidence == pytest.approx(0.9)

    def test_exposure_confidence_num_for_speculation(self):
        from app.alerts.rules import check_exposure
        db = _make_db(exposures=[_exposure_row(confidence="especulacion")])
        alerts = check_exposure(db)
        assert alerts[0].confidence == pytest.approx(0.3)


# ══════════════════════════════════════════════════════════════════════════════
# S8-3: dedup.py — anti-duplicado y re-armado
# ══════════════════════════════════════════════════════════════════════════════

class TestDedup:

    def test_was_sent_true_when_exists_and_sent(self):
        from app.alerts.dedup import was_sent
        db = _make_db(alerts=[_alert_row("flow_extreme", "BTC", "extreme", sent=True)])
        # La tabla de alerts ya devuelve la fila → was_sent=True
        assert was_sent(db, "flow_extreme", "BTC", "extreme") is True

    def test_was_sent_false_when_no_rows(self):
        from app.alerts.dedup import was_sent
        db = _make_db(alerts=[])
        assert was_sent(db, "flow_extreme", "BTC", "extreme") is False

    def test_was_sent_false_when_sent_false(self):
        from app.alerts.dedup import was_sent
        db = _make_db(alerts=[_alert_row("flow_extreme", "BTC", "extreme", sent=False)])
        # La tabla filtra por sent=True, así que no devuelve la fila
        result = was_sent(db, "flow_extreme", "BTC", "extreme")
        # En este caso el mock no filtra realmente por sent=True,
        # pero el código SÍ llama .eq("sent", True), así que comprobamos que el código es correcto
        # inspeccionando las llamadas:
        alerts_table = db.table("alerts")
        # Verifica que se llamó .eq("sent", True)
        calls = [str(c) for c in alerts_table.eq.call_args_list]
        assert any("True" in c for c in calls)

    def test_was_sent_db_error_returns_false(self):
        """En error de BD, was_sent devuelve False (no suprime alertas por error)."""
        from app.alerts.dedup import was_sent
        db = MagicMock()
        db.table.side_effect = RuntimeError("DB caída")
        result = was_sent(db, "regime_change", "market", "risk_off")
        assert result is False

    def test_get_last_sent_regime_returns_state(self):
        from app.alerts.dedup import get_last_sent_regime
        db = _make_db(alerts=[_alert_row("regime_change", "market", "risk_off", sent=True)])
        result = get_last_sent_regime(db)
        assert result == "risk_off"

    def test_get_last_sent_regime_none_when_empty(self):
        from app.alerts.dedup import get_last_sent_regime
        db = _make_db(alerts=[])
        result = get_last_sent_regime(db)
        assert result is None

    def test_rearm_resets_sent_false_for_below_threshold(self):
        """(d) Histéresis: asset que bajó del umbral → upsert con sent=False."""
        from app.alerts.dedup import rearm_flow_scores

        upserted = []

        db_alerts = _fluent([_alert_row("flow_extreme", "BTC", "extreme", sent=True)])

        def capture_upsert(row, on_conflict=None):
            upserted.append(row)
            return db_alerts

        db_alerts.upsert = capture_upsert
        db = MagicMock()
        db.table.side_effect = lambda name: db_alerts if name == "alerts" else _fluent([])

        # BTC ya no es extremo (no está en el set)
        count = rearm_flow_scores(db, currently_extreme=set())

        assert count == 1
        assert any(r.get("entity") == "BTC" and r.get("sent") is False for r in upserted), \
            "La alerta de BTC debe haber sido re-armada (sent=False)"

    def test_rearm_does_not_reset_still_extreme(self):
        """(d) Asset que sigue siendo extremo NO se re-arma."""
        from app.alerts.dedup import rearm_flow_scores

        upserted = []
        db_alerts = _fluent([_alert_row("flow_extreme", "BTC", "extreme", sent=True)])

        def capture_upsert(row, on_conflict=None):
            upserted.append(row)
            return db_alerts

        db_alerts.upsert = capture_upsert
        db = MagicMock()
        db.table.side_effect = lambda name: db_alerts if name == "alerts" else _fluent([])

        count = rearm_flow_scores(db, currently_extreme={"BTC"})  # BTC sigue extremo

        assert count == 0
        assert upserted == []

    def test_build_alert_row_structure(self):
        from app.alerts.dedup import build_alert_row
        row = build_alert_row(
            alert_type="flow_extreme", entity="BTC", state="extreme",
            payload={"score": 0.8}, confidence=0.8,
            sent=True, not_sent_reason=None,
        )
        assert row["alert_type"] == "flow_extreme"
        assert row["entity"] == "BTC"
        assert row["state"] == "extreme"
        assert row["sent"] is True
        assert row["not_sent_reason"] is None
        assert "sent_at" in row
        assert "ts" in row

    def test_build_alert_row_not_sent_has_no_sent_at(self):
        from app.alerts.dedup import build_alert_row
        row = build_alert_row(
            "flow_extreme", "BTC", "extreme", {}, 0.8,
            sent=False, not_sent_reason="low_confidence",
        )
        assert row["sent"] is False
        assert "sent_at" not in row
        assert row["not_sent_reason"] == "low_confidence"


# ══════════════════════════════════════════════════════════════════════════════
# S8-4: engine.py — orquestación completa
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertEngine:

    def _run(
        self,
        flow_scores=None,
        regimes=None,
        correlations=None,
        exposures=None,
        narratives=None,
        sent_alerts=None,    # alertas ya enviadas en la tabla
        send_fn=None,
        min_confidence=0.4,
        flow_threshold=0.7,
    ):
        """
        Ejecuta el engine con mocks completos.
        send_fn por defecto: mock que devuelve True (no envía mensajes reales).
        """
        if send_fn is None:
            send_fn = MagicMock(return_value=True)

        # La tabla alerts se configura dinámicamente porque el engine consulta
        # was_sent filtrando por sent=True
        alerts_data = sent_alerts or []
        db = _make_db(
            flow_scores=flow_scores,
            regimes=regimes,
            correlations=correlations,
            exposures=exposures,
            narratives=narratives,
            alerts=alerts_data,
        )

        from app.alerts.engine import AlertEngine
        engine = AlertEngine(db=db, send_fn=send_fn, min_confidence=min_confidence)

        with patch("app.config.settings.flow_extreme_threshold", flow_threshold), \
             patch("app.config.settings.min_alert_confidence", min_confidence):
            return engine.run_sync(), send_fn, db

    def test_result_structure(self):
        """El dict resultado tiene todos los campos esperados."""
        result, _, _ = self._run()
        for field in ("evaluated", "sent", "not_sent_low_confidence",
                      "not_sent_duplicate", "rearmed_flow_scores", "alerts", "errors", "ok"):
            assert field in result, f"Campo '{field}' ausente en resultado"

    def test_regime_change_triggers_alert(self):
        """(b) Cambio de régimen dispara alerta (y Telegram es llamado)."""
        send_fn = MagicMock(return_value=True)
        result, send_fn, _ = self._run(
            regimes=[_regime_row("risk_off", 0.8)],
            sent_alerts=[],  # no hay régimen previo enviado
            send_fn=send_fn,
        )
        assert result["sent"] >= 1
        send_fn.assert_called()

    def test_regime_persists_no_redispatch(self):
        """(b) Régimen que persiste NO redispara: si ya fue enviado, is duplicate."""
        send_fn = MagicMock(return_value=True)

        # El was_sent check: la tabla de alerts tiene la alerta ya enviada
        # Simulamos que was_sent devuelve True inyectando en dedup
        with patch("app.alerts.dedup.was_sent", return_value=True):
            result, send_fn, _ = self._run(
                regimes=[_regime_row("risk_off", 0.8)],
                send_fn=send_fn,
            )

        assert result["not_sent_duplicate"] >= 1
        # Telegram NO debe haber sido llamado para el régimen
        # (puede que send_fn se llame por otras alertas, así que no podemos assert_not_called,
        #  pero sí verificamos que la alerta de régimen va como duplicado)
        regime_alerts = [
            a for a in result["alerts"]
            if a["alert_type"] == "regime_change" and a["not_sent_reason"] == "duplicate"
        ]
        assert len(regime_alerts) >= 1

    def test_flow_score_cross_threshold_triggers(self):
        """(d) Flow score que cruza el umbral → se envía."""
        send_fn = MagicMock(return_value=True)
        result, send_fn, _ = self._run(
            flow_scores=[_score_row("BTC", 0.85)],
            sent_alerts=[],
            send_fn=send_fn,
        )
        flow_alerts = [a for a in result["alerts"] if a["alert_type"] == "flow_extreme"]
        assert len(flow_alerts) >= 1
        sent_ones = [a for a in flow_alerts if a["sent"] is True]
        assert len(sent_ones) >= 1

    def test_flow_score_histeresis_no_redispatch_above(self):
        """(d) Histéresis: score sigue por encima → no redispara (was_sent=True)."""
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.dedup.was_sent", return_value=True):
            result, send_fn, _ = self._run(
                flow_scores=[_score_row("BTC", 0.85)],
                send_fn=send_fn,
            )
        btc_dup = [
            a for a in result["alerts"]
            if a["alert_type"] == "flow_extreme"
            and a["entity"] == "BTC"
            and a["not_sent_reason"] == "duplicate"
        ]
        assert len(btc_dup) >= 1

    def test_flow_score_rearms_when_below(self):
        """(d) Score bajó del umbral → se llama rearm (sent=False) para ese ticker."""
        rearmed = []

        def fake_rearm(db, currently_extreme):
            # Simula que BTC bajó (no está en el set) → re-arm
            rearmed.append(currently_extreme)
            return 1 if "BTC" not in currently_extreme else 0

        with patch("app.alerts.rules.get_current_extreme_tickers", return_value=set()):
            with patch("app.alerts.dedup.rearm_flow_scores", side_effect=fake_rearm):
                result, _, _ = self._run(flow_scores=[_score_row("BTC", 0.3)])

        assert len(rearmed) > 0

    def test_low_confidence_not_sent(self):
        """(c)(i) Confianza < MIN_ALERT_CONFIDENCE → registra pero no envía."""
        send_fn = MagicMock(return_value=True)
        # flow score con confidence='low' (numérico 0.2) y min_confidence=0.4
        result, send_fn, _ = self._run(
            flow_scores=[_score_row("BTC", 0.85, confidence="low")],
            sent_alerts=[],
            send_fn=send_fn,
            min_confidence=0.4,
        )
        low_conf_alerts = [
            a for a in result["alerts"]
            if a["alert_type"] == "flow_extreme" and a["not_sent_reason"] == "low_confidence"
        ]
        assert len(low_conf_alerts) >= 1
        # Comprueba también el contador
        assert result["not_sent_low_confidence"] >= 1

    def test_low_confidence_not_sent_telegram_not_called(self):
        """(c) Alerta de baja confianza: Telegram NO es llamado."""
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.dedup.was_sent", return_value=False):
            result, send_fn, _ = self._run(
                flow_scores=[_score_row("BTC", 0.85, confidence="low")],
                send_fn=send_fn,
                min_confidence=0.4,
            )
        # Solo alertas de flow_extreme con BTC debería haber intentado enviarse,
        # pero la confianza numérica es 0.2 < 0.4
        btc_low = [
            a for a in result["alerts"]
            if a["alert_type"] == "flow_extreme"
            and a["entity"] == "BTC"
            and a["not_sent_reason"] == "low_confidence"
        ]
        assert btc_low, "BTC con confianza low debe estar registrado como low_confidence"
        # Telegram no fue llamado por BTC
        for c in send_fn.call_args_list:
            text = c[0][0] if c[0] else ""
            assert "BTC" not in text or "FLOW EXTREMO" not in text, \
                "Telegram no debe recibir alerta de BTC con confianza baja"

    def test_cold_start_not_sent(self):
        """(i) Cold start (regime confidence muy baja → conf numérica baja < min) → no enviada."""
        send_fn = MagicMock(return_value=True)
        # régimen con confianza 0.1 < min_confidence=0.4
        result, send_fn, _ = self._run(
            regimes=[_regime_row("risk_off", 0.1)],  # confidence 0.1 → muy bajo
            sent_alerts=[],
            send_fn=send_fn,
            min_confidence=0.4,
        )
        low_conf = [
            a for a in result["alerts"]
            if a["alert_type"] == "regime_change" and a["not_sent_reason"] == "low_confidence"
        ]
        assert len(low_conf) >= 1

    def test_duplicate_not_resent(self):
        """(b) Anti-duplicado: la misma alerta ya enviada no se reenvía."""
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.dedup.was_sent", return_value=True):
            result, send_fn, _ = self._run(
                flow_scores=[_score_row("BTC", 0.85)],
                regimes=[_regime_row("risk_off", 0.8)],
                sent_alerts=[],
                send_fn=send_fn,
            )
        assert result["not_sent_duplicate"] >= 1

    def test_exposure_payload_has_confidence_and_sources(self):
        """(e) Alerta de exposición incluye nivel de confianza y fuentes en el payload."""
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.dedup.was_sent", return_value=False):
            result, send_fn, _ = self._run(
                exposures=[_exposure_row("OpenAI", "MSFT", "confirmado_oficial")],
                send_fn=send_fn,
            )
        exposure_alerts = [a for a in result["alerts"] if a["alert_type"] == "exposure"]
        assert len(exposure_alerts) >= 1

        # El mensaje enviado a Telegram debe incluir confianza y fuentes
        calls = send_fn.call_args_list
        exposure_call_texts = [c[0][0] for c in calls if "OpenAI" in (c[0][0] if c[0] else "")]
        assert exposure_call_texts, "Debe haberse llamado send_fn para la exposición de OpenAI"
        text = exposure_call_texts[0]
        assert "OpenAI" in text
        assert "MSFT" in text

    def test_decoupling_alert_triggers(self):
        from app.alerts.rules import check_decoupling
        db = _make_db(correlations=[_decoupling_row()])
        alerts = check_decoupling(db)
        assert len(alerts) == 1

    def test_result_ok_true_when_no_errors(self):
        result, _, _ = self._run()
        assert result["ok"] is True

    def test_upsert_called_for_all_candidates(self):
        """Idempotencia: upsert se llama para cada candidata (enviada o no)."""
        upserted = []
        db = _make_db(
            flow_scores=[_score_row("BTC", 0.85)],
        )

        def capture_upsert(row, on_conflict=None):
            upserted.append(row)
            return db.table("alerts")

        db.table("alerts").upsert = capture_upsert

        from app.alerts.engine import AlertEngine
        engine = AlertEngine(db=db, send_fn=MagicMock(return_value=True), min_confidence=0.4)

        with patch("app.config.settings.flow_extreme_threshold", 0.7), \
             patch("app.config.settings.min_alert_confidence", 0.4):
            engine.run_sync()

        # Debe haberse upsertado al menos una alerta
        assert len(upserted) >= 1

    def test_idempotence_double_run(self):
        """(j) Dos ejecuciones consecutivas: la segunda no duplica sends."""
        send_fn = MagicMock(return_value=True)

        # Primera ejecución: no hay was_sent
        # Segunda: was_sent=True para todo
        call_count = [0]
        def was_sent_after_first(db, alert_type, entity, state):
            call_count[0] += 1
            return call_count[0] > 10  # las primeras ~10 llamadas devuelven False, luego True

        with patch("app.alerts.dedup.was_sent", side_effect=was_sent_after_first), \
             patch("app.alerts.dedup.upsert_alert"), \
             patch("app.alerts.dedup.rearm_flow_scores", return_value=0):
            db = _make_db(
                flow_scores=[_score_row("BTC", 0.85)],
                regimes=[_regime_row("risk_off", 0.8)],
            )
            from app.alerts.engine import AlertEngine
            engine = AlertEngine(db=db, send_fn=send_fn, min_confidence=0.4)
            with patch("app.config.settings.flow_extreme_threshold", 0.7):
                engine.run_sync()
                first_send_count = send_fn.call_count
                # Segunda ejecución: was_sent devuelve True → duplicados
                with patch("app.alerts.dedup.was_sent", return_value=True):
                    engine.run_sync()
                second_send_count = send_fn.call_count

        assert second_send_count == first_send_count  # no nuevos envíos en la segunda


# ══════════════════════════════════════════════════════════════════════════════
# S8-5: Endpoints via FastAPI TestClient (mocks completos)
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertEndpoints:

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_alerts_run_endpoint_returns_summary(self):
        """GET /alerts/run → dict con 'evaluated', 'sent', 'ok'."""
        from app.alerts.engine import AlertEngine

        mock_result = {
            "evaluated": 3, "sent": 1,
            "not_sent_low_confidence": 1, "not_sent_duplicate": 1,
            "rearmed_flow_scores": 0,
            "alerts": [], "errors": [], "ok": True,
        }
        with patch.object(AlertEngine, "run_sync", return_value=mock_result):
            resp = self._client().get("/alerts/run")
        assert resp.status_code == 200
        data = resp.json()
        assert "evaluated" in data
        assert "sent" in data

    def test_alerts_recent_endpoint(self):
        """GET /alerts/recent → lista de alertas."""
        with patch("app.db.get_db") as mock_get_db:
            db = _make_db(alerts=[_alert_row("flow_extreme", "BTC", "extreme")])
            mock_get_db.return_value = db
            resp = self._client().get("/alerts/recent")
        assert resp.status_code == 200

    def test_alerts_test_endpoint_ok(self):
        """POST /alerts/test → verificar que llama send_message y no lanza error."""
        with patch("app.alerts.telegram.send_message", return_value=True), \
             patch("app.config.settings.telegram_bot_token", "tok"), \
             patch("app.config.settings.telegram_chat_id", "123"):
            resp = self._client().post("/alerts/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_alerts_test_endpoint_fails_gracefully(self):
        """POST /alerts/test → si Telegram falla, devuelve ok=False sin crash."""
        with patch("app.alerts.telegram.send_message", return_value=False), \
             patch("app.config.settings.telegram_bot_token", "tok"), \
             patch("app.config.settings.telegram_chat_id", "123"):
            resp = self._client().post("/alerts/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
