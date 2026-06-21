"""
Ciclo INTRADÍA de MAREA — punto de entrada para GitHub Actions.

Ejecuta, EN ORDEN, la cadena intradía reutilizando los engines existentes
(sin pasar por HTTP):

  1. ingesta intradía .......... IntradayRunner       (yfinance barras + crypto re-lectura + FNG)
  2. scores intradía ........... IntradayScoreEngine  (flow_scores_intraday)
  3. análisis intradía ......... IntradayAnalysisEngine (movimientos en curso)
  4. evaluación de alertas ..... AlertEngine          (→ Telegram lo que proceda)

El motor de alertas respeta TODO lo ya construido: anti-duplicado por cambio
de estado, histéresis y el umbral de confianza mínimo (MIN_ALERT_CONFIDENCE).
En cold start / baja confianza es NORMAL que no se envíe nada.

Uso:
    python -m scripts.run_intraday_cycle
    python scripts/run_intraday_cycle.py     # también funciona (bootstrap de path abajo)
"""

from __future__ import annotations

import logging
import os
import sys

# Permite ejecutar el archivo directamente (`python scripts/run_intraday_cycle.py`)
# además de como módulo (`python -m scripts.run_intraday_cycle`): asegura que la
# raíz del repo esté en sys.path para poder importar `app` y `scripts`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._common import run_cycle  # noqa: E402

logger = logging.getLogger("marea.cycle.intraday")


def build_steps(db=None):
    """
    Construye la cadena de pasos del ciclo intradía.

    Cada paso es ``(nombre, callable)`` donde el callable instancia el engine
    correspondiente y llama a su ``run_sync()`` (que ya captura errores por
    fuente/paso y devuelve un dict con ``ok``/``errors``).

    ``db`` se puede inyectar en tests; en producción cada engine abre su propia
    conexión a Supabase si es None.
    """
    from app.analysis.intraday import IntradayAnalysisEngine
    from app.alerts.engine import AlertEngine
    from app.config import settings
    from app.ingest.intraday_runner import IntradayRunner
    from app.scoring.intraday_engine import IntradayScoreEngine

    # Estado compartido entre pasos: el análisis intradía guarda aquí su
    # resultado (movimientos en curso) para que el resumen final lo use sin
    # recalcularlo.
    shared: dict = {}

    def _analisis():
        res = IntradayAnalysisEngine(db=db).run_sync()
        shared["intraday_analysis"] = res
        return res

    def _resumen():
        # Paso final: resumen-señal de vida en Telegram (SIEMPRE, haya o no alertas).
        from app.alerts.digest import send_intraday_digest
        return send_intraday_digest(db=db, analysis=shared.get("intraday_analysis"))

    return [
        ("ingesta_intradia",  lambda: IntradayRunner(db=db).run_sync()),
        ("scores_intradia",   lambda: IntradayScoreEngine(db=db, min_obs=settings.score_min_obs).run_sync()),
        ("analisis_intradia", _analisis),
        ("alertas",           lambda: AlertEngine(db=db).run_sync()),
        ("resumen_telegram",  _resumen),
    ]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,   # GitHub Actions captura stdout
    )
    return run_cycle("INTRADÍA", build_steps())


if __name__ == "__main__":
    sys.exit(main())
