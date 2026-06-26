"""
Motor de flow scores: para cada asset activo, carga su serie histórica
desde raw_snapshots, elige la estrategia correcta y hace upsert en flow_scores.

Ventanas: 7d y 30d (ambas calculadas por asset en cada ejecución).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.scoring.base import ScoreResult
from app.scoring.strategies import get_strategy
from app.scoring.zscore import WINDOW_LONG, WINDOW_SHORT, MIN_OBS_DEFAULT

logger = logging.getLogger("marea.scoring.engine")

# Cuántos días de histórico cargar por asset (buffer para ventana 30d + margen)
_LOOKBACK_DAYS = 90
# Lote máximo de upserts por llamada a Supabase
_UPSERT_BATCH = 200


@dataclass
class EngineResult:
    scores_computed: int = 0
    low_confidence: int = 0
    errors: list[str] = field(default_factory=list)
    by_asset: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scores_computed": self.scores_computed,
            "low_confidence": self.low_confidence,
            "errors": self.errors,
            "by_asset": self.by_asset,
            "ok": len(self.errors) == 0,
        }


class ScoreEngine:
    def __init__(self, db=None, min_obs: int = MIN_OBS_DEFAULT, persist_min_obs: int | None = None):
        self._db = db
        self._min_obs = min_obs
        self._persist_min_obs_override = persist_min_obs

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    @property
    def _persist_min_obs(self) -> int:
        if self._persist_min_obs_override is not None:
            return self._persist_min_obs_override
        from app.config import settings
        return settings.credibility_persist_min_obs

    def run_sync(self) -> dict:
        result = EngineResult()
        try:
            assets = self._load_active_assets()
            if not assets:
                logger.warning("No hay assets activos para calcular scores")
                return result.to_dict()

            upsert_rows: list[dict] = []

            for asset in assets:
                asset_id = asset["id"]
                ticker = asset["ticker"]
                asset_class = asset.get("asset_class", "")
                sector = asset.get("sector")

                try:
                    rows = self._load_snapshots(asset_id)
                    strategy = get_strategy(asset_class, sector)
                    asset_scores: dict[str, dict] = {}

                    applies_cred = getattr(strategy, "applies_credibility", False)

                    for window in (WINDOW_SHORT, WINDOW_LONG):
                        window_label = f"{window}d"
                        sr = strategy.compute(rows, window, self._min_obs)

                        if sr.confidence == "low":
                            result.low_confidence += 1

                        # Capa de credibilidad (Bloque 2): solo a estrategias de
                        # flujo con volumen+precio, y solo si hay flujo que juzgar.
                        cred = None
                        if applies_cred and sr.score is not None:
                            from app.scoring.credibility import assess_credibility
                            cred = assess_credibility(
                                rows, sr.score, window,
                                persist_min_obs=self._persist_min_obs,
                            )

                        row = _build_row(asset_id, window_label, sr, cred)
                        if row:
                            upsert_rows.append(row)
                            result.scores_computed += 1
                            asset_scores[window_label] = {
                                "score": row["score"],
                                "score_raw": row["score_raw"],
                                "credibility": row["credibility"],
                                "credibility_label": row["credibility_label"],
                                "confidence": sr.confidence,
                                "proxy": sr.proxy_used,
                            }

                    result.by_asset[ticker] = asset_scores

                except Exception as e:
                    msg = f"{ticker}: {e}"
                    logger.error("Error calculando score para %s: %s", ticker, e)
                    result.errors.append(msg)

            # Upsert en lotes
            self._upsert_scores(upsert_rows, result)

        except Exception as e:
            logger.exception("Error inesperado en ScoreEngine")
            result.errors.append(str(e))

        logger.info(
            "Scores: %d calculados, %d low-confidence, %d errores",
            result.scores_computed, result.low_confidence, len(result.errors),
        )
        return result.to_dict()

    # ── Consultas a la BD ──────────────────────────────────────────────────────

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
            logger.error("Error cargando assets activos: %s", e)
            return []

    def _load_snapshots(self, asset_id: int) -> list[dict]:
        """
        Carga las últimas _LOOKBACK_DAYS observaciones del asset.
        No captura excepciones: el bloque try/except por asset en run_sync
        las recoge y las añade a result.errors.
        """
        resp = (
            self.db.table("raw_snapshots")
            .select("ts,open,high,low,close,volume,extra")
            .eq("asset_id", asset_id)
            .order("ts", desc=True)
            .limit(_LOOKBACK_DAYS)
            .execute()
        )
        rows = resp.data or []
        return list(reversed(rows))   # cronológico: más antiguo primero

    def _upsert_scores(self, rows: list[dict], result: EngineResult) -> None:
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch = rows[i: i + _UPSERT_BATCH]
            try:
                self.db.table("flow_scores").upsert(
                    batch, on_conflict="asset_id,ts,win"
                ).execute()
            except Exception as e:
                msg = f"upsert lote {i // _UPSERT_BATCH}: {e}"
                logger.error(msg)
                result.errors.append(msg)


# ── Helper ─────────────────────────────────────────────────────────────────────

def _build_row(asset_id: int, window: str, sr: ScoreResult, cred=None) -> Optional[dict]:
    from app.ingest._base import day_ts
    from app.scoring.credibility import penalized_score

    return {
        "asset_id":   asset_id,
        "ts":         day_ts(),
        "win":        window,
        # 'score' YA penalizado (lo consumen rankings/régimen/alertas); 'score_raw'
        # conserva el bruto. Credibilidad ⟂ confidence (cold start): ejes distintos.
        "score":              penalized_score(sr.score, cred),
        "score_raw":          sr.score,
        "raw_zscore":         sr.raw_zscore,
        "proxy_used":         sr.proxy_used,
        "n_obs":              sr.n_obs,
        "confidence":         sr.confidence,
        "credibility":        cred.credibility if cred else None,
        "credibility_label":  cred.label if cred else None,
        "credibility_reason": cred.reason if cred else None,
    }
