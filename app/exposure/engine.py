"""
Orquestador del mapa de exposición indirecta (Sesión 6).

Flujo por cada objetivo:
  1. DiscoveryService → candidatos crudos del LLM (con búsqueda web).
  2. verify_candidate → filtra y clasifica (sin URL → descartado).
  3. Upsert idempotente en tabla `exposures`.

El resultado resume: cuántos candidatos crudos, cuántos descartados por falta
de URL, y cuántos persistidos por nivel de confianza.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.exposure.discovery import (
    DEFAULT_CRYPTO_TARGETS,
    DEFAULT_PRE_IPO_TARGETS,
    DiscoveryService,
    RawCandidate,
)
from app.exposure.verify import VerifiedCandidate, verify_candidate

logger = logging.getLogger("marea.exposure.engine")

_UPSERT_BATCH = 50


@dataclass
class DiscoveryResult:
    raw_count: int = 0
    discarded_no_url: int = 0
    persisted_by_confidence: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw_candidates": self.raw_count,
            "discarded_no_url": self.discarded_no_url,
            "persisted_by_confidence": self.persisted_by_confidence,
            "total_persisted": sum(self.persisted_by_confidence.values()),
            "errors": self.errors,
            "ok": len(self.errors) == 0,
        }


class ExposureEngine:
    def __init__(self, db=None, llm_fn=None):
        self._db = db
        self._llm_fn = llm_fn

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def run_sync(
        self,
        pre_ipo_targets: list[str] | None = None,
        crypto_targets: list[str] | None = None,
    ) -> dict:
        from app.ingest._base import day_ts

        result = DiscoveryResult()
        ts = day_ts()

        if crypto_targets is None:
            crypto_targets = self._get_active_crypto_tickers() or DEFAULT_CRYPTO_TARGETS
        if pre_ipo_targets is None:
            pre_ipo_targets = DEFAULT_PRE_IPO_TARGETS

        svc = DiscoveryService(llm_fn=self._llm_fn)

        for entity in pre_ipo_targets:
            self._process_batch(svc.discover_pre_ipo(entity), result, ts)

        for crypto in crypto_targets:
            self._process_batch(svc.discover_crypto(crypto), result, ts)

        logger.info(
            "Exposición: %d crudos, %d descartados sin URL, %d persistidos: %s",
            result.raw_count,
            result.discarded_no_url,
            sum(result.persisted_by_confidence.values()),
            result.persisted_by_confidence,
        )
        return result.to_dict()

    # ── Procesado de un lote de candidatos ────────────────────────────────────

    def _process_batch(
        self,
        raw_candidates: list[RawCandidate],
        result: DiscoveryResult,
        ts: str,
    ) -> None:
        result.raw_count += len(raw_candidates)
        rows_to_upsert: list[dict] = []

        for raw in raw_candidates:
            try:
                verified = verify_candidate(
                    source_entity=raw.source_entity,
                    exposed_ticker=raw.exposed_ticker,
                    exposure_type=raw.exposure_type,
                    relationship=raw.relationship,
                    sources=raw.sources,
                    llm_engine=raw.llm_engine,
                )
                if verified is None:
                    result.discarded_no_url += 1
                    continue

                rows_to_upsert.append(_to_row(verified, ts))
                conf = verified.confidence
                result.persisted_by_confidence[conf] = (
                    result.persisted_by_confidence.get(conf, 0) + 1
                )

            except Exception as e:
                msg = f"{raw.source_entity}→{raw.exposed_ticker}: {e}"
                logger.error(msg)
                result.errors.append(msg)

        self._upsert(rows_to_upsert, result)

    # ── DB ────────────────────────────────────────────────────────────────────

    def _upsert(self, rows: list[dict], result: DiscoveryResult) -> None:
        if not rows:
            return
        for i in range(0, len(rows), _UPSERT_BATCH):
            batch = rows[i: i + _UPSERT_BATCH]
            try:
                self.db.table("exposures").upsert(
                    batch,
                    on_conflict="source_entity,exposed_ticker,exposure_type",
                ).execute()
            except Exception as e:
                msg = f"upsert exposures lote {i // _UPSERT_BATCH}: {e}"
                logger.error(msg)
                result.errors.append(msg)

    def _get_active_crypto_tickers(self) -> list[str]:
        """Obtiene tickers crypto activos del universo dinámico (S3)."""
        try:
            resp = (
                self.db.table("assets")
                .select("ticker")
                .eq("asset_class", "crypto")
                .eq("is_active", True)
                .execute()
            )
            tickers = [
                r["ticker"] for r in (resp.data or [])
                if r.get("ticker")
            ]
            # Excluye tickers sintéticos (perpetuos, stablecoins)
            return [t for t in tickers if "_PERP" not in t and "STABLES" not in t]
        except Exception as e:
            logger.warning("No se pudo cargar crypto activos de BD: %s", e)
            return []


# ── Helper ────────────────────────────────────────────────────────────────────

def _to_row(v: VerifiedCandidate, ts: str) -> dict:
    """Convierte VerifiedCandidate a dict para upsert en Supabase (JSONB como list)."""
    return {
        "source_entity":    v.source_entity,
        "exposed_ticker":   v.exposed_ticker,
        "exposure_type":    v.exposure_type,
        "relationship":     v.relationship,
        "confidence":       v.confidence,
        "sources":          v.sources,    # list → Supabase lo serializa a JSONB
        "llm_engine":       v.llm_engine,
        "discovered_at":    ts,
        "last_verified_at": ts,
    }
