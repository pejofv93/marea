"""
Recálculo del universo dinámico por reglas.

Franjas:
  1. Top-20 cripto por market cap  → CoinGecko, 1 llamada en lote
  2. Top-50 acciones por volumen   → yfinance, 1 llamada en lote sobre STOCK_POOL

Soft-delete: is_active=True/False nunca borra histórico de raw_snapshots.
is_fixed=True: inmune a la desactivación — sea cual sea el ranking.
"""

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

from app.ingest._base import fetch_json
from app.universe.rules import STOCK_POOL, TOP_CRYPTO_N, TOP_STOCK_N

logger = logging.getLogger("marea.universe.dynamic")

_CG_URL = "https://api.coingecko.com/api/v3/coins/markets"
_CG_HEADERS = {"Accept": "application/json", "User-Agent": "MAREA-monitor/0.2"}


class UniverseRecomputer:
    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    # ── Punto de entrada principal ─────────────────────────────────────────────

    def run_sync(self) -> dict:
        result = _new_result()
        self._recompute_top_crypto(result)
        self._recompute_top_stocks(result)
        result["total_active"] = self._count_active()
        return result

    # Métodos públicos individuales (útiles para tests y endpoints parciales)
    def recompute_top_crypto(self) -> dict:
        result = _new_result()
        self._recompute_top_crypto(result)
        return result

    def recompute_top_stocks(self) -> dict:
        result = _new_result()
        self._recompute_top_stocks(result)
        return result

    # ── Lógica de cada franja ──────────────────────────────────────────────────

    def _recompute_top_crypto(self, result: dict) -> None:
        """Una sola llamada CoinGecko → top-N por market cap."""
        data = fetch_json(
            _CG_URL,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": str(TOP_CRYPTO_N),
                "page": "1",
                "sparkline": "false",
            },
            headers=_CG_HEADERS,
            logger_=logger,
        )
        if data is None:
            result["errors"].append("CoinGecko top-crypto: sin respuesta")
            return

        top_items = []
        for rank, coin in enumerate(data, 1):
            symbol = (coin.get("symbol") or "").upper()
            if not symbol:
                continue
            top_items.append({
                "ticker": symbol,
                "name":   coin.get("name", symbol),
                "sector": None,
                "rank":   rank,
            })

        self._apply_changes(
            top_items=top_items,
            ingest_source="coingecko",
            asset_class="crypto",
            reason="top_20_crypto",
            result=result,
        )

    def _recompute_top_stocks(self, result: dict) -> None:
        """Una sola llamada yfinance sobre STOCK_POOL → top-N por volumen 5d."""
        try:
            df = yf.download(
                tickers=STOCK_POOL,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        except Exception as e:
            logger.error("Error descargando pool de stocks: %s", e)
            result["errors"].append(f"stock_pool_download: {e}")
            return

        if df is None or df.empty:
            result["errors"].append("Pool de stocks: DataFrame vacío")
            return

        volumes = _compute_avg_volume(df, STOCK_POOL)
        sorted_tickers = sorted(volumes, key=lambda t: volumes[t] or 0.0, reverse=True)
        top_50 = [t for t in sorted_tickers if (volumes.get(t) or 0) > 0][:TOP_STOCK_N]

        top_items = [{"ticker": t, "name": t, "sector": None, "rank": i + 1}
                     for i, t in enumerate(top_50)]

        self._apply_changes(
            top_items=top_items,
            ingest_source="yfinance",
            asset_class="stock",
            reason="top_50_stock",
            result=result,
        )

    # ── Motor de cambios (soft-delete) ─────────────────────────────────────────

    def _apply_changes(
        self,
        top_items: list[dict],
        ingest_source: str,
        asset_class: str,
        reason: str,
        result: dict,
    ) -> None:
        """
        Activa / crea los assets en top_items.
        Desactiva los que ya no están en el top (solo is_fixed=False).
        Nunca toca is_fixed=True.
        """
        top_tickers = {item["ticker"] for item in top_items}

        try:
            resp = (
                self.db.table("assets")
                .select("id,ticker,is_active,is_fixed")
                .eq("ingest_source", ingest_source)
                .eq("asset_class", asset_class)
                .execute()
            )
            existing = {row["ticker"]: row for row in (resp.data or [])}
        except Exception as e:
            logger.error("Error cargando assets existentes: %s", e)
            result["errors"].append(f"load_existing: {e}")
            return

        # Paso 1: activar o crear
        for item in top_items:
            ticker = item["ticker"]
            if ticker in existing:
                asset = existing[ticker]
                if asset["is_fixed"]:
                    continue  # Escudo: los fijos son inmunes al recálculo
                if not asset["is_active"]:
                    self._set_active(asset["id"], True)
                    self._log_history(asset["id"], "activated", reason, item.get("rank"))
                    result["activated"].append(ticker)
                # Si ya estaba activo: no-op
            else:
                new_id = self._insert_asset(item, ingest_source, asset_class)
                if new_id:
                    self._log_history(new_id, "activated", reason, item.get("rank"))
                result["activated"].append(ticker)

        # Paso 2: desactivar los que salieron del top (solo dinámicos)
        for ticker, asset in existing.items():
            if (ticker not in top_tickers
                    and asset["is_active"]
                    and not asset["is_fixed"]):
                self._set_active(asset["id"], False)
                self._log_history(asset["id"], "deactivated", reason, None)
                result["deactivated"].append(ticker)

    # ── Operaciones sobre la BD ────────────────────────────────────────────────

    def _set_active(self, asset_id: int, is_active: bool) -> None:
        try:
            (self.db.table("assets")
             .update({"is_active": is_active})
             .eq("id", asset_id)
             .execute())
        except Exception as e:
            logger.error("Error actualizando is_active para asset %d: %s", asset_id, e)

    def _insert_asset(self, item: dict, ingest_source: str, asset_class: str) -> Optional[int]:
        try:
            resp = (self.db.table("assets").insert({
                "ticker":       item["ticker"],
                "name":         item.get("name", item["ticker"]),
                "asset_class":  asset_class,
                "sector":       item.get("sector"),
                "ingest_source": ingest_source,
                "is_fixed":     False,
                "is_active":    True,
            }).execute())
            data = getattr(resp, "data", None) or []
            return data[0].get("id") if data else None
        except Exception as e:
            logger.error("Error creando asset %s: %s", item["ticker"], e)
            return None

    def _log_history(self, asset_id: int, action: str, reason: str, rank: Optional[int]) -> None:
        try:
            payload: dict = {"asset_id": asset_id, "action": action, "reason": reason}
            if rank is not None:
                payload["rank"] = rank
            self.db.table("universe_history").insert(payload).execute()
        except Exception as e:
            logger.warning("Error en universe_history: %s", e)

    def _count_active(self) -> dict[str, int]:
        try:
            resp = (
                self.db.table("assets")
                .select("asset_class")
                .eq("is_active", True)
                .execute()
            )
            counts: dict[str, int] = {}
            for row in (resp.data or []):
                cls = row["asset_class"]
                counts[cls] = counts.get(cls, 0) + 1
            return counts
        except Exception as e:
            logger.error("Error contando assets activos: %s", e)
            return {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_avg_volume(df: pd.DataFrame, tickers: list[str]) -> dict[str, float]:
    volumes: dict[str, float] = {}
    for ticker in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                vol_series = df.xs(ticker, axis=1, level=1)["Volume"]
            else:
                vol_series = df["Volume"]
            avg = float(vol_series.dropna().mean())
            volumes[ticker] = avg if not pd.isna(avg) else 0.0
        except (KeyError, Exception):
            volumes[ticker] = 0.0
    return volumes


def _new_result() -> dict:
    return {"activated": [], "deactivated": [], "errors": [], "total_active": {}}
