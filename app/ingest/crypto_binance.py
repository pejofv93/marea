"""
Ingesta de funding rate + open interest desde Binance Futures (API pública, sin key).

BTC_PERP y ETH_PERP son tickers propios distintos de BTC/ETH spot, evitando
conflicto de upsert entre CoinGecko (que escribe el precio spot) y Binance
(que escribe mark price + datos de derivados).

Llamadas:
  1. GET /fapi/v1/premiumIndex  (sin symbol → devuelve todos los pares, filtramos)
  2. GET /fapi/v1/openInterest?symbol=BTCUSDT
  3. GET /fapi/v1/openInterest?symbol=ETHUSDT
"""

import logging
from typing import Optional

from app.ingest._base import fetch_json, load_asset_map, upsert_records, day_ts

logger = logging.getLogger("marea.ingest.binance")

_SOURCE = "binance"
_BASE = "https://fapi.binance.com"
_FUNDING_URL = f"{_BASE}/fapi/v1/premiumIndex"
_OI_URL      = f"{_BASE}/fapi/v1/openInterest"

# Binance symbol → ticker de BD
_PAIR_TO_TICKER: dict[str, str] = {
    "BTCUSDT": "BTC_PERP",
    "ETHUSDT": "ETH_PERP",
}


class IngestBinance:
    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def run_sync(self) -> dict:
        result = _new_result(_SOURCE)
        try:
            asset_map = load_asset_map(self.db, _SOURCE, logger_=logger)
            if not asset_map:
                return result

            # Llamada 1: todos los premiumIndex → filtramos BTC/ETH
            funding_list = fetch_json(_FUNDING_URL, logger_=logger)
            if not funding_list:
                result["errors"].append("Binance premiumIndex sin datos")
                return result

            # Llamada 2 y 3: OI por par (no hay endpoint batch para OI)
            oi_by_symbol: dict[str, float] = {}
            for symbol in _PAIR_TO_TICKER:
                oi_data = fetch_json(_OI_URL, params={"symbol": symbol}, logger_=logger)
                if oi_data:
                    oi_by_symbol[symbol] = _sf(oi_data.get("openInterest"))

            records = _normalize(funding_list, oi_by_symbol, asset_map, result)
            if records:
                ins, errs = upsert_records(self.db, records, logger_=logger)
                result["snapshots_inserted"] += ins
                result["errors"].extend(errs)

        except Exception as e:
            logger.exception("Error inesperado en Binance")
            result["errors"].append(str(e))

        result["ok"] = len(result["errors"]) == 0
        logger.info("Binance: %d snapshots, %d errores", result["snapshots_inserted"], len(result["errors"]))
        return result


# ──────────────────────────────────────────────────────────────────────────────

def _normalize(
    funding_list: list,
    oi_by_symbol: dict[str, float],
    asset_map: dict[str, int],
    result: dict,
) -> list[dict]:
    ts = day_ts()
    # Indexar la lista por symbol para acceso O(1)
    funding_index = {item["symbol"]: item for item in funding_list if "symbol" in item}
    records = []

    for symbol, ticker in _PAIR_TO_TICKER.items():
        if ticker not in asset_map:
            continue
        item = funding_index.get(symbol)
        if not item:
            result["tickers_missing"].append(ticker)
            continue

        mark_price = _sf(item.get("markPrice"))
        if mark_price is None:
            result["tickers_missing"].append(ticker)
            continue

        records.append({
            "asset_id": asset_map[ticker],
            "ts":       ts,
            "open":     None,
            "high":     None,
            "low":      None,
            "close":    mark_price,
            "volume":   None,
            "extra": {
                "funding_rate":      _sf(item.get("lastFundingRate")),
                "index_price":       _sf(item.get("indexPrice")),
                "open_interest":     oi_by_symbol.get(symbol),
                "next_funding_time": item.get("nextFundingTime"),
            },
        })

    return records


def _sf(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _new_result(source: str) -> dict:
    return {"source": source, "snapshots_inserted": 0, "tickers_missing": [], "errors": [], "ok": True}
