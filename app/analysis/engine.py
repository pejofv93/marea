"""
Orquestador del análisis intermercado (Sesión 5).

Flujo:
  1. Cargar flow_scores (últimos 40 días, window='7d') con join a assets.
  2. Construir Matriz A (intermarket) y Matriz B (sector) → upsert en correlations.
  3. Obtener scores actuales por clase → clasificar régimen → upsert en regimes.
  4. Detectar rotación sectorial → upsert en rotations.
  5. Devolver resumen en dict.

Todas las escrituras son upsert idempotentes (on_conflict).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timezone

from app.analysis.correlation import CorrelationBuilder, aggregate_to_class_scores, filter_to_sector_scores
from app.analysis.regime import RegimeResult, ClassScores, classify_regime, class_scores_from_df_row, compute_data_confidence_factor
from app.analysis.sector import (
    SectorAnalyzer,
    RotationEvent,
    detect_sector_rotations,
    rotation_events_to_rows,
)

logger = logging.getLogger("marea.analysis.engine")

_UPSERT_BATCH = 200


@dataclass
class AnalysisResult:
    regime: str = "neutral"
    regime_confidence: float = 0.0
    regime_signals: list[str] = field(default_factory=list)
    n_decouplings_intermarket: int = 0
    n_decouplings_sector: int = 0
    rotations: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "regime_signals": self.regime_signals,
            "n_decouplings_intermarket": self.n_decouplings_intermarket,
            "n_decouplings_sector": self.n_decouplings_sector,
            "rotations": self.rotations,
            "errors": self.errors,
            "ok": len(self.errors) == 0,
        }


class AnalysisEngine:
    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def run_sync(self) -> dict:
        from app.ingest._base import day_ts

        result = AnalysisResult()
        ts = day_ts()   # midnight UTC hoy como string ISO

        try:
            builder = CorrelationBuilder(db=self.db)
            records = builder.load_scores()

            if not records:
                logger.warning("Sin flow_scores disponibles para el análisis")
                result.errors.append("Sin datos en flow_scores")
                return result.to_dict()

            # ── Paso 1: DataFrames pivotados ──────────────────────────────────
            class_df = aggregate_to_class_scores(records)
            sector_df = filter_to_sector_scores(records)

            # ── Paso 2: Correlaciones (Matriz A + Matriz B) ───────────────────
            intermarket_rows, sector_rows = builder.build(ts)

            self._upsert_correlations(intermarket_rows + sector_rows, result)

            result.n_decouplings_intermarket = sum(
                1 for r in intermarket_rows if r.get("is_decoupling") and r["win"] == "7d"
            )
            result.n_decouplings_sector = sum(
                1 for r in sector_rows if r.get("is_decoupling") and r["win"] == "7d"
            )

            # ── Paso 3: Régimen de mercado ────────────────────────────────────
            # Computar para ambas ventanas (7d scores ya cargados; 30d: carga separada)
            data_factor = compute_data_confidence_factor(records)
            # Moduladores de contexto (Bloque 1). Best-effort: si falla la lectura,
            # devuelve {} y el régimen se calcula igual que antes (degradación elegante).
            context_mods = self._load_context_modulators()
            regime_rows = self._compute_regimes(
                class_df, sector_df, ts, result, data_factor, context_mods
            )
            self._upsert_regimes(regime_rows, result)

            # Tomar régimen 7d como resultado principal
            regime_7d = next((r for r in regime_rows if r["win"] == "7d"), None)
            if regime_7d:
                result.regime = regime_7d["regime"]
                result.regime_confidence = regime_7d["confidence"]
                result.regime_signals = regime_7d["signals"]

            # ── Paso 4: Rotación sectorial ────────────────────────────────────
            sector_scores = SectorAnalyzer().get_sector_scores_from_df(sector_df)
            from datetime import datetime
            rotation_events = detect_sector_rotations(
                sector_scores,
                ts=datetime.now(timezone.utc),
            )
            rotation_rows = rotation_events_to_rows(rotation_events, ts)
            self._upsert_rotations(rotation_rows, result)
            result.rotations = rotation_rows

        except Exception as e:
            logger.exception("Error inesperado en AnalysisEngine")
            result.errors.append(str(e))

        logger.info(
            "Análisis: régimen=%s (conf=%.2f), desacoples inter=%d sector=%d, rotaciones=%d",
            result.regime,
            result.regime_confidence,
            result.n_decouplings_intermarket,
            result.n_decouplings_sector,
            len(result.rotations),
        )
        return result.to_dict()

    # ── Cálculo de regímenes ──────────────────────────────────────────────────

    def _load_context_modulators(self) -> dict[str, list[str]]:
        """Lee y evalúa los indicadores de contexto. Nunca lanza → {} si falla."""
        try:
            from app.analysis.context import evaluate_context
            return evaluate_context(self.db).regime_modulators
        except Exception as e:  # noqa: BLE001
            logger.warning("No se pudieron cargar moduladores de contexto: %s", e)
            return {}

    def _compute_regimes(
        self,
        class_df,
        sector_df,
        ts: str,
        result: AnalysisResult,
        data_factor: float = 1.0,
        context_mods: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        """Clasifica el régimen con scores de window='7d' y window='30d'."""
        regime_rows: list[dict] = []

        # Scores window='7d' (ya en class_df) — usar últimas observaciones
        if not class_df.empty:
            sector_scores_7d = SectorAnalyzer().get_sector_scores_from_df(sector_df)
            from datetime import datetime
            rotations_7d = detect_sector_rotations(sector_scores_7d, datetime.now(timezone.utc))
            has_rot = len(rotations_7d) > 0
            rot_conf = max((e.strength for e in rotations_7d), default=0.0) if rotations_7d else 0.0

            scores_7d = class_scores_from_df_row(class_df.iloc[-1])
            r7 = classify_regime(
                scores_7d,
                has_sector_rotation=has_rot,
                rotation_confidence=rot_conf,
                data_confidence_factor=data_factor,
                context_modulators=context_mods,
            )
            regime_rows.append(_regime_to_row(r7, ts, "7d"))

        # Scores window='30d' — carga separada (para régimen de tendencia larga)
        try:
            records_30d = CorrelationBuilder(db=self.db).load_scores()
            # Reutiliza los mismos records; la diferencia es que el usuario puede
            # cargar window='30d' explícitamente. Aquí usamos la misma lógica
            # pero con los scores del DataFrame ya cargado con ventana='7d'.
            # Nota: en una iteración futura podríamos cargar también window='30d'.
            # Por ahora el régimen '30d' usa la penúltima semana de datos si hay.
            class_df_30d = class_df  # mismo df, la ventana semántica es la de scoring
            if not class_df_30d.empty and len(class_df_30d) >= 7:
                scores_30d = class_scores_from_df_row(class_df_30d.iloc[-7])  # hace 7 días
                data_factor_30d = compute_data_confidence_factor(records_30d)
                r30 = classify_regime(scores_30d, data_confidence_factor=data_factor_30d)
                regime_rows.append(_regime_to_row(r30, ts, "30d"))
        except Exception as e:
            logger.warning("No se pudo computar régimen 30d: %s", e)
            result.errors.append(f"régimen 30d: {e}")

        return regime_rows

    # ── Upserts ───────────────────────────────────────────────────────────────

    def _upsert_correlations(self, rows: list[dict], result: AnalysisResult) -> None:
        self._upsert_table("correlations", rows, "ts,win,matrix_type,pair_a,pair_b", result)

    def _upsert_regimes(self, rows: list[dict], result: AnalysisResult) -> None:
        # signals es list[str] → serializar a JSON-compatible
        serialized = [
            {**r, "signals": r.get("signals", [])}
            for r in rows
        ]
        self._upsert_table("regimes", serialized, "ts,win", result)

    def _upsert_rotations(self, rows: list[dict], result: AnalysisResult) -> None:
        self._upsert_table("rotations", rows, "ts,from_sector,to_sector", result)

    def _upsert_table(
        self,
        table: str,
        rows: list[dict],
        conflict_cols: str,
        result: AnalysisResult,
    ) -> None:
        if not rows:
            return
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch = rows[i: i + _UPSERT_BATCH]
            try:
                self.db.table(table).upsert(batch, on_conflict=conflict_cols).execute()
            except Exception as e:
                msg = f"upsert {table} lote {i // _UPSERT_BATCH}: {e}"
                logger.error(msg)
                result.errors.append(msg)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _regime_to_row(r: RegimeResult, ts: str, window: str) -> dict:
    return {
        "ts": ts,
        "win": window,
        "regime": r.regime,
        "confidence": r.confidence,
        "signals": r.signals,
        "structural_confidence": r.structural_confidence,
        "data_confidence_factor": r.data_confidence_factor,
    }
