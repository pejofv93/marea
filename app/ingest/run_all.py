"""
Orquestador: lanza todas las fuentes de ingesta en un solo ciclo.
Cada fuente es independiente — si una falla, las demás continúan.
"""

import logging
import time

from app.ingest.yfinance_fixed import IngestFixedUniverse
from app.ingest.crypto_coingecko import IngestCoinGecko
from app.ingest.crypto_defillama import IngestDefiLlama
from app.ingest.crypto_binance import IngestBinance
from app.ingest.crypto_fng import IngestFNG
from app.ingest.context_runner import ContextIngestRunner

logger = logging.getLogger("marea.ingest.run_all")

# Pausa entre fuentes HTTP para respetar rate limits de APIs free tier
_HTTP_PAUSE_S = 1.0


class IngestAll:
    def __init__(self, db=None):
        self._db = db

    def run_sync(self) -> dict:
        sources = [
            ("yfinance_fixed", IngestFixedUniverse(db=self._db)),
            ("coingecko",      IngestCoinGecko(db=self._db)),
            ("defillama",      IngestDefiLlama(db=self._db)),
            ("binance",        IngestBinance(db=self._db)),
            ("fng",            IngestFNG(db=self._db)),
            # Indicadores de contexto de régimen (Bloque 1): termómetros macro,
            # NO flujo. Van a context_indicators, no a raw_snapshots. Como toda
            # fuente, si falla se registra y el ciclo continúa.
            ("context",        ContextIngestRunner(db=self._db)),
        ]

        by_source: dict[str, dict] = {}
        total_snapshots = 0
        all_errors: list[str] = []

        for name, ingestor in sources:
            try:
                result = ingestor.run_sync()
                by_source[name] = result
                total_snapshots += result.get("snapshots_inserted", 0)
                for err in result.get("errors", []):
                    all_errors.append(f"{name}: {err}")
            except Exception as e:
                logger.error("Fuente %s falló con excepción no capturada: %s", name, e)
                by_source[name] = {
                    "source": name,
                    "snapshots_inserted": 0,
                    "errors": [str(e)],
                    "ok": False,
                }
                all_errors.append(f"{name}: {e}")

            # Pausa respetuosa entre fuentes HTTP (no aplica a yfinance que tiene su propio control)
            if name != "yfinance_fixed":
                time.sleep(_HTTP_PAUSE_S)

        logger.info(
            "Ciclo completo: %d snapshots en %d fuentes, %d errores",
            total_snapshots, len(sources), len(all_errors),
        )
        return {
            "total_snapshots": total_snapshots,
            "by_source":       by_source,
            "errors":          all_errors,
            "ok":              len(all_errors) == 0,
        }
