"""
Detección de rotación sectorial entre ETFs.

Definición de rotación: un ETF sectorial tiene score claramente negativo
(salida de liquidez) mientras otro tiene score claramente positivo (entrada)
de forma simultánea. Cada par (from, to) constituye un evento de rotación.

La fuerza (strength) del evento = min(|score_salida|, |score_entrada|),
normalizada al rango [0, 1] dado que los scores ya están en [-1, +1].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from app.analysis.correlation import SECTOR_ETFS

logger = logging.getLogger("marea.analysis.sector")

# ── Umbral para considerar una señal sectorial clara ─────────────────────────
ROTATION_THRESHOLD: float = 0.25


@dataclass
class RotationEvent:
    from_sector: str    # ETF con outflow claro (score muy negativo)
    to_sector: str      # ETF con inflow claro (score muy positivo)
    strength: float     # min(|out_score|, |in_score|) ∈ [0, 1]


def detect_sector_rotations(
    sector_scores: dict[str, float],
    ts: datetime,
) -> list[RotationEvent]:
    """
    Detecta eventos de rotación sectorial a partir de los scores actuales.

    Señal: ETF con score < -THRESHOLD (outflow) → ETF con score > THRESHOLD (inflow).
    Se generan todos los pares (from, to) donde se cumple la condición simultáneamente.

    `ts` es informativo (se usa para logging y para construir la fila de DB).
    """
    T = ROTATION_THRESHOLD
    outflows = [(t, s) for t, s in sector_scores.items() if s < -T and t in SECTOR_ETFS]
    inflows = [(t, s) for t, s in sector_scores.items() if s > T and t in SECTOR_ETFS]

    if not outflows or not inflows:
        return []

    events: list[RotationEvent] = []
    for out_ticker, out_score in outflows:
        for in_ticker, in_score in inflows:
            strength = round(min(abs(out_score), abs(in_score)), 4)
            events.append(RotationEvent(
                from_sector=out_ticker,
                to_sector=in_ticker,
                strength=strength,
            ))
            logger.debug(
                "Rotación sectorial %s → %s (strength=%.3f) en %s",
                out_ticker, in_ticker, strength, ts.date() if hasattr(ts, "date") else ts,
            )

    return events


def rotation_events_to_rows(events: list[RotationEvent], ts: str) -> list[dict]:
    """Convierte eventos de rotación a filas listas para upsert en tabla `rotations`."""
    return [
        {
            "ts": ts,
            "from_sector": e.from_sector,
            "to_sector": e.to_sector,
            "strength": e.strength,
        }
        for e in events
    ]


class SectorAnalyzer:
    """Carga los scores sectoriales más recientes y detecta rotaciones."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def latest_sector_scores(self) -> dict[str, float]:
        """
        Devuelve los scores más recientes (window='7d') de los ETFs sectoriales.
        Consulta flow_scores filtrando por ticker en SECTOR_ETFS.
        """
        try:
            resp = (
                self.db.table("flow_scores")
                .select("score,assets(ticker)")
                .eq("win", "7d")
                .order("ts", desc=True)
                .limit(len(SECTOR_ETFS) * 2)  # margen por si hay gaps
                .execute()
            )
            scores: dict[str, float] = {}
            for r in resp.data or []:
                assets = r.get("assets") or {}
                ticker = assets.get("ticker", "")
                if ticker in SECTOR_ETFS and ticker not in scores:
                    score = r.get("score")
                    if score is not None:
                        scores[ticker] = float(score)
            return scores
        except Exception as e:
            logger.error("Error cargando scores sectoriales: %s", e)
            return {}

    def get_sector_scores_from_df(self, sector_df: pd.DataFrame) -> dict[str, float]:
        """
        Extrae los scores más recientes del DataFrame pivotado de ETFs sectoriales.
        Alternativa a consultar la BD directamente (usa datos ya cargados por el engine).
        """
        if sector_df.empty:
            return {}
        latest = sector_df.iloc[-1]
        return {
            col: float(val)
            for col, val in latest.items()
            if col in SECTOR_ETFS and val is not None and val == val  # NaN check
        }
