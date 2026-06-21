"""
Ingesta del Fear & Greed Index crypto desde Alternative.me.

close = valor numérico 0-100.
extra = {value_classification, raw_value}.
"""

import logging
from typing import Optional

from app.ingest._base import fetch_json, load_asset_map, upsert_records, day_ts

logger = logging.getLogger("marea.ingest.fng")

_SOURCE = "alternative_me"
_URL = "https://api.alternative.me/fng/"
_TICKER = "CRYPTO_FNG"


class IngestFNG:
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
            if _TICKER not in asset_map:
                result["errors"].append(f"{_TICKER} no encontrado en BD")
                return result

            data = fetch_json(_URL, params={"limit": "1"}, logger_=logger)
            if not data:
                result["errors"].append("Alternative.me FNG sin datos")
                return result

            record = _normalize(data, asset_map[_TICKER], result)
            if record:
                ins, errs = upsert_records(self.db, [record], logger_=logger)
                result["snapshots_inserted"] += ins
                result["errors"].extend(errs)

        except Exception as e:
            logger.exception("Error inesperado en FNG")
            result["errors"].append(str(e))

        result["ok"] = len(result["errors"]) == 0
        logger.info("FNG: %d snapshots, %d errores", result["snapshots_inserted"], len(result["errors"]))
        return result


# ──────────────────────────────────────────────────────────────────────────────

def _normalize(data: dict, asset_id: int, result: dict) -> Optional[dict]:
    entries = data.get("data", [])
    if not entries:
        result["tickers_missing"].append(_TICKER)
        return None

    entry = entries[0]
    raw_value = entry.get("value")
    if raw_value is None:
        result["tickers_missing"].append(_TICKER)
        return None

    return {
        "asset_id": asset_id,
        "ts":       day_ts(),
        "open":     None,
        "high":     None,
        "low":      None,
        "close":    float(raw_value),
        "volume":   None,
        "extra": {
            "value_classification": entry.get("value_classification"),
            "raw_value": int(raw_value),
        },
    }


def _new_result(source: str) -> dict:
    return {"source": source, "snapshots_inserted": 0, "tickers_missing": [], "errors": [], "ok": True}
