"""
Matrices de correlación intermercado y sectorial.

Matriz A (intermarket): correlaciones rolling entre clases de activo agregadas
(crypto, equities, gold, silver, bonds, dollar, vix). Detecta desacoples
comparando ventana corta (7d) con ventana larga (30d).

Matriz B (sector): correlaciones rolling entre ETFs sectoriales individuales
(SOXX, SMH, XME, GDX, SIL, ITA, XAR, XLE, XLK, XLF, XLV).

Ambas matrices se calculan sobre los flow_scores diarios con window='7d'
(z-score de 7 días, más sensible a movimientos recientes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("marea.analysis.correlation")

# ── ETFs sectoriales — Matriz B ───────────────────────────────────────────────
SECTOR_ETFS: list[str] = [
    "SOXX", "SMH",                      # semiconductores
    "XME", "GDX", "SIL",               # metales / minería
    "ITA", "XAR",                       # aeroespacial / defensa
    "XLE", "XLK", "XLF", "XLV",        # energía, tech, financiero, salud
]

# ── Umbrales de desacople ─────────────────────────────────────────────────────
# Un par se considera "en desacople" si estaba muy correlacionado (base ≥ 0.7)
# pero la correlación reciente (7d) cae más de 0.5 puntos respecto a la base (30d).
DECOUPLE_BASE_THRESHOLD: float = 0.7
DECOUPLE_DROP_THRESHOLD: float = 0.5

# Mínimo de observaciones por ventana para calcular correlación fiable
MIN_CORR_OBS: int = 4

# Días de histórico a cargar desde flow_scores
LOOKBACK_DAYS: int = 40

# ── Asignación ticker → clase intermercado ────────────────────────────────────

def ticker_to_intermarket_class(
    ticker: str,
    asset_class: str,
    sector: Optional[str],
) -> Optional[str]:
    """
    Asigna un ticker a su clase intermercado para la Matriz A.
    Devuelve None si el ticker no pertenece a ninguna clase (ej. sector ETFs,
    stablecoins o assets de sentimiento que no representan flujo directo).

    DXY y VIX se incluyen en la Matriz A como clases separadas; en el
    clasificador de régimen actúan como moduladores, no como flujo.
    """
    if asset_class == "onchain":
        return None
    if ticker == "CRYPTO_FNG":
        return None
    # Sector ETFs van exclusivamente a Matriz B
    if ticker in SECTOR_ETFS:
        return None

    # Coincidencias explícitas (máxima prioridad)
    if ticker in ("GC=F", "GLD"):
        return "gold"
    if ticker in ("SI=F", "SLV"):
        return "silver"
    if ticker == "^TNX":
        return "bonds"
    if ticker == "DX-Y.NYB":
        return "dollar"
    if ticker == "^VIX":
        return "vix"
    if ticker == "IBIT":
        return "crypto"   # Bitcoin ETF → clase crypto

    # Por asset_class
    if asset_class == "crypto":
        return "crypto"
    if asset_class in ("index", "stock"):
        return "equities"
    if asset_class == "etf" and sector == "broad_market":
        return "equities"

    return None


# ── Funciones puras (testeables sin BD) ───────────────────────────────────────

def aggregate_to_class_scores(records: list[dict]) -> pd.DataFrame:
    """
    Agrega scores individuales a nivel de clase intermercado.

    Entrada: lista de dicts {ticker, ts, score, asset_class, sector}.
    Salida: DataFrame con índice=ts (date UTC), columnas=clases, valores=mean(score).
    """
    rows = []
    for r in records:
        cls = ticker_to_intermarket_class(
            r.get("ticker", ""),
            r.get("asset_class", ""),
            r.get("sector"),
        )
        if cls is None:
            continue
        rows.append({"ts": r["ts"], "class": cls, "score": r["score"]})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.normalize()
    # Media por (ts, clase): si hay varios assets en la misma clase el mismo día
    pivot = (
        df.groupby(["ts", "class"])["score"]
        .mean()
        .unstack("class")
    )
    pivot.sort_index(inplace=True)
    return pivot


def filter_to_sector_scores(records: list[dict]) -> pd.DataFrame:
    """
    Filtra a ETFs sectoriales para la Matriz B.

    Salida: DataFrame con índice=ts (date UTC), columnas=ticker ETF, valores=score.
    """
    rows = []
    for r in records:
        if r.get("ticker") in SECTOR_ETFS:
            rows.append({"ts": r["ts"], "ticker": r["ticker"], "score": r["score"]})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.normalize()
    pivot = (
        df.groupby(["ts", "ticker"])["score"]
        .mean()
        .unstack("ticker")
    )
    pivot.sort_index(inplace=True)
    return pivot


def compute_pairwise_corr(df: pd.DataFrame, window: int) -> dict[tuple[str, str], float]:
    """
    Calcula correlaciones de Pearson entre todos los pares de columnas del DataFrame,
    usando únicamente las últimas `window` filas (ventana temporal).

    Devuelve: dict {(pair_a, pair_b): corr} donde pair_a < pair_b (orden alfabético).
    Los pares con NaN (constantes o datos insuficientes) se omiten.
    """
    subset = df.iloc[-window:] if len(df) >= window else df
    if len(subset) < MIN_CORR_OBS:
        return {}

    corr_matrix = subset.corr(method="pearson")
    result: dict[tuple[str, str], float] = {}
    cols = list(corr_matrix.columns)
    for i, col_a in enumerate(cols):
        for col_b in cols[i + 1:]:
            val = corr_matrix.loc[col_a, col_b]
            if pd.isna(val):
                continue
            pair = tuple(sorted([col_a, col_b]))  # siempre (menor, mayor) alfabético
            result[pair] = float(round(val, 6))  # type: ignore[arg-type]

    return result


def detect_decoupling(corr_short: float, corr_long: float) -> bool:
    """
    Detecta si un par que estaba fuertemente correlacionado en la ventana larga
    se ha desacoplado en la ventana corta.

    Condición: |corr_long| > BASE_THRESHOLD y |corr_short - corr_long| > DROP_THRESHOLD.
    """
    was_correlated = abs(corr_long) >= DECOUPLE_BASE_THRESHOLD
    has_dropped = abs(corr_short - corr_long) >= DECOUPLE_DROP_THRESHOLD
    return was_correlated and has_dropped


def build_corr_rows(
    df: pd.DataFrame,
    matrix_type: str,
    ts: str,
) -> list[dict]:
    """
    Construye las filas para la tabla `correlations` a partir del DataFrame pivotado.

    Calcula correlaciones para ventana 7d y 30d. La flag `is_decoupling` sólo
    se activa en las filas de window='7d' cuando la correlación reciente diverge
    de la base de 30 días.

    ts: ISO string (midnight UTC) para el campo ts de la tabla.
    """
    corr_7d = compute_pairwise_corr(df, window=7)
    corr_30d = compute_pairwise_corr(df, window=30)

    all_pairs: set[tuple[str, str]] = set(corr_7d.keys()) | set(corr_30d.keys())
    rows: list[dict] = []

    for pair in sorted(all_pairs):
        a, b = pair
        c7 = corr_7d.get(pair)
        c30 = corr_30d.get(pair)

        is_dec = (
            detect_decoupling(c7, c30)
            if c7 is not None and c30 is not None
            else False
        )

        if c7 is not None:
            rows.append({
                "ts": ts,
                "win": "7d",
                "matrix_type": matrix_type,
                "pair_a": a,
                "pair_b": b,
                "corr": c7,
                "is_decoupling": is_dec,
            })
        if c30 is not None:
            rows.append({
                "ts": ts,
                "win": "30d",
                "matrix_type": matrix_type,
                "pair_a": a,
                "pair_b": b,
                "corr": c30,
                "is_decoupling": False,  # el desacople se mide en la ventana corta
            })

    return rows


# ── CorrelationBuilder — acceso a BD ─────────────────────────────────────────

class CorrelationBuilder:
    """Carga flow_scores y construye ambas matrices de correlación."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from app.db import get_db
        return get_db()

    def load_scores(self, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
        """
        Carga flow_scores con window='7d' de los últimos `lookback_days` días.
        Devuelve lista de dicts con ticker, asset_class, sector aplanados.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()

        try:
            resp = (
                self.db.table("flow_scores")
                .select("ts,score,confidence,assets(ticker,asset_class,sector)")
                .gte("ts", cutoff)
                .eq("win", "7d")
                .execute()
            )
            return self._flatten(resp.data or [])
        except Exception as e:
            logger.error("Error cargando scores para correlación: %s", e)
            return []

    @staticmethod
    def _flatten(records: list[dict]) -> list[dict]:
        """Aplana el join anidado assets(…) a campos de primer nivel."""
        flat = []
        for r in records:
            assets = r.get("assets") or {}
            if not assets:
                continue
            score = r.get("score")
            if score is None:
                continue
            flat.append({
                "ts": r["ts"],
                "score": float(score),
                "confidence": r.get("confidence") or "low",
                "ticker": assets.get("ticker", ""),
                "asset_class": assets.get("asset_class", ""),
                "sector": assets.get("sector"),
            })
        return flat

    def build(self, ts: str) -> tuple[list[dict], list[dict]]:
        """
        Carga datos, construye Matriz A (intermarket) y Matriz B (sector).
        Devuelve (intermarket_rows, sector_rows) listos para upsert.
        """
        records = self.load_scores()
        if not records:
            logger.warning("Sin datos de scores para construir correlaciones")
            return [], []

        class_df = aggregate_to_class_scores(records)
        sector_df = filter_to_sector_scores(records)

        intermarket_rows = build_corr_rows(class_df, "intermarket", ts) if not class_df.empty else []
        sector_rows = build_corr_rows(sector_df, "sector", ts) if not sector_df.empty else []

        n_dec_inter = sum(1 for r in intermarket_rows if r.get("is_decoupling") and r["win"] == "7d")
        n_dec_sec = sum(1 for r in sector_rows if r.get("is_decoupling") and r["win"] == "7d")
        logger.info(
            "Correlaciones: %d pares intermarket, %d pares sector — desacoples: %d inter, %d sector",
            len([r for r in intermarket_rows if r["win"] == "7d"]),
            len([r for r in sector_rows if r["win"] == "7d"]),
            n_dec_inter,
            n_dec_sec,
        )

        return intermarket_rows, sector_rows

    def get_class_df(self) -> pd.DataFrame:
        """Devuelve el DataFrame de scores por clase (para el clasificador de régimen)."""
        records = self.load_scores()
        return aggregate_to_class_scores(records) if records else pd.DataFrame()

    def get_sector_df(self) -> pd.DataFrame:
        """Devuelve el DataFrame de scores por ETF sectorial."""
        records = self.load_scores()
        return filter_to_sector_scores(records) if records else pd.DataFrame()
