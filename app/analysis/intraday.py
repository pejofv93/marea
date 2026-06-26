"""
Detección de movimientos de liquidez intradía en curso (MAREA S9b).

Compara el flow score intradía reciente (win='4h') de cada asset
contra la lectura anterior del mismo asset, identificando:
  - inflow fuerte en curso  (score > threshold)
  - outflow fuerte en curso (score < -threshold)

Esta señal es INDEPENDIENTE del régimen diario (que sigue inalterado).
Está pensada para alertas en vivo, no para sustituir al análisis de fondo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("marea.analysis.intraday")


@dataclass
class IntradayMovement:
    ticker:     str
    asset_class: str
    direction:  str             # 'inflow' | 'outflow' | 'neutral'
    score:      float
    score_prev: Optional[float]
    delta:      float           # score actual − score previo (0 si sin previo)
    confidence: str             # 'ok' | 'low'
    interval:   str
    ts:         str
    credibility_label:  Optional[str] = None   # confirmado/dudoso/fogonazo (Bloque 2)
    credibility_reason: Optional[str] = None


@dataclass
class IntradayAnalysisResult:
    movements:     list[IntradayMovement] = field(default_factory=list)
    strong_inflow:  list[str] = field(default_factory=list)
    strong_outflow: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "movements": [
                {
                    "ticker":      m.ticker,
                    "asset_class": m.asset_class,
                    "direction":   m.direction,
                    "score":       m.score,
                    "score_prev":  m.score_prev,
                    "delta":       m.delta,
                    "confidence":  m.confidence,
                    "interval":    m.interval,
                    "ts":          m.ts,
                    "credibility_label":  m.credibility_label,
                    "credibility_reason": m.credibility_reason,
                }
                for m in self.movements
            ],
            "strong_inflow":  self.strong_inflow,
            "strong_outflow": self.strong_outflow,
            "summary":        _build_summary(self.strong_inflow, self.strong_outflow),
            "errors":         self.errors,
            "ok":             len(self.errors) == 0,
        }


class IntradayAnalysisEngine:
    """
    Detecta movimientos de liquidez intradía comparando las 2 últimas
    lecturas de flow_scores_intraday (win='4h') por asset.
    """

    def __init__(
        self,
        db=None,
        interval: str | None = None,
        threshold: float | None = None,
    ):
        self._db = db
        self._interval = interval
        self._threshold = threshold

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    @property
    def interval(self) -> str:
        if self._interval:
            return self._interval
        from app.config import settings
        return settings.intraday_interval

    @property
    def threshold(self) -> float:
        if self._threshold is not None:
            return self._threshold
        from app.config import settings
        return settings.intraday_flow_threshold

    def run_sync(self) -> dict:
        result = IntradayAnalysisResult()
        try:
            rows = self._load_recent_scores()
            if not rows:
                logger.warning("Sin scores intradía disponibles para análisis")
                return result.to_dict()

            by_asset = _group_by_asset(rows)

            for asset_id, entries in by_asset.items():
                try:
                    movement = _build_movement(entries, self.threshold)
                    if movement is None:
                        continue
                    result.movements.append(movement)
                    if movement.direction == "inflow" and movement.confidence == "ok":
                        result.strong_inflow.append(movement.ticker)
                    elif movement.direction == "outflow" and movement.confidence == "ok":
                        result.strong_outflow.append(movement.ticker)
                except Exception as e:
                    msg = f"asset {asset_id}: {e}"
                    logger.error("Error analizando asset intradía %s: %s", asset_id, e)
                    result.errors.append(msg)

        except Exception as e:
            logger.exception("Error inesperado en IntradayAnalysisEngine")
            result.errors.append(str(e))

        logger.info(
            "Intradía análisis: %d movimientos, %d inflow, %d outflow",
            len(result.movements), len(result.strong_inflow), len(result.strong_outflow),
        )
        return result.to_dict()

    def _load_recent_scores(self) -> list[dict]:
        """Carga las 2 últimas lecturas 4h por asset (para detectar cambio de dirección)."""
        try:
            resp = (
                self.db.table("flow_scores_intraday")
                .select(
                    "asset_id,ts,interval,win,score,confidence,"
                    "credibility_label,credibility_reason,"
                    "assets(ticker,asset_class,sector)"
                )
                .eq("win", "4h")
                .eq("interval", self.interval)
                .order("ts", desc=True)
                .limit(500)
                .execute()
            )
            return resp.data or []
        except Exception as e:
            logger.error("Error cargando scores intradía para análisis: %s", e)
            return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _group_by_asset(rows: list[dict]) -> dict[int, list[dict]]:
    """Agrupa las filas por asset_id, conservando las 2 más recientes por asset."""
    by_asset: dict[int, list[dict]] = {}
    for row in rows:
        aid = row.get("asset_id")
        if aid is None:
            continue
        bucket = by_asset.setdefault(aid, [])
        if len(bucket) < 2:
            bucket.append(row)
    return by_asset


def _build_movement(entries: list[dict], threshold: float) -> Optional[IntradayMovement]:
    if not entries:
        return None

    latest      = entries[0]
    score       = float(latest.get("score") or 0.0)
    conf        = latest.get("confidence") or "low"
    ts          = latest.get("ts") or ""
    iv          = latest.get("interval") or "60m"
    assets_info = latest.get("assets") or {}
    ticker      = assets_info.get("ticker") or str(latest.get("asset_id", "?"))
    a_class     = assets_info.get("asset_class") or ""

    score_prev: Optional[float] = None
    if len(entries) > 1:
        score_prev = float(entries[1].get("score") or 0.0)

    delta = round((score - score_prev) if score_prev is not None else 0.0, 4)

    if abs(score) < threshold:
        direction = "neutral"
    elif score > 0:
        direction = "inflow"
    else:
        direction = "outflow"

    return IntradayMovement(
        ticker=ticker,
        asset_class=a_class,
        direction=direction,
        score=score,
        score_prev=score_prev,
        delta=delta,
        confidence=conf,
        interval=iv,
        ts=ts,
        credibility_label=latest.get("credibility_label"),
        credibility_reason=latest.get("credibility_reason"),
    )


def _build_summary(inflow: list[str], outflow: list[str]) -> str:
    parts = []
    if inflow:
        parts.append(f"Inflow: {', '.join(inflow)}")
    if outflow:
        parts.append(f"Outflow: {', '.join(outflow)}")
    return " | ".join(parts) if parts else "Sin movimientos intradía fuertes."
