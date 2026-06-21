"""
Orquestador de la ingesta intradía MAREA.

Ejecuta en cada ciclo intradía:
  1. yfinance: barras OHLCV 60m (o 15m) para todos los assets yfinance
     (índices, ETFs, commodities, macro). Una sola llamada batch.
  2. CoinGecko: re-lee precio/volumen ACTUAL para crypto spot (BTC, ETH…).
     Timestamp real → captura el estado del mercado en el momento del ciclo.
  3. FNG: re-lee Fear & Greed actual. Timestamp real.

Todos los datos van a raw_snapshots_intraday (no a raw_snapshots),
preservando el carril diario completamente intacto.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.ingest._base import fetch_json, load_asset_map

logger = logging.getLogger("marea.ingest.intraday_runner")

_CG_URL = "https://api.coingecko.com/api/v3/coins/markets"
_CG_HEADERS = {"Accept": "application/json", "User-Agent": "MAREA-monitor/0.2"}
_FNG_URL = "https://api.alternative.me/fng/"

# CoinGecko id → ticker de BD (mismo mapeo que el módulo diario)
_CG_ID_TO_TICKER: dict[str, str] = {
    "bitcoin":  "BTC",
    "ethereum": "ETH",
}

_UPSERT_BATCH = 500


@dataclass
class IntradayRunnerResult:
    yfinance: dict = field(default_factory=dict)
    coingecko: dict = field(default_factory=dict)
    fng: dict = field(default_factory=dict)
    total_snapshots: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "yfinance":        self.yfinance,
            "coingecko":       self.coingecko,
            "fng":             self.fng,
            "total_snapshots": self.total_snapshots,
            "errors":          self.errors,
            "ok":              len(self.errors) == 0,
        }


class IntradayRunner:
    """
    Orquesta la ingesta intradía de todas las fuentes hacia
    raw_snapshots_intraday.
    """

    def __init__(self, db=None, interval: str | None = None, period: str | None = None):
        self._db = db
        self._interval = interval
        self._period = period

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
    def period(self) -> str:
        if self._period:
            return self._period
        from app.config import settings
        return settings.intraday_period

    def run_sync(self) -> dict:
        result = IntradayRunnerResult()

        # 1. yfinance: barras OHLCV intradía
        try:
            from app.ingest.yfinance_intraday import IngestYFinanceIntraday
            yf_res = IngestYFinanceIntraday(
                db=self.db,
                interval=self.interval,
                period=self.period,
            ).run_sync()
            result.yfinance = yf_res
            result.total_snapshots += yf_res.get("snapshots_inserted", 0)
        except Exception as e:
            msg = f"yfinance_intraday: {e}"
            logger.error(msg)
            result.errors.append(msg)

        # 2. CoinGecko: precio/volumen actual (crypto spot)
        try:
            cg_res = self._ingest_coingecko_intraday()
            result.coingecko = cg_res
            result.total_snapshots += cg_res.get("snapshots_inserted", 0)
        except Exception as e:
            msg = f"coingecko_intraday: {e}"
            logger.error(msg)
            result.errors.append(msg)

        # 3. FNG: fear & greed actual
        try:
            fng_res = self._ingest_fng_intraday()
            result.fng = fng_res
            result.total_snapshots += fng_res.get("snapshots_inserted", 0)
        except Exception as e:
            msg = f"fng_intraday: {e}"
            logger.error(msg)
            result.errors.append(msg)

        logger.info(
            "IntradayRunner: %d snapshots totales, %d errores",
            result.total_snapshots, len(result.errors),
        )
        return result.to_dict()

    # ── CoinGecko ─────────────────────────────────────────────────────────────

    def _ingest_coingecko_intraday(self) -> dict:
        r = _new_src_result("coingecko")
        asset_map = load_asset_map(self.db, "coingecko", logger_=logger)
        if not asset_map:
            return r

        data = fetch_json(
            _CG_URL,
            params={
                "vs_currency": "usd",
                "ids": ",".join(_CG_ID_TO_TICKER.keys()),
                "order": "market_cap_desc",
                "sparkline": "false",
            },
            headers=_CG_HEADERS,
            logger_=logger,
        )
        if not data:
            r["errors"].append("CoinGecko intradía sin datos")
            r["ok"] = False
            return r

        ts = _now_ts()
        records = []
        for coin in data:
            cg_id  = coin.get("id", "")
            ticker = _CG_ID_TO_TICKER.get(cg_id)
            if not ticker or ticker not in asset_map:
                continue
            price = coin.get("current_price")
            if price is None:
                r["tickers_missing"].append(ticker)
                continue
            records.append({
                "asset_id": asset_map[ticker],
                "ts":       ts,
                "interval": self.interval,
                "open":     None,
                "high":     _sf(coin.get("high_24h")),
                "low":      _sf(coin.get("low_24h")),
                "close":    float(price),
                "volume":   _sf(coin.get("total_volume")),
                "extra": {
                    "volume_24h": _sf(coin.get("total_volume")),
                    "market_cap": _sf(coin.get("market_cap")),
                },
            })

        if records:
            ins, errs = _upsert_intraday(self.db, records, logger)
            r["snapshots_inserted"] += ins
            r["errors"].extend(errs)

        r["ok"] = len(r["errors"]) == 0
        return r

    # ── FNG ───────────────────────────────────────────────────────────────────

    def _ingest_fng_intraday(self) -> dict:
        r = _new_src_result("alternative_me")
        asset_map = load_asset_map(self.db, "alternative_me", logger_=logger)
        if "CRYPTO_FNG" not in asset_map:
            r["errors"].append("CRYPTO_FNG no encontrado en BD")
            r["ok"] = False
            return r

        data = fetch_json(_FNG_URL, params={"limit": "1"}, logger_=logger)
        if not data:
            r["errors"].append("FNG intradía sin datos")
            r["ok"] = False
            return r

        entries = data.get("data", [])
        if not entries:
            r["tickers_missing"].append("CRYPTO_FNG")
            return r

        raw_val = entries[0].get("value")
        if raw_val is None:
            r["tickers_missing"].append("CRYPTO_FNG")
            return r

        record = {
            "asset_id": asset_map["CRYPTO_FNG"],
            "ts":       _now_ts(),
            "interval": self.interval,
            "open":     None,
            "high":     None,
            "low":      None,
            "close":    float(raw_val),
            "volume":   None,
            "extra": {
                "value_classification": entries[0].get("value_classification"),
                "raw_value":            int(raw_val),
            },
        }
        ins, errs = _upsert_intraday(self.db, [record], logger)
        r["snapshots_inserted"] += ins
        r["errors"].extend(errs)
        r["ok"] = len(r["errors"]) == 0
        return r


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ts() -> str:
    """Timestamp UTC actual con hora REAL (no medianoche)."""
    return datetime.now(timezone.utc).isoformat()


def _upsert_intraday(db, records: list[dict], log: logging.Logger) -> tuple[int, list[str]]:
    inserted = 0
    errors: list[str] = []
    for i in range(0, len(records), _UPSERT_BATCH):
        batch = records[i : i + _UPSERT_BATCH]
        try:
            db.table("raw_snapshots_intraday").upsert(
                batch, on_conflict="asset_id,ts,interval"
            ).execute()
            inserted += len(batch)
        except Exception as e:
            msg = f"upsert_intraday lote {i // _UPSERT_BATCH}: {e}"
            log.error(msg)
            errors.append(msg)
    return inserted, errors


def _sf(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _new_src_result(source: str) -> dict:
    return {
        "source":             source,
        "snapshots_inserted": 0,
        "tickers_missing":    [],
        "errors":             [],
        "ok":                 True,
    }
