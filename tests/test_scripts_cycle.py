"""
Tests MAREA Sesión 10 — Scripts de ciclo para GitHub Actions.

Garantías verificadas:
  (a) build_steps (intradía y diario) devuelve los pasos en el ORDEN correcto.
  (b) Cada paso reutiliza el engine existente vía run_sync (sin HTTP).
  (c) run_cycle ejecuta los pasos en orden y, si uno casca, SIGUE con el resto
      (el manejo de errores no impide que se evalúen las alertas).
  (d) Un paso que casca → exit code 1 + notificación de error (Telegram).
  (e) Todos los pasos ok → exit code 0 + NO se notifica error.
  (f) Errores "blandos" de un engine (ok=False / errors no vacío) NO tumban el
      ciclo por sí solos (exit 0): que falle una fuente de datos es normal.
  (g) NINGÚN test hace llamadas reales (ni Telegram, ni APIs): todo mockeado.
"""

from unittest.mock import MagicMock, patch

from scripts import _common
from scripts._common import run_cycle, run_step
from scripts import run_intraday_cycle, run_daily_cycle


# ══════════════════════════════════════════════════════════════════════════════
# run_cycle — orquestación, orden y manejo de errores
# ══════════════════════════════════════════════════════════════════════════════

def test_run_cycle_ejecuta_en_orden_y_devuelve_0():
    calls = []
    steps = [
        ("uno",  lambda: calls.append("uno") or {"ok": True}),
        ("dos",  lambda: calls.append("dos") or {"ok": True}),
        ("tres", lambda: calls.append("tres") or {"ok": True}),
    ]
    notify = MagicMock()

    code = run_cycle("TEST", steps, notify_fn=notify)

    assert code == 0
    assert calls == ["uno", "dos", "tres"]   # orden respetado
    notify.assert_not_called()               # sin errores → no se notifica


def test_run_cycle_un_paso_que_casca_no_impide_los_siguientes():
    calls = []

    def step_dos():
        calls.append("dos")
        raise RuntimeError("boom en dos")

    steps = [
        ("uno",  lambda: calls.append("uno") or {"ok": True}),
        ("dos",  step_dos),
        ("tres", lambda: calls.append("tres") or {"ok": True}),  # alertas: debe correr igual
    ]
    notify = MagicMock()

    code = run_cycle("TEST", steps, notify_fn=notify)

    # El paso 'tres' (p. ej. alertas) se ejecuta pese al fallo en 'dos'.
    assert calls == ["uno", "dos", "tres"]
    assert code == 1                          # algún paso cascó → fallo del workflow
    notify.assert_called_once()               # se avisó por Telegram
    msg = notify.call_args[0][0]
    assert "dos" in msg and "boom en dos" in msg


def test_run_cycle_errores_blandos_no_tumban_el_ciclo():
    # Un engine que terminó pero reporta errores de fuente (ok=False) NO debe
    # marcar el workflow como fallido: es el caso normal de una fuente caída.
    steps = [
        ("ingesta", lambda: {"ok": False, "errors": ["coingecko: timeout"]}),
        ("alertas", lambda: {"ok": True, "errors": []}),
    ]
    notify = MagicMock()

    code = run_cycle("TEST", steps, notify_fn=notify)

    assert code == 0
    notify.assert_not_called()


def test_run_cycle_paso_que_devuelve_none_es_valido():
    code = run_cycle("TEST", [("x", lambda: None)], notify_fn=MagicMock())
    assert code == 0


def test_run_step_captura_excepcion_y_no_propaga():
    def boom():
        raise ValueError("explota")

    outcome = run_step("paso", boom)

    assert outcome.crashed is True
    assert "explota" in outcome.error


def test_run_step_extrae_errores_blandos():
    outcome = run_step("ingesta", lambda: {"ok": False, "errors": ["a", "b"]})
    assert outcome.crashed is False
    assert outcome.soft_errors == ["a", "b"]


def test_notify_error_no_propaga_si_telegram_falla():
    # Si la notificación de error falla, no debe tumbar el proceso.
    def fn_que_falla(_text):
        raise RuntimeError("telegram caído")

    steps = [("x", lambda: (_ for _ in ()).throw(RuntimeError("fallo")))]
    # No debe lanzar pese a que notify_fn casca:
    code = run_cycle("TEST", steps, notify_fn=fn_que_falla)
    assert code == 1


