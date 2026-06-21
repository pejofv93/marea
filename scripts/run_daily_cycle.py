"""
Ciclo DIARIO de MAREA — punto de entrada para GitHub Actions.

Ejecuta, EN ORDEN, la cadena diaria completa reutilizando los engines
existentes (sin pasar por HTTP):

  1. ingesta diaria ............ IngestAll          (yfinance + crypto + on-chain)
  2. recompute universo ........ UniverseRecomputer (top-N dinámico)
  3. scores diarios ............ ScoreEngine        (flow_scores)
  4. análisis diario ........... AnalysisEngine     (correlaciones + régimen + rotación)
  5. narrativa ................. NarrativeEngine    (Groq, sin web)
  6. evaluación de alertas ..... AlertEngine        (→ Telegram lo que proceda)

El motor de alertas respeta TODO lo ya construido: anti-duplicado por cambio
de estado, histéresis y el umbral de confianza mínimo (MIN_ALERT_CONFIDENCE).
En cold start / baja confianza es NORMAL que no se envíe nada.

Uso:
    python -m scripts.run_daily_cycle
    python scripts/run_daily_cycle.py     # también funciona (bootstrap de path abajo)
"""

from __future__ import annotations

import logging
import os
import sys

# Permite ejecutar el archivo directamente además de como módulo (ver intradía).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._common import run_cycle  # noqa: E402

logger = logging.getLogger("marea.cycle.daily")


def build_steps(db=None):
    """
    Construye la cadena de pasos del ciclo diario.

    Cada paso instancia el engine correspondiente y llama a su ``run_sync()``.
    ``db`` se puede inyectar en tests.
    """
    from app.alerts.engine import AlertEngine
    from app.analysis.engine import AnalysisEngine
    from app.config import settings
    from app.ingest.run_all import IngestAll
    from app.narrative.engine import NarrativeEngine
    from app.scoring.engine import ScoreEngine
    from app.universe.dynamic import UniverseRecomputer

    return [
        ("ingesta_diaria",   lambda: IngestAll(db=db).run_sync()),
        ("recompute_universo", lambda: UniverseRecomputer(db=db).run_sync()),
        ("scores_diarios",   lambda: ScoreEngine(db=db, min_obs=settings.score_min_obs).run_sync()),
        ("analisis_diario",  lambda: AnalysisEngine(db=db).run_sync()),
        ("narrativa",        lambda: NarrativeEngine(db=db).run_sync()),
        ("alertas",          lambda: AlertEngine(db=db).run_sync()),
    ]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,   # GitHub Actions captura stdout
    )
    return run_cycle("DIARIO", build_steps())


if __name__ == "__main__":
    sys.exit(main())
