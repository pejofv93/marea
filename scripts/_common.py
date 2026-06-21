"""
Motor de ejecución de ciclos compartido por los scripts de entrada.

Un "ciclo" es una cadena ordenada de pasos (ingesta → scores → análisis →
alertas). Cada paso es un engine ya existente que se reutiliza tal cual.

Garantías de robustez (clave para algo desatendido en GitHub Actions):

  * Cada paso se ejecuta dentro de un try/except: si un paso casca con una
    excepción no controlada, se registra y el ciclo CONTINÚA con el siguiente
    paso (así, p. ej., las alertas se evalúan aunque la ingesta fallase).
  * Los engines ya capturan internamente errores por fuente/paso y devuelven
    un dict con ``ok``/``errors``; esos errores "blandos" se loguean pero NO
    tumban el workflow (que falle una sola fuente de datos es normal).
  * Si algún paso casca del todo (excepción), el ciclo termina con exit code
    distinto de 0 para que GitHub Actions lo marque como fallido, y además
    intenta avisar por Telegram con un mensaje breve, para que el usuario se
    entere sin tener que mirar GitHub.

Nada aquí hace llamadas HTTP a la propia app: se invoca la lógica interna.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("marea.cycle")


# ══════════════════════════════════════════════════════════════════════════════
# Resultado de cada paso y del ciclo completo
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StepOutcome:
    name: str
    crashed: bool = False          # True si el paso lanzó una excepción no controlada
    result: dict | None = None     # dict devuelto por el engine (si no cascó)
    error: str | None = None       # mensaje de la excepción, si crashed

    @property
    def soft_errors(self) -> list[str]:
        """Errores "blandos" reportados por el engine (fuente/paso que falló pero no abortó)."""
        if not isinstance(self.result, dict):
            return []
        return list(self.result.get("errors") or [])


@dataclass
class CycleReport:
    cycle: str
    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def crashed(self) -> list[StepOutcome]:
        return [s for s in self.steps if s.crashed]

    @property
    def ok(self) -> bool:
        """El ciclo es 'ok' si ningún paso cascó (los errores blandos no cuentan)."""
        return not self.crashed


# Un paso es un par (nombre, función-sin-argumentos que devuelve un dict o None).
Step = tuple[str, Callable[[], object]]


# ══════════════════════════════════════════════════════════════════════════════
# Ejecución
# ══════════════════════════════════════════════════════════════════════════════

def run_step(name: str, fn: Callable[[], object]) -> StepOutcome:
    """Ejecuta un paso capturando cualquier excepción. Nunca propaga."""
    logger.info("▶ paso: %s …", name)
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 — desatendido: capturamos todo y seguimos
        logger.error("✖ paso '%s' cascó: %s", name, e)
        logger.error(traceback.format_exc())
        return StepOutcome(name=name, crashed=True, error=str(e))

    if isinstance(result, dict):
        soft = result.get("errors") or []
        if soft:
            logger.warning(
                "⚠ paso '%s' terminó con %d error(es) de fuente/paso (no aborta): %s",
                name, len(soft), soft,
            )
        else:
            logger.info("✔ paso '%s' ok", name)
        return StepOutcome(name=name, result=result)

    logger.info("✔ paso '%s' ok", name)
    return StepOutcome(name=name, result=result if isinstance(result, dict) else None)


def run_cycle(
    cycle_name: str,
    steps: list[Step],
    notify_fn: Callable[[str], object] | None = None,
) -> int:
    """
    Ejecuta los pasos en orden, capturando errores por paso y continuando.

    Devuelve un exit code apto para ``sys.exit``:
      * 0  → ningún paso cascó (puede haber errores blandos de alguna fuente).
      * 1  → al menos un paso cascó con una excepción → se avisa por Telegram.

    ``notify_fn`` es inyectable en tests (firma: ``(text) -> bool``). En
    producción, si es None, se usa el bot de Telegram real.
    """
    logger.info("══════ Inicio ciclo %s ══════", cycle_name)
    report = CycleReport(cycle=cycle_name)

    for name, fn in steps:
        report.steps.append(run_step(name, fn))

    crashed = report.crashed
    logger.info(
        "══════ Fin ciclo %s: %d pasos, %d cascados ══════",
        cycle_name, len(report.steps), len(crashed),
    )

    if crashed:
        _notify_error(cycle_name, crashed, notify_fn)
        return 1
    return 0


def _notify_error(
    cycle_name: str,
    crashed: list[StepOutcome],
    notify_fn: Callable[[str], object] | None = None,
) -> None:
    """
    Avisa por Telegram (mensaje breve) de que el ciclo tuvo un error.

    Va envuelto en su propio try/except: si Telegram falla, NO debe enmascarar
    el error original ni tumbar el proceso (que ya devolverá exit code 1).
    """
    names = ", ".join(s.name for s in crashed)
    detail = crashed[0].error or "sin detalle"
    text = (
        f"🔴 <b>MAREA — error en ciclo {cycle_name}</b>\n"
        f"Paso(s) con fallo: <b>{names}</b>\n"
        f"Detalle: <code>{detail}</code>\n"
        "Revisa los logs en GitHub Actions (pestaña «Actions»)."
    )
    try:
        if notify_fn is not None:
            notify_fn(text)
            logger.info("Notificación de error enviada (notify_fn inyectado)")
            return
        from app.alerts.telegram import send_message
        from app.config import settings

        ok = send_message(
            text,
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        if ok:
            logger.info("Notificación de error del ciclo enviada a Telegram")
        else:
            logger.warning("Telegram rechazó la notificación de error del ciclo")
    except Exception as e:  # noqa: BLE001
        logger.error("No se pudo notificar el error del ciclo por Telegram: %s", e)
