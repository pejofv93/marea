"""
Interfaz base para estrategias de flow score.

Cada asset_class implementa una Strategy que:
  - Recibe las filas de raw_snapshots del asset (lista de dicts).
  - Devuelve un ScoreResult para cada ventana temporal.
"""

from dataclasses import dataclass
from typing import Optional, Protocol

import pandas as pd

from app.scoring.zscore import ZResult, rolling_zscore, series_from_snapshots, WINDOW_SHORT, WINDOW_LONG, MIN_OBS_DEFAULT


@dataclass
class ScoreResult:
    score: Optional[float]       # clipeado [-1, +1]
    raw_zscore: Optional[float]  # sin clipear
    proxy_used: str              # descripción del proxy
    n_obs: int
    confidence: str              # 'ok' | 'low'


class Strategy(Protocol):
    """
    Protocolo (duck-typing) para estrategias de scoring.
    Cada asset_class implementa compute().
    """
    proxy_name: str

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        ...


def _make_result(zr: ZResult, proxy_used: str) -> ScoreResult:
    """Convierte un ZResult en ScoreResult."""
    return ScoreResult(
        score=zr.score,
        raw_zscore=zr.zscore,
        proxy_used=proxy_used,
        n_obs=zr.n_obs,
        confidence=zr.confidence,
    )


def _low_result(proxy_used: str, reason: str = "sin_datos") -> ScoreResult:
    """ScoreResult vacío para assets sin datos suficientes."""
    return ScoreResult(
        score=None,
        raw_zscore=None,
        proxy_used=f"{proxy_used}:{reason}",
        n_obs=0,
        confidence="low",
    )
