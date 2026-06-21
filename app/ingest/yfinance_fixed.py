"""
Ingesta en lote del universo fijo desde yfinance.

REGLA ANTI-BANEO: yf.download se llama UNA sola vez con TODOS los tickers.
Nunca se itera ticker a ticker.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import yfinance as yf

from app.config import settings
from app.ingest._base import load_asset_map
from app.universe.fixed import FIXED_TICKERS

logger = logging.getLogger("marea.ingest.fixed")

_RETRY_DELAYS = [2, 5, 15]  # backoff en segundos entre reintentos
_UPSERT_BATCH = 500          # filas por llamada upsert para no saturar la API


@dataclass
class IngestResult:
    assets_queried: int = 0
    snapshots_inserted: int = 0
    tickers_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "assets_queried": self.assets_queried,
            "snapshots_inserted": self.snapshots_inserted,
            "tickers_missing": self.tickers_missing,
            "errors": self.errors,
            "ok": len(self.errors) == 0,
        }


class IngestFixedUniverse:
    """
    Descarga datos OHLCV del universo fijo en un solo lote yfinance
    y hace upsert en raw_snapshots.
    """

    def __init__(self, db=None, period: Optional[str] = None):
        self._db = db
        self._period = period or settings.ingest_period

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def run_sync(self) -> dict:
        result = IngestResult()
        try:
            asset_map = self._load_asset_map(result)
            if not asset_map:
                result.errors.append("No se encontraron assets fijos en la BD. ¿Corriste la migración?")
                return result.to_dict()

            result.assets_queried = len(asset_map)

            raw_df = self._download_batch(list(asset_map.keys()), result)
            if raw_df is None or raw_df.empty:
                result.errors.append("yfinance no devolvió datos")
                return result.to_dict()

            records = self._normalize(raw_df, asset_map, result)
            if records:
                self._upsert(records, result)

        except Exception as e:
            logger.exception("Error inesperado en ingesta")
            result.errors.append(str(e))

        logger.info(
            "Ingesta completada: %d assets, %d snapshots, %d errores",
            result.assets_queried, result.snapshots_inserted, len(result.errors),
        )
        return result.to_dict()

    # ──────────────────────────────────────────────────────────────────────────
    # Pasos internos
    # ──────────────────────────────────────────────────────────────────────────

    def _load_asset_map(self, result: IngestResult) -> dict[str, int]:
        """Devuelve {ticker: asset_id} para assets activos de yfinance en BD."""
        return load_asset_map(self.db, "yfinance", logger_=logger)

    def _download_batch(self, tickers: list[str], result: IngestResult) -> Optional[pd.DataFrame]:
        """
        UNA sola llamada yf.download con TODOS los tickers (anti-baneo).
        Reintenta con backoff exponencial ante fallos de red.
        """
        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            try:
                logger.info("Descargando %d tickers (intento %d)…", len(tickers), attempt)
                df = yf.download(
                    tickers=tickers,
                    period=self._period,
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=False,   # evita peticiones paralelas por ticker
                )
                if not df.empty:
                    return df
                logger.warning("DataFrame vacío (intento %d/%d)", attempt, len(_RETRY_DELAYS))
            except Exception as e:
                logger.error("Error descargando datos (intento %d/%d): %s", attempt, len(_RETRY_DELAYS), e)
                result.errors.append(f"download intento {attempt}: {e}")

            if attempt < len(_RETRY_DELAYS):
                time.sleep(delay)

        return None

    def _normalize(
        self,
        raw_df: pd.DataFrame,
        asset_map: dict[str, int],
        result: IngestResult,
    ) -> list[dict]:
        """
        Convierte el DataFrame MultiIndex de yfinance en registros para raw_snapshots.

        yfinance devuelve columnas (field, ticker) cuando se descargan varios tickers:
            ('Close', 'SPY'), ('Close', 'QQQ'), …
        Usamos xs(ticker, level=1) para extraer cada slice.
        """
        records: list[dict] = []

        for ticker, asset_id in asset_map.items():
            try:
                if isinstance(raw_df.columns, pd.MultiIndex):
                    ticker_df = raw_df.xs(ticker, axis=1, level=1)
                else:
                    # Único ticker en el lote (caso degenerado; no debería ocurrir)
                    ticker_df = raw_df
            except KeyError:
                logger.warning("Sin datos para %s", ticker)
                result.tickers_missing.append(ticker)
                continue

            ticker_df = ticker_df.dropna(subset=["Close"])
            if ticker_df.empty:
                result.tickers_missing.append(ticker)
                continue

            for ts, row in ticker_df.iterrows():
                records.append({
                    "asset_id": asset_id,
                    "ts": ts.isoformat(),
                    "open":   _safe_float(row.get("Open")),
                    "high":   _safe_float(row.get("High")),
                    "low":    _safe_float(row.get("Low")),
                    "close":  _safe_float(row.get("Close")),
                    "volume": _safe_float(row.get("Volume")),
                    "extra":  {},
                })

        logger.info("Normalizados %d registros de %d tickers", len(records), len(asset_map))
        return records

    def _upsert(self, records: list[dict], result: IngestResult) -> None:
        """Upsert en lotes para no saturar la API de Supabase."""
        for i in range(0, len(records), _UPSERT_BATCH):
            batch = records[i : i + _UPSERT_BATCH]
            try:
                self.db.table("raw_snapshots").upsert(
                    batch, on_conflict="asset_id,ts"
                ).execute()
                result.snapshots_inserted += len(batch)
            except Exception as e:
                logger.error("Error en upsert lote %d: %s", i // _UPSERT_BATCH, e)
                result.errors.append(f"upsert lote {i // _UPSERT_BATCH}: {e}")


def _safe_float(val) -> Optional[float]:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None
