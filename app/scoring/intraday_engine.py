"""
Motor de flow scores intradía MAREA.

Calcula flow_scores_intraday sobre raw_snapshots_intraday.
Dos ventanas:
  '4h'          → últimas N barras equivalentes a 4 horas
  '1d_intraday' → últimas N barras equivalentes a ~1 sesión US (8h con margen)

El número de barras depende del interval configurado:
  60m → 4h=4 barras, 1d=8 barras
  15m → 4h=16 barras, 1d=32 barras

Reutiliza las mismas estrategias y z-score del carril diario.
NO modifica el motor diario (ScoreEngine), ni toca raw_snapshots/flow_scores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.scoring.strategies import get_strategy
from app.scoring.zscore import MIN_OBS_DEFAULT

logger = logging.getLogger("marea.scoring.intraday_engine")

_LOOKBACK_BARS = 60     # barras a cargar por asset (buffer amplio para ambas ventanas)
_UPSERT_BATCH  = 200

# Número de horas por ventana nombrada
_WINDOW_HOURS: dict[str, int] = {
    "4h":          4,   # corto plazo: últimas horas de la sesión
    "1d_intraday": 8,   # sesión completa (~6.5h US, usamos 8h con margen)
}

# Barras por hora según interval
_BARS_PER_HOUR: dict[str, int] = {
    "60m": 1,
    "15m": 4,
}


def bars_for_window(win: str, interval: str) -> int:
    """Número de barras para la ventana intradía según el interval."""
    bph   = _BARS_PER_HOUR.get(interval, 1)
    hours = _WINDOW_HOURS.get(win, 4)
    return max(hours * bph, 2)   # mínimo 2 para que rolling_zscore opere


@dataclass
class IntradayEngineResult:
    scores_computed: int = 0
    low_confidence: int = 0
    errors: list[str] = field(default_factory=list)
    by_asset: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scores_computed": self.scores_computed,
            "low_confidence":  self.low_confidence,
            "errors":          self.errors,
            "by_asset":        self.by_asset,
            "ok":              len(self.errors) == 0,
        }


class IntradayScoreEngine:
    """
    Calcula flow_scores_intraday para todos los assets activos.
    Lee desde raw_snapshots_intraday; escribe en flow_scores_intraday.
    El carril diario (raw_snapshots, flow_scores) queda intacto.
    """

    def __init__(
        self,
        db=None,
        interval: str | None = None,
        min_obs: int = MIN_OBS_DEFAULT,
    ):
        self._db = db
        self._interval = interval
        self._min_obs = min_obs

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

    def run_sync(self) -> dict:
        result = IntradayEngineResult()
        try:
            assets = self._load_active_assets()
            if not assets:
                logger.warning("No hay assets activos para scores intradía")
                return result.to_dict()

            upsert_rows: list[dict] = []

            for asset in assets:
                asset_id = asset["id"]
                ticker   = asset["ticker"]
                a_class  = asset.get("asset_class", "")
                sector   = asset.get("sector")

                try:
                    rows     = self._load_snapshots(asset_id)
                    strategy = get_strategy(a_class, sector)
                    asset_scores: dict = {}

                    for win_name in _WINDOW_HOURS:
                        n_bars = bars_for_window(win_name, self.interval)
                        sr     = strategy.compute(rows, n_bars, self._min_obs)

                        if sr.confidence == "low":
                            result.low_confidence += 1

                        row = _build_row(asset_id, _latest_ts(rows), self.interval, win_name, sr)
                        if row is not None:
                            upsert_rows.append(row)
                            result.scores_computed += 1
                            asset_scores[win_name] = {
                                "score":      sr.score,
                                "confidence": sr.confidence,
                                "proxy":      sr.proxy_used,
                                "n_obs":      sr.n_obs,
                            }

                    if asset_scores:
                        result.by_asset[ticker] = asset_scores

                except Exception as e:
                    msg = f"{ticker}: {e}"
                    logger.error("Error score intradía %s: %s", ticker, e)
                    result.errors.append(msg)

            self._upsert_scores(upsert_rows, result)

        except Exception as e:
            logger.exception("Error inesperado en IntradayScoreEngine")
            result.errors.append(str(e))

        logger.info(
            "IntradayScoreEngine: %d scores, %d low-conf, %d errores",
            result.scores_computed, result.low_confidence, len(result.errors),
        )
        return result.to_dict()

    # ── BD ─────────────────────────────────────────────────────────────────────

    def _load_active_assets(self) -> list[dict]:
        try:
            resp = (
                self.db.table("assets")
                .select("id,ticker,asset_class,sector")
                .eq("is_active", True)
                .execute()
            )
            return resp.data or []
        except Exception as e:
            logger.error("Error cargando assets activos (intradía): %s", e)
            return []

    def _load_snapshots(self, asset_id: int) -> list[dict]:
        resp = (
            self.db.table("raw_snapshots_intraday")
            .select("ts,open,high,low,close,volume,extra")
            .eq("asset_id", asset_id)
            .eq("interval", self.interval)
            .order("ts", desc=True)
            .limit(_LOOKBACK_BARS)
            .execute()
        )
        rows = resp.data or []
        return list(reversed(rows))   # cronológico: más antiguo primero

    def _upsert_scores(self, rows: list[dict], result: IntradayEngineResult) -> None:
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch = rows[i : i + _UPSERT_BATCH]
            try:
                self.db.table("flow_scores_intraday").upsert(
                    batch, on_conflict="asset_id,ts,interval,win"
                ).execute()
            except Exception as e:
                msg = f"upsert_scores intradía lote {i // _UPSERT_BATCH}: {e}"
                logger.error(msg)
                result.errors.append(msg)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _latest_ts(rows: list[dict]) -> str:
    """Timestamp de la barra más reciente en la lista, o now() si la lista está vacía."""
    if rows:
        return rows[-1]["ts"]
    return datetime.now(timezone.utc).isoformat()


def _build_row(
    asset_id: int,
    ts: str,
    interval: str,
    win: str,
    sr,
) -> Optional[dict]:
    """Devuelve None si el score no pudo calcularse (cold start total: <2 obs)."""
    if sr.score is None:
        return None
    return {
        "asset_id":   asset_id,
        "ts":         ts,
        "interval":   interval,
        "win":        win,
        "score":      sr.score,
        "raw_zscore": sr.raw_zscore,
        "proxy_used": sr.proxy_used,
        "n_obs":      sr.n_obs,
        "confidence": sr.confidence,
    }
