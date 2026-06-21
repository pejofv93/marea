"""
Tests MAREA Sesión 11 — Mensaje-resumen por ciclo en Telegram.

Garantías verificadas:
  (a) build_daily_digest / build_intraday_digest componen el texto a partir de
      estado sintético (régimen, scores, movimientos).
  (b) La coletilla de "datos preliminares" aparece en cold start / baja
      confianza, y NO aparece cuando la confianza es ok.
  (c) El resumen se envía una vez por ciclo (Telegram mockeado → 1 llamada).
  (d) Un fallo de envío de Telegram NO rompe el ciclo (ok=True, error blando).
  (e) DIGEST_ENABLED=false → no se envía.
  (f) Reutiliza el cliente Telegram existente (no se duplica el envío).
  (g) NINGÚN test hace llamadas reales (ni Telegram, ni BD).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.alerts import digest
from app.alerts.digest import (
    build_daily_digest,
    build_intraday_digest,
    send_daily_digest,
    send_intraday_digest,
)


# ══════════════════════════════════════════════════════════════════════════════
# Estado sintético
# ══════════════════════════════════════════════════════════════════════════════

def _snapshot(cold=False, conf=0.8, regime="risk_on"):
    return {
        "regime": {
            "name": regime,
            "confidence": conf,
            "signals": ["crypto_inflow", "dxy_falling"],
            "ts": "2026-06-21T00:00:00Z",
        },
        "top_inflow": [
            {"ticker": "BTC-USD", "score": 0.82, "confidence": "ok"},
            {"ticker": "ETH-USD", "score": 0.61, "confidence": "ok"},
        ],
        "top_outflow": [
            {"ticker": "GLD", "score": -0.55, "confidence": "ok"},
        ],
        "rotations": [{"from": "tech", "to": "energy", "strength": 0.34}],
        "cold_start": cold,
    }


def _intraday(strong_in=None, strong_out=None, ok_conf=True):
    conf = "ok" if ok_conf else "low"
    movements = [
        {"ticker": "BTC-USD", "direction": "inflow", "score": 0.7, "confidence": conf},
    ]
    return {
        "movements": movements,
        "strong_inflow": strong_in or [],
        "strong_outflow": strong_out or [],
        "summary": "",
        "errors": [],
        "ok": True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Composición — DIARIO
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_digest_incluye_regimen_signals_y_scores():
    text = build_daily_digest(_snapshot(), narrative="El mercado muestra apetito por riesgo.")
    assert "Risk-ON" in text                      # nombre traducido
    assert "confianza 80%" in text                # confianza real
    assert "entrada a crypto" in text             # señal traducida
    assert "dólar debilitándose" in text
    assert "BTC-USD" in text and "+0.82" in text  # top inflow con valor
    assert "GLD" in text and "-0.55" in text      # top outflow
    assert "tech → energy" in text                # rotación
    assert "apetito por riesgo" in text           # línea de narrativa
    assert "no es consejo de inversión" in text   # sello


def test_daily_digest_marca_cold_start():
    text = build_daily_digest(_snapshot(cold=True))
    assert "Datos preliminares" in text


def test_daily_digest_marca_baja_confianza():
    text = build_daily_digest(_snapshot(cold=False, conf=0.2))
    assert "Datos preliminares" in text


def test_daily_digest_sin_coletilla_cuando_confianza_ok():
    text = build_daily_digest(_snapshot(cold=False, conf=0.8))
    assert "Datos preliminares" not in text


def test_daily_digest_sin_regimen():
    snap = _snapshot()
    snap["regime"] = None
    text = build_daily_digest(snap)
    assert "Datos preliminares" in text           # sin régimen = preliminar
    assert "sin determinar" in text


# ══════════════════════════════════════════════════════════════════════════════
# Composición — INTRADÍA
# ══════════════════════════════════════════════════════════════════════════════

def test_intraday_digest_con_movimientos():
    text = build_intraday_digest(
        _intraday(strong_in=["BTC-USD"], strong_out=["GLD"]),
        context={"dxy": -0.3, "vix": 0.5},
        moment="Apertura USA",
    )
    assert "Apertura USA" in text
    assert "Entradas fuertes:</b> BTC-USD" in text
    assert "Salidas fuertes:</b> GLD" in text
    assert "DXY -0.30" in text and "VIX +0.50" in text
    assert "no es consejo de inversión" in text
    assert "Datos preliminares" not in text       # hay movimiento con conf ok


def test_intraday_digest_sin_movimientos():
    text = build_intraday_digest(_intraday(), moment="Tarde USA")
    assert "Sin movimientos intradía fuertes." in text


def test_intraday_digest_marca_preliminar_si_todo_low():
    text = build_intraday_digest(_intraday(ok_conf=False), moment="Media sesión USA")
    assert "Datos preliminares" in text


def test_intraday_moment_derivado_de_hora_utc():
    assert digest._intraday_moment(13) == "Apertura USA"
    assert digest._intraday_moment(16) == "Media sesión USA"
    assert digest._intraday_moment(18) == "Tarde USA"


# ══════════════════════════════════════════════════════════════════════════════
# Envío — DIARIO
# ══════════════════════════════════════════════════════════════════════════════

def _db_for_daily():
    db = MagicMock()
    with patch("app.narrative.snapshot.build_snapshot", return_value=_snapshot()):
        pass
    return db


def test_send_daily_digest_envia_una_vez():
    send_fn = MagicMock(return_value=True)
    with patch("app.narrative.snapshot.build_snapshot", return_value=_snapshot()), \
         patch("app.alerts.digest._latest_narrative", return_value=None):
        res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["ok"] is True
    assert res["sent"] is True
    send_fn.assert_called_once()


def test_send_daily_digest_fallo_envio_no_rompe():
    send_fn = MagicMock(return_value=False)   # Telegram rechaza
    with patch("app.narrative.snapshot.build_snapshot", return_value=_snapshot()), \
         patch("app.alerts.digest._latest_narrative", return_value=None):
        res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["ok"] is True                  # el ciclo NO se rompe
    assert res["sent"] is False
    assert "telegram_send_failed" in res["errors"]


def test_send_daily_digest_excepcion_no_propaga():
    with patch("app.narrative.snapshot.build_snapshot", side_effect=RuntimeError("boom")):
        res = send_daily_digest(db=MagicMock(), send_fn=MagicMock(return_value=True))
    assert res["ok"] is True
    assert res["sent"] is False
    assert any("boom" in e for e in res["errors"])


def test_send_daily_digest_respeta_digest_enabled_false(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "digest_enabled", False)
    send_fn = MagicMock(return_value=True)
    res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["enabled"] is False
    assert res["sent"] is False
    send_fn.assert_not_called()


def test_send_daily_digest_usa_cliente_telegram_real_si_no_hay_send_fn():
    # Sin send_fn, debe usar send_message del cliente existente (mockeado).
    with patch("app.narrative.snapshot.build_snapshot", return_value=_snapshot()), \
         patch("app.alerts.digest._latest_narrative", return_value=None), \
         patch("app.alerts.telegram.send_message", return_value=True) as send:
        res = send_daily_digest(db=MagicMock())
    assert res["sent"] is True
    send.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Envío — INTRADÍA
# ══════════════════════════════════════════════════════════════════════════════

def test_send_intraday_digest_envia_una_vez():
    send_fn = MagicMock(return_value=True)
    with patch("app.alerts.digest._intraday_context", return_value={"dxy": None, "vix": None}):
        res = send_intraday_digest(
            db=MagicMock(),
            analysis=_intraday(strong_in=["BTC-USD"]),
            hour_utc=13,
            send_fn=send_fn,
        )
    assert res["ok"] is True and res["sent"] is True
    send_fn.assert_called_once()
    # El texto enviado contiene el momento derivado de la hora.
    assert "Apertura USA" in send_fn.call_args[0][0]


def test_send_intraday_digest_fallo_no_rompe():
    send_fn = MagicMock(return_value=False)
    with patch("app.alerts.digest._intraday_context", return_value={"dxy": None, "vix": None}):
        res = send_intraday_digest(db=MagicMock(), analysis=_intraday(), hour_utc=16, send_fn=send_fn)
    assert res["ok"] is True
    assert res["sent"] is False
    assert "telegram_send_failed" in res["errors"]


def test_send_intraday_digest_respeta_digest_enabled_false(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "digest_enabled", False)
    send_fn = MagicMock(return_value=True)
    res = send_intraday_digest(db=MagicMock(), analysis=_intraday(), send_fn=send_fn)
    assert res["enabled"] is False
    send_fn.assert_not_called()
