"""
Orquestador de la capa narrativa MAREA (Sesión 7).

Flujo:
  1. build_snapshot(db)        → snapshot compacto del estado actual
  2. generate_narrative(snap)  → texto LLM (Groq sin web search)
  3. upsert en `narratives`    → idempotente: mismo ts actualiza en vez de duplicar

Si Groq falla, la excepción se registra y no se persiste nada (igual que S6).
El snapshot_json se guarda siempre junto con la narrativa para auditoría.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.narrative.generator import DISCLAIMER, generate_narrative
from app.narrative.snapshot import build_snapshot

logger = logging.getLogger("marea.narrative.engine")


@dataclass
class NarrativeResult:
    text: str = ""
    regime: str = "neutral"
    confidence: float = 0.0
    disclaimer: str = DISCLAIMER
    ts: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "regime": self.regime,
            "confidence": self.confidence,
            "disclaimer": self.disclaimer,
            "ts": self.ts,
            "errors": self.errors,
            "ok": len(self.errors) == 0,
        }


class NarrativeEngine:
    def __init__(self, db=None, generate_fn=None):
        self._db = db
        self._generate_fn = generate_fn   # inyectable en tests para aislar Groq

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def _generate(self, snapshot: dict) -> str:
        if self._generate_fn is not None:
            return self._generate_fn(snapshot)
        return generate_narrative(snapshot)

    def run_sync(self) -> dict:
        from app.ingest._base import day_ts

        result = NarrativeResult()
        ts = day_ts()
        result.ts = ts

        try:
            snapshot = build_snapshot(self.db)

            regime_info = snapshot.get("regime") or {}
            result.regime = regime_info.get("name", "neutral")
            result.confidence = float(regime_info.get("confidence", 0.0))

            # Si Groq falla aquí, la excepción sale al except y no se persiste nada
            text = self._generate(snapshot)
            result.text = text

            self._upsert(ts, result, snapshot)

        except Exception as e:
            logger.error("Error en NarrativeEngine: %s", e)
            result.errors.append(str(e))

        logger.info(
            "Narrativa: régimen=%s conf=%.2f chars=%d errores=%d",
            result.regime,
            result.confidence,
            len(result.text),
            len(result.errors),
        )
        return result.to_dict()

    def _upsert(self, ts: str, result: NarrativeResult, snapshot: dict) -> None:
        row = {
            "ts": ts,
            "regime_at_ts": result.regime,
            "confidence": result.confidence,
            "text": result.text,
            "snapshot_json": snapshot,
            "llm_engine": "groq",
        }
        try:
            self.db.table("narratives").upsert(row, on_conflict="ts").execute()
        except Exception as e:
            logger.error("Error persistiendo narrativa: %s", e)
            result.errors.append(f"upsert narrativa: {e}")
