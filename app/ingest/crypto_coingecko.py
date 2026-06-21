"""
Ingesta de precios BTC y ETH desde CoinGecko API (free tier).

ANTI-BANEO: UNA sola llamada /coins/markets con ids=bitcoin,ethereum.
Nunca una llamada por moneda.
"""

import logging
from typing import Optional

from app.ingest._base import fetch_json, load_asset_map, upsert_records, day_ts

logger = logging.getLogger("marea.ingest.coingecko")

_SOURCE = "coingecko"
_URL = "https://api.coingecko.com/api/v3/coins/markets"
_HEADERS = {"Accept": "application/json", "User-Agent": "MAREA-monitor/0.2"}

# Mapeo CoinGecko id → ticker de BD
_CG_ID_TO_TICKER: dict[str, str] = {
    "bitcoin":  "BTC",
    "ethereum": "ETH",
}


class IngestCoinGecko:
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

            # UNA sola llamada para TODOS los ids (anti-baneo)
            data = fetch_json(
                _URL,
                params={
                    "vs_currency": "usd",
                    "ids": ",".join(_CG_ID_TO_TICKER.keys()),   # "bitcoin,ethereum"
                    "order": "market_cap_desc",
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
                headers=_HEADERS,
                logger_=logger,
            )
            if not data:
                result["errors"].append("CoinGecko no devolvió datos")
                return result

            records = _normalize(data, asset_map, result)
            if records:
                ins, errs = upsert_records(self.db, records, logger_=logger)
                result["snapshots_inserted"] += ins
                result["errors"].extend(errs)

        except Exception as e:
            logger.exception("Error inesperado en CoinGecko")
            result["errors"].append(str(e))

        result["ok"] = len(result["errors"]) == 0
        logger.info("CoinGecko: %d snapshots, %d errores", result["snapshots_inserted"], len(result["errors"]))
        return result


# ──────────────────────────────────────────────────────────────────────────────

def _normalize(data: list, asset_map: dict[str, int], result: dict) -> list[dict]:
    ts = day_ts()
    records = []
    for coin in data:
        cg_id = coin.get("id", "")
        ticker = _CG_ID_TO_TICKER.get(cg_id)
        if not ticker or ticker not in asset_map:
            continue
        price = coin.get("current_price")
        if price is None:
            result["tickers_missing"].append(ticker)
            continue
        records.append({
            "asset_id": asset_map[ticker],
            "ts":       ts,
            "open":     None,
            "high":     coin.get("high_24h"),
            "low":      coin.get("low_24h"),
            "close":    float(price),
            "volume":   _sf(coin.get("total_volume")),
            "extra": {
                "market_cap":           _sf(coin.get("market_cap")),
                "volume_24h":           _sf(coin.get("total_volume")),
                "price_change_24h_pct": _sf(coin.get("price_change_percentage_24h")),
                "circulating_supply":   _sf(coin.get("circulating_supply")),
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
