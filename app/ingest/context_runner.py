"""
Ingesta de INDICADORES DE CONTEXTO de régimen (Bloque 1).

Estos indicadores son TERMÓMETROS de estado macro, NO flujos de liquidez. Por
eso NO van por el carril de flow_scores (donde se rankea "entra/sale dinero" y
se disparan alertas), sino a su propia tabla context_indicators. Quedan
excluidos de rankings y alertas de flujo POR CONSTRUCCIÓN (nunca son flow_scores).

Indicadores (Bloque 1):
  1. btc_dominance — % de market cap de BTC sobre el total crypto (CoinGecko /global).
  2. credit_spread — ratio HYG/LQD (high-yield vs investment-grade, yfinance).
                     Cae = el high yield sufre más → spreads ensanchándose → risk-off.
  3. yield_curve   — spread 10Y-2Y en puntos porcentuales (^TNX − 2YY=F).
                     < 0 = curva invertida → señal macro de recesión / risk-off.

PUT/CALL — OMITIDO deliberadamente. Tras verificar fuentes (junio 2026):
  · El endpoint de índices de CBOE (cdn.cboe.com/api/global/...) responde 403 y
    el de estadísticas diarias (cboe.com/us/options/market_statistics/daily)
    redirige a una página gateada: NO hay API gratuita y estable de put/call total.
  · Derivarlo de las cadenas de opciones de yfinance es frágil: solo da la foto
    ACTUAL (sin histórico), lo que rompe el modelo de auto-activación por min_obs,
    además de ser lento y propenso a rate-limit.
  Conclusión: no forzamos una fuente dudosa. Si en el futuro aparece una fuente
  fiable, se añade aquí como un cuarto indicador sin tocar el resto.

ROBUSTEZ: cada fuente va en su propio try/except. Si una falla (API caída,
ticker cambiado), se registra el error y se CONTINÚA con las demás; nunca tumba
el ciclo. Si todas fallan, MAREA sigue funcionando igual que antes (el resto del
sistema no depende de estos indicadores: si faltan, se omiten limpiamente).

AUTO-ACTIVACIÓN: este módulo solo INGIERE y guarda el valor del día. La decisión
de si un indicador está "encendido" (suficiente histórico) la toma la capa de
evaluación (app/analysis/context.py), no la ingesta.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.ingest._base import day_ts, fetch_json

logger = logging.getLogger("marea.ingest.context")

INDICATOR_BTC_DOMINANCE = "btc_dominance"
INDICATOR_CREDIT_SPREAD = "credit_spread"
INDICATOR_YIELD_CURVE = "yield_curve"

_CG_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_CG_HEADERS = {"Accept": "application/json", "User-Agent": "MAREA-monitor/0.3"}

# Tickers de yfinance para credit spread y curva. ^TNX y 2YY=F vienen en % directo.
_CREDIT_TICKERS = ("HYG", "LQD")
_TNX_TICKER = "^TNX"   # 10Y (yield en % directo)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers PUROS de cálculo (testeables sin red)
# ══════════════════════════════════════════════════════════════════════════════

def compute_credit_spread(hyg_close: Optional[float], lqd_close: Optional[float]) -> Optional[float]:
    """Ratio HYG/LQD. None si falta alguno o LQD es 0 (evita división por cero)."""
    if hyg_close is None or lqd_close is None:
        return None
    if lqd_close == 0:
        return None
    return round(float(hyg_close) / float(lqd_close), 6)


def compute_yield_curve(tnx_pct: Optional[float], two_y_pct: Optional[float]) -> Optional[float]:
    """Spread 10Y-2Y en puntos porcentuales. None si falta alguna pata."""
    if tnx_pct is None or two_y_pct is None:
        return None
    return round(float(tnx_pct) - float(two_y_pct), 4)


def extract_btc_dominance(global_json: Optional[dict]) -> Optional[dict]:
    """
    Extrae el % de dominancia de BTC (y ETH como contexto) del JSON de CoinGecko
    /global. Devuelve {'value': btc_pct, 'extra': {...}} o None si no hay dato.
    """
    if not isinstance(global_json, dict):
        return None
    data = global_json.get("data") or {}
    mcp = data.get("market_cap_percentage") or {}
    btc = mcp.get("btc")
    if btc is None:
        return None
    try:
        btc = round(float(btc), 4)
    except (TypeError, ValueError):
        return None
    extra = {}
    eth = mcp.get("eth")
    if eth is not None:
        try:
            extra["eth"] = round(float(eth), 4)
        except (TypeError, ValueError):
            pass
    return {"value": btc, "extra": extra}


# ══════════════════════════════════════════════════════════════════════════════
# Fetchers (aislados; patcheables en tests)
# ══════════════════════════════════════════════════════════════════════════════

def _download_closes(tickers: list[str]) -> dict[str, float]:
    """
    Último cierre diario por ticker en UNA sola llamada batch a yfinance
    (anti rate-limit). Devuelve {ticker: close} solo para los que tienen dato.
    Aislado en función de módulo para poder mockearlo en tests.
    """
    import pandas as pd
    import yfinance as yf

    df = yf.download(
        tickers=tickers,
        period="10d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    out: dict[str, float] = {}
    if df is None or df.empty:
        return out
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                serie = df["Close"][t].dropna()
            else:
                serie = df["Close"].dropna()
            if len(serie):
                out[t] = float(serie.iloc[-1])
        except (KeyError, IndexError):
            continue
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

class ContextIngestRunner:
    """
    Orquesta la ingesta de los indicadores de contexto hacia context_indicators.
    Interfaz idéntica al resto de fuentes: run_sync() → dict con ok/errors, de
    modo que IngestAll lo trate como una fuente más sin lógica especial.
    """

    def __init__(self, db=None, short_ticker: str | None = None):
        self._db = db
        self._short_ticker = short_ticker

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    @property
    def short_ticker(self) -> str:
        if self._short_ticker:
            return self._short_ticker
        from app.config import settings
        return settings.yield_curve_short_ticker

    def run_sync(self) -> dict:
        ts = day_ts()
        rows: list[dict] = []
        errors: list[str] = []

        # ── 1. Dominancia de BTC (CoinGecko /global) ─────────────────────────
        try:
            row = self._ingest_dominance(ts)
            if row:
                rows.append(row)
        except Exception as e:  # noqa: BLE001 — fuente aislada, nunca tumba el ciclo
            msg = f"btc_dominance: {e}"
            logger.error(msg)
            errors.append(msg)

        # ── 2+3. Credit spread y curva (un solo batch de yfinance) ───────────
        try:
            rows.extend(self._ingest_market_indicators(ts))
        except Exception as e:  # noqa: BLE001
            msg = f"market_indicators: {e}"
            logger.error(msg)
            errors.append(msg)

        written = 0
        if rows:
            written, up_errors = self._upsert(rows)
            errors.extend(up_errors)

        logger.info(
            "ContextIngestRunner: %d indicadores escritos, %d errores",
            written, len(errors),
        )
        return {
            "source":            "context",
            "indicators_written": written,
            "errors":            errors,
            "ok":                len(errors) == 0,
        }

    # ── Fuentes ────────────────────────────────────────────────────────────────

    def _ingest_dominance(self, ts: str) -> Optional[dict]:
        data = fetch_json(_CG_GLOBAL_URL, headers=_CG_HEADERS, logger_=logger)
        parsed = extract_btc_dominance(data)
        if not parsed:
            logger.warning("Dominancia BTC sin dato en la respuesta de CoinGecko")
            return None
        return {
            "ts":        ts,
            "indicator": INDICATOR_BTC_DOMINANCE,
            "value":     parsed["value"],
            "extra":     parsed["extra"],
        }

    def _ingest_market_indicators(self, ts: str) -> list[dict]:
        tickers = list(_CREDIT_TICKERS) + [_TNX_TICKER, self.short_ticker]
        closes = _download_closes(tickers)
        out: list[dict] = []

        # Credit spread (HYG/LQD)
        spread = compute_credit_spread(closes.get("HYG"), closes.get("LQD"))
        if spread is not None:
            out.append({
                "ts":        ts,
                "indicator": INDICATOR_CREDIT_SPREAD,
                "value":     spread,
                "extra":     {"hyg": closes.get("HYG"), "lqd": closes.get("LQD")},
            })
        else:
            logger.warning("Credit spread omitido: falta HYG o LQD en yfinance")

        # Curva 10Y-2Y (^TNX − short)
        curve = compute_yield_curve(closes.get(_TNX_TICKER), closes.get(self.short_ticker))
        if curve is not None:
            out.append({
                "ts":        ts,
                "indicator": INDICATOR_YIELD_CURVE,
                "value":     curve,
                "extra":     {"tnx": closes.get(_TNX_TICKER),
                              "two_y": closes.get(self.short_ticker),
                              "short_ticker": self.short_ticker},
            })
        else:
            logger.warning("Curva de tipos omitida: falta ^TNX o %s en yfinance", self.short_ticker)

        return out

    # ── Persistencia ────────────────────────────────────────────────────────────

    def _upsert(self, rows: list[dict]) -> tuple[int, list[str]]:
        errors: list[str] = []
        try:
            self.db.table("context_indicators").upsert(
                rows, on_conflict="ts,indicator"
            ).execute()
            return len(rows), errors
        except Exception as e:  # noqa: BLE001
            msg = f"upsert context_indicators: {e}"
            logger.error(msg)
            errors.append(msg)
            return 0, errors
