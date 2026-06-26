"""
Tests MAREA Sesión 12 — Rediseño de los partes de Telegram ("sigue la liquidez").

Garantías verificadas (entregables del rediseño):
  (a) NINGÚN flujo queda a medias: toda salida fuerte trae destino o "en espera".
  (b) La pólvora de stablecoins SIEMPRE se cierra (3 casos: a crypto / a otro
      lado / en espera) + acumulación.
  (c) "Quién manda" dictamina la fuerza mayor o declara "señales cruzadas".
  (d) Crypto SIEMPRE con nombres y dirección concretos.
  (e) Comparación temporal: sin parte anterior → lo dice; con parte anterior →
      calcula deltas y cierra cada salida con destino.
  (f) Afirmativo en flujo (entra/sale) vs condicional en destino (parece/apunta).
  (g) Nombres reales, graduación de intensidad y semáforo correctos.
  (h) DIGEST_ENABLED sigue funcionando; los senders nunca rompen el ciclo.
  (i) NINGÚN test hace llamadas reales (ni Telegram, ni BD).
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

def _a(ticker, score, asset_class="etf", sector=None, confidence="ok", name=None):
    return {"ticker": ticker, "name": name, "asset_class": asset_class,
            "sector": sector, "score": score, "confidence": confidence}


def _state(assets=None, regime="risk_on", conf=0.8, cold=False, rotations=None):
    return {
        "assets": assets if assets is not None else [
            _a("XLF", -0.92, "etf", "financials"),
            _a("^GSPC", 0.88, "index"),
            _a("DX-Y.NYB", 0.60, "macro", "currency"),
            _a("BTC", 0.55, "crypto"),
            _a("ETH", -0.20, "crypto"),
            _a("STABLES_USDT", -0.70, "onchain", "stablecoin"),
        ],
        "regime": None if regime is None else {
            "name": regime, "confidence": conf,
            "signals": ["crypto_inflow", "dxy_rising"],
        },
        "cold_start": cold,
        "rotations": rotations if rotations is not None else [{"from": "tech", "to": "energy", "strength": 0.34}],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers puros
# ══════════════════════════════════════════════════════════════════════════════

def test_nombres_reales_no_tickers():
    assert digest._name(_a("^GSPC", 0.1)) == "S&P 500"
    assert digest._name(_a("DX-Y.NYB", 0.1)) == "Dólar (DXY)"
    assert digest._name(_a("GC=F", 0.1)) == "Oro"
    assert digest._name(_a("XLF", 0.1)) == "Financieras (bancos)"
    assert digest._name(_a("BTC", 0.1)) == "Bitcoin"
    # Fallback: ticker desconocido cae a assets.name, luego al propio ticker.
    assert digest._name(_a("ZZZ", 0.1, name="Cosa Rara")) == "Cosa Rara"
    assert digest._name(_a("ZZZ", 0.1)) == "ZZZ"


def test_graduacion_intensidad():
    assert digest._intensity(0.9) == "fuerte"
    assert digest._intensity(-0.86) == "fuerte"
    assert digest._intensity(0.6) == "moderada"
    assert digest._intensity(-0.5) == "moderada"
    assert digest._intensity(0.3) == "leve"
    assert digest._intensity(0.0) == "leve"


def test_clasificacion_activos():
    assert digest._classify(_a("STABLES_USDC", 0.1, "onchain", "stablecoin")) == "stable"
    assert digest._classify(_a("BTC", 0.1, "crypto")) == "crypto"
    assert digest._classify(_a("IBIT", 0.1, "etf", "crypto")) == "crypto"
    assert digest._classify(_a("GC=F", 0.1, "commodity")) == "safe"
    assert digest._classify(_a("^GSPC", 0.1, "index")) == "risk"


@pytest.mark.parametrize("maxscore,expected", [(0.3, "🟢"), (0.6, "🟡"), (0.9, "🔴")])
def test_semaforo_por_umbral(maxscore, expected):
    assets = [_a("^GSPC", maxscore, "index")]
    assert digest._semaphore(assets, None, cold_start=False, rotation_strength=0.0) == expected


def test_semaforo_cold_start_nunca_rojo():
    assets = [_a("^GSPC", 0.95, "index")]
    # Aunque haya un score fortísimo, en cold start no se pinta rojo.
    assert digest._semaphore(assets, None, cold_start=True, rotation_strength=0.9) == "🟡"


def test_semaforo_rojo_por_regimen_estresado():
    assets = [_a("^GSPC", 0.2, "index")]
    regime = {"name": "flight_to_safety", "confidence": 0.8}
    assert digest._semaphore(assets, regime, cold_start=False, rotation_strength=0.0) == "🔴"


# ══════════════════════════════════════════════════════════════════════════════
# Parte DIARIO — estructura y bloques
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_estructura_y_nombres():
    text = build_daily_digest(_state(), narrative="El mercado rota hacia bolsa USA.")
    assert "📊 <b>MAREA — Cierre de mercado</b>" in text
    assert "S&P 500" in text and "+0.88" in text          # nombre real + valor
    assert "Financieras (bancos)" in text                 # XLF traducido
    assert "🔥 <b>Lo más fuerte:</b>" in text
    assert "🟢 <b>Más entrada de liquidez:</b>" in text
    assert "🔴 <b>Más salida de liquidez:</b>" in text
    assert "📈 <b>Fondo:</b>" in text and "confianza 80%" in text
    assert "El mercado rota hacia bolsa USA." in text      # color de Groq
    assert "no es consejo de inversión" in text            # sello


def test_daily_semaforo_en_titular():
    text = build_daily_digest(_state())          # XLF -0.92 → fuerte → rojo
    assert text.startswith("🔴")


def test_daily_cold_start_coletilla():
    text = build_daily_digest(_state(cold=True))
    assert "Datos preliminares" in text


def test_daily_baja_confianza_coletilla():
    text = build_daily_digest(_state(conf=0.2))
    assert "Datos preliminares" in text


def test_daily_sin_coletilla_cuando_confianza_ok():
    text = build_daily_digest(_state(conf=0.8, cold=False))
    assert "Datos preliminares" not in text


def test_daily_sin_regimen_es_preliminar():
    text = build_daily_digest(_state(regime=None))
    assert "Datos preliminares" in text
    assert "sin determinar" in text


# ── (a) No dejar nada a medias ────────────────────────────────────────────────

def test_outflow_fuerte_siempre_cierra_con_destino_o_espera():
    # Salida fuerte de XLF con receptores claros (S&P, dólar) → destino nombrado.
    text = build_daily_digest(_state())
    # La línea de salida de XLF debe cerrar el círculo.
    assert "Financieras (bancos)" in text
    assert ("parece dirigirse a" in text) or ("capital en espera" in text)


def test_outflow_fuerte_sin_receptor_declara_en_espera():
    # Único movimiento: una salida fuerte y nada que reciba con fuerza.
    assets = [_a("XLF", -0.9, "etf", "financials"), _a("XLV", -0.05, "etf", "healthcare")]
    text = build_daily_digest(_state(assets=assets, regime=None))
    assert "capital en espera" in text                     # cierra como "en espera"
    assert "parece dirigirse a" not in text.split("Más salida")[1].split("💰")[0]


def test_destino_es_condicional_no_afirmativo():
    # El destino inferido usa lenguaje condicional ("parece"), nunca rastreo literal.
    text = build_daily_digest(_state())
    assert "parece dirigirse a" in text
    # El flujo en sí SÍ es afirmativo:
    assert "sale" in text and "entra" in text


# ── (b) Pólvora: tres cierres ─────────────────────────────────────────────────

def _powder_assets(stable_score, btc_score, gspc_score):
    return [
        _a("STABLES_USDT", stable_score, "onchain", "stablecoin"),
        _a("BTC", btc_score, "crypto"),
        _a("^GSPC", gspc_score, "index"),
    ]


def test_polvora_libera_hacia_crypto():
    # Stablecoins caen y crypto recibe → "entra en crypto".
    line = digest._powder_line(_powder_assets(-0.8, 0.7, 0.1))
    assert "dispara hacia crypto" in line
    assert "Bitcoin" in line


def test_polvora_libera_hacia_otro_lado():
    # Stablecoins caen, crypto NO recibe pero bolsa sí → "no va a crypto".
    line = digest._powder_line(_powder_assets(-0.8, -0.1, 0.7))
    assert "NO va a crypto" in line
    assert "S&P 500" in line


def test_polvora_libera_en_espera():
    # Stablecoins caen y nadie recibe con fuerza → "en espera".
    line = digest._powder_line(_powder_assets(-0.8, 0.1, 0.1))
    assert "en espera" in line


def test_polvora_acumulacion():
    # Stablecoins SUBEN → pólvora acumulándose (capital aparcado).
    line = digest._powder_line(_powder_assets(0.7, 0.1, 0.1))
    assert "acumula pólvora" in line


def test_crypto_siempre_con_nombres_y_direccion():
    text = build_daily_digest(_state())
    assert "💰 <b>En crypto:</b>" in text
    assert "Bitcoin entra" in text
    assert "Ethereum sale" in text
    # Prohibido vaguedades:
    assert "en pausa" not in text


def test_crypto_sin_datos_no_queda_vago():
    assets = [_a("^GSPC", 0.5, "index"), _a("STABLES_USDT", -0.6, "onchain", "stablecoin")]
    text = build_daily_digest(_state(assets=assets))
    assert "sin datos de crypto" in text                   # explícito, no vago


# ── (c) Quién manda ───────────────────────────────────────────────────────────

def test_quien_manda_dictamina_la_mayor():
    # Salida -0.9 domina a entrada +0.5 → manda la salida.
    assets = [_a("XLF", -0.9, "etf", "financials"), _a("^GSPC", 0.5, "index")]
    line = digest._who_dominates(assets)
    assert "domina la salida de Financieras (bancos)" in line


def test_quien_manda_empate_es_senales_cruzadas():
    # Fuerzas casi iguales (0.88 vs 0.92) → señales cruzadas, sin lectura.
    assets = [_a("XLF", -0.92, "etf", "financials"), _a("^GSPC", 0.88, "index")]
    line = digest._who_dominates(assets)
    assert "señales cruzadas sin dirección clara" in line


def test_quien_manda_solo_entradas():
    assets = [_a("^GSPC", 0.7, "index"), _a("BTC", 0.3, "crypto")]
    line = digest._who_dominates(assets)
    assert "domina la entrada en S&P 500" in line
    assert "apetito por riesgo" in line


# ── (e) Comparación temporal ──────────────────────────────────────────────────

def test_comparacion_sin_parte_anterior():
    text = build_daily_digest(_state(), compare=None)
    assert "sin parte anterior suficiente para comparar" in text


def test_comparacion_calcula_deltas_y_cierra_salidas():
    compare = {"label": "la media sesión", "scores": {"XLF": -0.3, "^GSPC": 0.5, "BTC": -0.1}}
    text = build_daily_digest(_state(), compare=compare)
    assert "🔄 vs. la media sesión" in text                # subtítulo
    assert "Cambio desde la media sesión" in text
    # BTC pasó de -0.1 a +0.55 → giro a ENTRADA.
    assert "Bitcoin gira a ENTRADA" in text
    # XLF intensifica salida (-0.3 → -0.92) → cierra con destino.
    assert "Financieras (bancos) intensifica salida" in text
    assert "parece dirigirse a" in text


def test_comparacion_sin_cambios_relevantes():
    # Mismos scores → deltas ~0 → "sin cambios relevantes".
    same = {"label": "la apertura", "scores": {a["ticker"]: a["score"] for a in _state()["assets"]}}
    text = build_daily_digest(_state(), compare=same)
    assert "Sin cambios relevantes" in text


# ══════════════════════════════════════════════════════════════════════════════
# Parte INTRADÍA — versión corta
# ══════════════════════════════════════════════════════════════════════════════

def test_intraday_estructura_corta():
    assets = [_a("BTC", 0.7, "crypto"), _a("XLF", -0.8, "etf", "financials"), _a("^GSPC", 0.6, "index")]
    text = build_intraday_digest({"assets": assets}, moment="Apertura USA (intradía)")
    assert "📡 <b>MAREA — Apertura USA (intradía)</b>" in text
    assert "🟢 <b>Top entradas:</b>" in text
    assert "🔴 <b>Top salidas:</b>" in text
    assert "💰 <b>En crypto:</b>" in text
    assert "no es consejo de inversión" in text
    # No lleva el bloque largo de fondo (es del diario).
    assert "📈 <b>Fondo:</b>" not in text


def test_intraday_preliminar_si_todo_low():
    text = build_intraday_digest({"assets": [_a("BTC", 0.7, "crypto", confidence="low")]},
                                 moment="Media sesión USA")
    assert "Datos preliminares" in text


def test_intraday_no_preliminar_con_confianza_ok():
    text = build_intraday_digest({"assets": [_a("BTC", 0.7, "crypto", confidence="ok")]},
                                 moment="Apertura USA")
    assert "Datos preliminares" not in text


def test_intraday_moment_derivado_de_hora_utc():
    assert digest._intraday_moment(13) == "Apertura USA"
    assert digest._intraday_moment(16) == "Media sesión USA"
    assert digest._intraday_moment(18) == "Tarde USA"


# ══════════════════════════════════════════════════════════════════════════════
# Envío — DIARIO (nunca rompe el ciclo, persiste y compara)
# ══════════════════════════════════════════════════════════════════════════════

def _patch_daily(assets=None, prev=None, narrative=None):
    snap = {"regime": {"name": "risk_on", "confidence": 0.8}, "cold_start": False, "rotations": []}
    return (
        patch("app.narrative.snapshot.build_snapshot", return_value=snap),
        patch("app.alerts.digest._load_daily_assets", return_value=assets or []),
        patch("app.alerts.digest._load_prev_cycle", return_value=prev),
        patch("app.alerts.digest._save_cycle"),
        patch("app.alerts.digest._latest_narrative", return_value=narrative),
    )


def test_send_daily_envia_una_vez_y_persiste():
    send_fn = MagicMock(return_value=True)
    p1, p2, p3, p4, p5 = _patch_daily(assets=[_a("^GSPC", 0.6, "index")])
    with p1, p2, p3, p4 as save, p5:
        res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["ok"] is True and res["sent"] is True
    send_fn.assert_called_once()
    save.assert_called_once()                              # persiste el ciclo para comparar luego


def test_send_daily_fallo_envio_no_rompe():
    send_fn = MagicMock(return_value=False)
    p1, p2, p3, p4, p5 = _patch_daily()
    with p1, p2, p3, p4, p5:
        res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["ok"] is True
    assert res["sent"] is False
    assert "telegram_send_failed" in res["errors"]


def test_send_daily_excepcion_no_propaga():
    with patch("app.narrative.snapshot.build_snapshot", side_effect=RuntimeError("boom")):
        res = send_daily_digest(db=MagicMock(), send_fn=MagicMock(return_value=True))
    assert res["ok"] is True
    assert res["sent"] is False
    assert any("boom" in e for e in res["errors"])


def test_send_daily_respeta_digest_enabled_false(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "digest_enabled", False)
    send_fn = MagicMock(return_value=True)
    res = send_daily_digest(db=MagicMock(), send_fn=send_fn)
    assert res["enabled"] is False
    assert res["sent"] is False
    send_fn.assert_not_called()


def test_send_daily_usa_cliente_telegram_real_si_no_hay_send_fn():
    p1, p2, p3, p4, p5 = _patch_daily(assets=[_a("^GSPC", 0.6, "index")])
    with p1, p2, p3, p4, p5, \
         patch("app.alerts.telegram.send_message", return_value=True) as send:
        res = send_daily_digest(db=MagicMock())
    assert res["sent"] is True
    send.assert_called_once()


def test_send_daily_usa_parte_anterior_para_comparar():
    send_fn = MagicMock(return_value=True)
    prev = {"label": "la media sesión", "scores": {"^GSPC": 0.1}}
    p1, p2, p3, p4, p5 = _patch_daily(assets=[_a("^GSPC", 0.7, "index")], prev=prev)
    with p1, p2, p3, p4, p5:
        send_daily_digest(db=MagicMock(), send_fn=send_fn)
    text = send_fn.call_args[0][0]
    assert "🔄 vs. la media sesión" in text


# ══════════════════════════════════════════════════════════════════════════════
# Envío — INTRADÍA
# ══════════════════════════════════════════════════════════════════════════════

def _intraday_analysis(*assets):
    return {"movements": [
        {"ticker": t, "asset_class": c, "score": s, "confidence": conf}
        for (t, s, c, conf) in assets
    ], "strong_inflow": [], "strong_outflow": [], "errors": [], "ok": True}


def test_send_intraday_envia_una_vez_y_deriva_momento():
    send_fn = MagicMock(return_value=True)
    analysis = _intraday_analysis(("BTC", 0.7, "crypto", "ok"))
    with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
         patch("app.alerts.digest._save_cycle"):
        res = send_intraday_digest(db=MagicMock(), analysis=analysis, hour_utc=13, send_fn=send_fn)
    assert res["ok"] is True and res["sent"] is True
    send_fn.assert_called_once()
    assert "Apertura USA" in send_fn.call_args[0][0]


def test_send_intraday_fallo_no_rompe():
    send_fn = MagicMock(return_value=False)
    with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
         patch("app.alerts.digest._save_cycle"):
        res = send_intraday_digest(db=MagicMock(), analysis=_intraday_analysis(), hour_utc=16, send_fn=send_fn)
    assert res["ok"] is True
    assert res["sent"] is False
    assert "telegram_send_failed" in res["errors"]


def test_send_intraday_respeta_digest_enabled_false(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "digest_enabled", False)
    send_fn = MagicMock(return_value=True)
    res = send_intraday_digest(db=MagicMock(), analysis=_intraday_analysis(), send_fn=send_fn)
    assert res["enabled"] is False
    send_fn.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Persistencia / comparación (helpers de BD, todo mockeado)
# ══════════════════════════════════════════════════════════════════════════════

def test_load_prev_cycle_prefiere_momento_mapeado():
    db = MagicMock()
    db.table().select().eq().order().limit().execute.return_value.data = [
        {"moment": "apertura", "scores": [{"ticker": "BTC", "score": 0.2}]},
        {"moment": "media", "scores": [{"ticker": "BTC", "score": 0.9}]},
    ]
    # cierre → mapea a 'media'
    prev = digest._load_prev_cycle(db, "daily", "cierre")
    assert prev["scores"]["BTC"] == 0.9
    assert prev["label"] == "la media sesión"


def test_load_prev_cycle_sin_filas_es_none():
    db = MagicMock()
    db.table().select().eq().order().limit().execute.return_value.data = []
    assert digest._load_prev_cycle(db, "daily", "cierre") is None


def test_save_cycle_no_propaga_si_db_falla():
    db = MagicMock()
    db.table.side_effect = RuntimeError("db caída")
    # No debe lanzar:
    digest._save_cycle(db, "daily", "cierre", [_a("BTC", 0.5, "crypto")])
