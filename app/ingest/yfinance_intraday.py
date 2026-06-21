"""
Ingesta intradía en lote desde yfinance.

REGLA ANTI-BANEO: una sola llamada yf.download para TODOS los tickers,
con interval y period corto. Nunca ticker a ticker.

Escribe en raw_snapshots_intraday con timestamps REALES (no aplastados a
medianoche): el carril intradía mantiene la resolución temporal real de
cada barra.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import yfinance as yf

from app.ingest._base import load_asset_map

logger = logging.getLogger("marea.ingest.yfinance_intraday")

_RETRY_DELAYS = [2, 5, 15]
_UPSERT_BATCH = 500


@dataclass
class IntradayIngestResult:
    assets_queried: int = 0
    snapshots_inserted: int = 0
    tickers_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "assets_queried":     self.assets_queried,
            "snapshots_inserted": self.snapshots_inserted,
            "tickers_missing":    self.tickers_missing,
            "errors":             self.errors,
            "ok":                 len(self.errors) == 0,
        }


class IngestYFinanceIntraday:
    """
    Descarga barras OHLCV intradía del universo yfinance en un solo lote
    y hace upsert en raw_snapshots_intraday (no en raw_snapshots).
    """

    def __init__(self, db=None, interval: str = "60m", period: str = "5d"):
        self._db = db
        self.interval = interval
        self.period = period

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def run_sync(self) -> dict:
        result = IntradayIngestResult()
        try:
            asset_map = load_asset_map(self.db, "yfinance", logger_=logger)
            if not asset_map:
                result.errors.append("No hay assets yfinance activos en BD")
                return result.to_dict()

            result.assets_queried = len(asset_map)

            raw_df = self._download_batch(list(asset_map.keys()), result)
            if raw_df is None or raw_df.empty:
                result.errors.append("yfinance intradía no devolvió datos")
                return result.to_dict()

            records = self._normalize(raw_df, asset_map, result)
            if records:
                self._upsert(records, result)

        except Exception as e:
            logger.exception("Error inesperado en ingesta intradía yfinance")
            result.errors.append(str(e))

        logger.info(
            "Intradía yfinance: %d assets, %d snapshots, %d errores",
            result.assets_queried, result.snapshots_inserted, len(result.errors),
        )
        return result.to_dict()

    # ── Pasos internos ─────────────────────────────────────────────────────────

    def _download_batch(
        self, tickers: list[str], result: IntradayIngestResult
    ) -> Optional[pd.DataFrame]:
        """UNA sola llamada yf.download con TODOS los tickers (anti-baneo)."""
        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            try:
                logger.info(
                    "Descargando %d tickers intradía interval=%s period=%s (intento %d)…",
                    len(tickers), self.interval, self.period, attempt,
                )
                df = yf.download(
                    tickers=tickers,
                    period=self.period,
                    interval=self.interval,
                    auto_adjust=True,
                    progress=False,
                    threads=False,   # una sola conexión, sin paralelismo por ticker
                )
                if not df.empty:
                    return df
                logger.warning(
                    "DataFrame intradía vacío (intento %d/%d)", attempt, len(_RETRY_DELAYS)
                )
            except Exception as e:
                logger.error(
                    "Error descargando intradía (intento %d/%d): %s",
                    attempt, len(_RETRY_DELAYS), e,
                )
                result.errors.append(f"download intento {attempt}: {e}")

            if attempt < len(_RETRY_DELAYS):
                time.sleep(delay)

        return None

    def _normalize(
        self,
        raw_df: pd.DataFrame,
        asset_map: dict[str, int],
        result: IntradayIngestResult,
    ) -> list[dict]:
        """
        Convierte el DataFrame MultiIndex de yfinance en registros para
        raw_snapshots_intraday.

        A diferencia del carril diario, el ts es el timestamp REAL de la barra
        (no aplastado a medianoche). El campo 'interval' identifica la resolución.
        """
        records: list[dict] = []

        for ticker, asset_id in asset_map.items():
            try:
                if isinstance(raw_df.columns, pd.MultiIndex):
                    ticker_df = raw_df.xs(ticker, axis=1, level=1)
                else:
                    ticker_df = raw_df
            except KeyError:
                logger.warning("Sin datos intradía para %s", ticker)
                result.tickers_missing.append(ticker)
                continue

            ticker_df = ticker_df.dropna(subset=["Close"])
            if ticker_df.empty:
                result.tickers_missing.append(ticker)
                continue

            for ts_idx, row in ticker_df.iterrows():
                # Conserva la hora real de la barra (no se aplasta a medianoche)
                ts_str = ts_idx.isoformat() if hasattr(ts_idx, "isoformat") else str(ts_idx)
                records.append({
                    "asset_id": asset_id,
                    "ts":       ts_str,
                    "interval": self.interval,
                    "open":     _safe_float(row.get("Open")),
                    "high":     _safe_float(row.get("High")),
                    "low":      _safe_float(row.get("Low")),
                    "close":    _safe_float(row.get("Close")),
                    "volume":   _safe_float(row.get("Volume")),
                    "extra":    {},
                })

        logger.info(
            "Normalizados %d registros intradía de %d tickers",
            len(records), len(asset_map),
        )
        return records

    def _upsert(self, records: list[dict], result: IntradayIngestResult) -> None:
        for i in range(0, len(records), _UPSERT_BATCH):
            batch = records[i : i + _UPSERT_BATCH]
            try:
                self.db.table("raw_snapshots_intraday").upsert(
                    batch, on_conflict="asset_id,ts,interval"
                ).execute()
                result.snapshots_inserted += len(batch)
            except Exception as e:
                logger.error(
                    "Error en upsert intradía lote %d: %s", i // _UPSERT_BATCH, e
                )
                result.errors.append(f"upsert lote {i // _UPSERT_BATCH}: {e}")


def _safe_float(val) -> Optional[float]:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None