def test_notify_error_usa_telegram_real_si_no_hay_notify_fn():
    # Sin notify_fn, _notify_error debe usar el bot real (mockeado aquí).
    steps = [("x", lambda: (_ for _ in ()).throw(RuntimeError("fallo")))]
    with patch("app.alerts.telegram.send_message", return_value=True) as send:
        code = run_cycle("TEST", steps)   # notify_fn=None
    assert code == 1
    send.assert_called_once()
    text = send.call_args[0][0]
    assert "error en ciclo TEST" in text


# ══════════════════════════════════════════════════════════════════════════════
# build_steps — orden de la cadena y reutilización de engines
# ══════════════════════════════════════════════════════════════════════════════

def test_intraday_build_steps_orden_correcto():
    steps = run_intraday_cycle.build_steps(db=MagicMock())
    nombres = [name for name, _ in steps]
    assert nombres == [
        "ingesta_intradia",
        "scores_intradia",
        "analisis_intradia",
        "alertas",
    ]


def test_daily_build_steps_orden_correcto():
    steps = run_daily_cycle.build_steps(db=MagicMock())
    nombres = [name for name, _ in steps]
    assert nombres == [
        "ingesta_diaria",
        "recompute_universo",
        "scores_diarios",
        "analisis_diario",
        "narrativa",
        "alertas",
    ]


def test_intraday_steps_invocan_los_engines_correctos():
    # Cada paso debe instanciar su engine y llamar a run_sync, sin tocar HTTP.
    db = MagicMock()
    with patch("app.ingest.intraday_runner.IntradayRunner") as Runner, \
         patch("app.scoring.intraday_engine.IntradayScoreEngine") as Scorer, \
         patch("app.analysis.intraday.IntradayAnalysisEngine") as Analyzer, \
         patch("app.alerts.engine.AlertEngine") as Alerter:
        for cls in (Runner, Scorer, Analyzer, Alerter):
            cls.return_value.run_sync.return_value = {"ok": True}

        steps = run_intraday_cycle.build_steps(db=db)
        for _name, fn in steps:
            fn()

        Runner.return_value.run_sync.assert_called_once()
        Scorer.return_value.run_sync.assert_called_once()
        Analyzer.return_value.run_sync.assert_called_once()
        Alerter.return_value.run_sync.assert_called_once()


def test_daily_steps_invocan_los_engines_correctos():
    db = MagicMock()
    with patch("app.ingest.run_all.IngestAll") as Ingest, \
         patch("app.universe.dynamic.UniverseRecomputer") as Universe, \
         patch("app.scoring.engine.ScoreEngine") as Scorer, \
         patch("app.analysis.engine.AnalysisEngine") as Analyzer, \
         patch("app.narrative.engine.NarrativeEngine") as Narrative, \
         patch("app.alerts.engine.AlertEngine") as Alerter:
        for cls in (Ingest, Universe, Scorer, Analyzer, Narrative, Alerter):
            cls.return_value.run_sync.return_value = {"ok": True}

        steps = run_daily_cycle.build_steps(db=db)
        for _name, fn in steps:
            fn()

        Ingest.return_value.run_sync.assert_called_once()
        Universe.return_value.run_sync.assert_called_once()
        Scorer.return_value.run_sync.assert_called_once()
        Analyzer.return_value.run_sync.assert_called_once()
        Narrative.return_value.run_sync.assert_called_once()
        Alerter.return_value.run_sync.assert_called_once()


def test_main_intraday_devuelve_exit_code(monkeypatch):
    # main() no debe hacer llamadas reales: mockeamos build_steps con pasos ok.
    monkeypatch.setattr(
        run_intraday_cycle, "build_steps",
        lambda: [("falso", lambda: {"ok": True})],
    )
    assert run_intraday_cycle.main() == 0


def test_main_daily_propaga_fallo(monkeypatch):
    monkeypatch.setattr(
        run_daily_cycle, "build_steps",
        lambda: [("falso", lambda: (_ for _ in ()).throw(RuntimeError("x")))],
    )
    with patch("app.alerts.telegram.send_message", return_value=True):
        assert run_daily_cycle.main() == 1
