"""
Ingesta del supply circulante de USDT y USDC desde DefiLlama.

Una sola llamada /stablecoins trae todos los activos; filtramos por símbolo.
close = supply circulante en USD (la "pólvora" disponible en el mercado).
"""

import logging
from typing import Optional

from app.ingest._base import fetch_json, load_asset_map, upsert_records, day_ts

logger = logging.getLogger("marea.ingest.defillama")

_SOURCE = "defillama"
_URL = "https://stablecoins.llama.fi/stablecoins"

# símbolo DefiLlama → ticker de BD
_SYMBOL_TO_TICKER: dict[str, str] = {
    "USDT": "STABLES_USDT",
    "USDC": "STABLES_USDC",
}


class IngestDefiLlama:
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

            data = fetch_json(_URL, logger_=logger)
            if not data:
                result["errors"].append("DefiLlama no devolvió datos")
                return result

            records = _normalize(data, asset_map, result)
            if records:
                ins, errs = upsert_records(self.db, records, logger_=logger)
                result["snapshots_inserted"] += ins
                result["errors"].extend(errs)

        except Exception as e:
            logger.exception("Error inesperado en DefiLlama")
            result["errors"].append(str(e))

        result["ok"] = len(result["errors"]) == 0
        logger.info("DefiLlama: %d snapshots, %d errores", result["snapshots_inserted"], len(result["errors"]))
        return result


# ──────────────────────────────────────────────────────────────────────────────

def _normalize(data: dict, asset_map: dict[str, int], result: dict) -> list[dict]:
    ts = day_ts()
    records = []
    pegged_assets = data.get("peggedAssets", [])

    for item in pegged_assets:
        symbol = (item.get("symbol") or "").upper()
        ticker = _SYMBOL_TO_TICKER.get(symbol)
        if not ticker or ticker not in asset_map:
            continue

        circulating_today = _peg_usd(item.get("circulating"))
        if circulating_today is None:
            result["tickers_missing"].append(ticker)
            continue

        prev_day = _peg_usd(item.get("circulatingPrevDay"))
        records.append({
            "asset_id": asset_map[ticker],
            "ts":       ts,
            "open":     None,
            "high":     None,
            "low":      None,
            "close":    circulating_today,
            "volume":   None,
            "extra": {
                "supply_prev_day": prev_day,
                "change_usd": (circulating_today - prev_day) if prev_day is not None else None,
                "symbol": symbol,
            },
        })

    return records


def _peg_usd(obj) -> Optional[float]:
    """Extrae el campo peggedUSD de un objeto de supply DefiLlama."""
    if not isinstance(obj, dict):
        return None
    val = obj.get("peggedUSD")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _new_result(source: str) -> dict:
    return {"source": source, "snapshots_inserted": 0, "tickers_missing": [], "errors": [], "ok": True}
