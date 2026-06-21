"""
Utilidades compartidas entre todas las fuentes de ingesta crypto.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Union

import httpx

_RETRY_DELAYS = [2, 5, 15]
_UPSERT_BATCH = 500


def fetch_json(
    url: str,
    params: dict = None,
    headers: dict = None,
    timeout: float = 15.0,
    logger_: logging.Logger = None,
    retry_delays: list = None,
) -> Optional[Union[dict, list]]:
    """
    GET con reintentos y backoff exponencial.
    Devuelve el JSON parseado o None si todos los intentos fallan.
    """
    delays = retry_delays or _RETRY_DELAYS
    for attempt, delay in enumerate(delays, 1):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if logger_:
                logger_.error("fetch_json %s intento %d/%d: %s", url, attempt, len(delays), e)
            if attempt < len(delays):
                time.sleep(delay)
    return None


def upsert_records(db, records: list[dict], logger_: logging.Logger = None) -> tuple[int, list[str]]:
    """
    Upsert por lotes en raw_snapshots.
    Devuelve (registros_insertados, lista_de_errores).
    """
    inserted = 0
    errors: list[str] = []
    for i in range(0, len(records), _UPSERT_BATCH):
        batch = records[i : i + _UPSERT_BATCH]
        try:
            db.table("raw_snapshots").upsert(batch, on_conflict="asset_id,ts").execute()
            inserted += len(batch)
        except Exception as e:
            msg = f"upsert lote {i // _UPSERT_BATCH}: {e}"
            if logger_:
                logger_.error(msg)
            errors.append(msg)
    return inserted, errors


def load_asset_map(db, ingest_source: str, logger_: logging.Logger = None) -> dict[str, int]:
    """
    Devuelve {ticker: asset_id} para todos los assets ACTIVOS de una fuente.
    Punto de consulta único: cada módulo de ingesta llama a esta función.
    Filtra por is_active=True para excluir assets desactivados por el recálculo.
    """
    try:
        resp = (
            db.table("assets")
            .select("id,ticker")
            .eq("is_active", True)
            .eq("ingest_source", ingest_source)
            .execute()
        )
        return {row["ticker"]: row["id"] for row in (resp.data or [])}
    except Exception as e:
        if logger_:
            logger_.error("Error cargando asset map (%s): %s", ingest_source, e)
        return {}


def day_ts() -> str:
    """
    Timestamp UTC de medianoche del día actual.
    Alinea los snapshots crypto con los bars diarios de yfinance.
    """
    return (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
