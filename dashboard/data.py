"""
Capa de acceso a datos del dashboard MAREA.

Todas las funciones load_* son solo-lectura y están cacheadas con st.cache_data
(TTL 5 min) para no martillear Supabase en cada interacción de Streamlit.

Las funciones _fetch_* contienen la lógica real (tomando `db` como argumento)
y son lo que testean los tests unitarios.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import streamlit as st

from app.db import get_db

logger = logging.getLogger("marea.dashboard.data")
CACHE_TTL = 300  # segundos


# ── Helpers internos ──────────────────────────────────────────────────────────

def _safe(fn, *args, default=None, **kwargs):
    """Ejecuta fn; en caso de excepción loguea y devuelve default."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.error("%s failed: %s", fn.__name__, exc)
        return default


# ── Régimen ──────────────────────────────────────────────────────────────────

def _fetch_regime_current(db) -> dict | None:
    resp = (
        db.table("regimes")
        .select("*")
        .eq("win", "7d")
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def _fetch_regime_history(db, limit: int = 60) -> list[dict]:
    resp = (
        db.table("regimes")
        .select("ts, regime, confidence")
        .eq("win", "7d")
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=CACHE_TTL)
def load_regime_current() -> dict | None:
    return _safe(_fetch_regime_current, get_db())


@st.cache_data(ttl=CACHE_TTL)
def load_regime_history(limit: int = 60) -> list[dict]:
    return _safe(_fetch_regime_history, get_db(), limit, default=[])


# ── Narrativa ─────────────────────────────────────────────────────────────────

def _fetch_latest_narrative(db) -> dict | None:
    resp = (
        db.table("narratives")
        .select("ts, regime_at_ts, confidence, text, llm_engine")
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


@st.cache_data(ttl=CACHE_TTL)
def load_latest_narrative() -> dict | None:
    return _safe(_fetch_latest_narrative, get_db())


# ── Flow scores ───────────────────────────────────────────────────────────────

def _fetch_flow_scores(db, window: str = "7d") -> pd.DataFrame:
    """
    Devuelve DataFrame con los flow scores más recientes por asset para la
    ventana indicada.  Columnas: ticker, name, asset_class, sector, score,
    confidence, proxy_used, n_obs.
    """
    # 1. Timestamp más reciente
    ts_resp = (
        db.table("flow_scores")
        .select("ts")
        .eq("win", window)
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    if not ts_resp.data:
        return pd.DataFrame()
    latest_ts = ts_resp.data[0]["ts"]

    # 2. Scores en ese timestamp
    scores_resp = (
        db.table("flow_scores")
        .select("asset_id, score, confidence, proxy_used, n_obs")
        .eq("ts", latest_ts)
        .eq("win", window)
        .execute()
    )
    rows = scores_resp.data or []
    if not rows:
        return pd.DataFrame()

    # 3. Info de assets (JOIN manual — más robusto que PostgREST embed)
    asset_ids = [r["asset_id"] for r in rows]
    assets_resp = (
        db.table("assets")
        .select("id, ticker, name, asset_class, sector")
        .in_("id", asset_ids)
        .execute()
    )
    asset_map: dict[int, dict] = {a["id"]: a for a in (assets_resp.data or [])}

    # 4. Merge
    records = []
    for row in rows:
        a = asset_map.get(row["asset_id"], {})
        records.append(
            {
                "ticker": a.get("ticker", "?"),
                "name": a.get("name", "?"),
                "asset_class": a.get("asset_class", "?"),
                "sector": a.get("sector"),
                "score": row.get("score"),
                "confidence": row.get("confidence", "ok"),
                "proxy_used": row.get("proxy_used", ""),
                "n_obs": row.get("n_obs", 0),
            }
        )

    df = pd.DataFrame(records)
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=CACHE_TTL)
def load_flow_scores(window: str = "7d") -> pd.DataFrame:
    return _safe(_fetch_flow_scores, get_db(), window, default=pd.DataFrame())


# ── Correlaciones ─────────────────────────────────────────────────────────────

def _fetch_correlations(
    db, matrix_type: str = "intermarket", window: str = "7d"
) -> pd.DataFrame:
    """
    Devuelve matriz de correlación como DataFrame simétrico.
    Almacena en df.attrs['decoupling_pairs'] el set de tuplas (a, b) en desacople.
    """
    ts_resp = (
        db.table("correlations")
        .select("ts")
        .eq("matrix_type", matrix_type)
        .eq("win", window)
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    if not ts_resp.data:
        return pd.DataFrame()
    latest_ts = ts_resp.data[0]["ts"]

    rows_resp = (
        db.table("correlations")
        .select("pair_a, pair_b, corr, is_decoupling")
        .eq("ts", latest_ts)
        .eq("matrix_type", matrix_type)
        .eq("win", window)
        .execute()
    )
    rows = rows_resp.data or []
    if not rows:
        return pd.DataFrame()

    labels = sorted(
        set(r["pair_a"] for r in rows) | set(r["pair_b"] for r in rows)
    )
    matrix = pd.DataFrame(1.0, index=labels, columns=labels, dtype=float)
    decoupling_pairs: set[tuple[str, str]] = set()

    for row in rows:
        a, b, corr = row["pair_a"], row["pair_b"], row.get("corr")
        if corr is not None:
            matrix.loc[a, b] = float(corr)
            matrix.loc[b, a] = float(corr)
        if row.get("is_decoupling"):
            decoupling_pairs.add((a, b))
            decoupling_pairs.add((b, a))

    matrix.attrs["decoupling_pairs"] = decoupling_pairs
    return matrix


@st.cache_data(ttl=CACHE_TTL)
def load_correlations(
    matrix_type: str = "intermarket", window: str = "7d"
) -> pd.DataFrame:
    return _safe(
        _fetch_correlations, get_db(), matrix_type, window, default=pd.DataFrame()
    )


# ── Rotaciones sectoriales ────────────────────────────────────────────────────

def _fetch_rotations(db, limit: int = 20) -> list[dict]:
    resp = (
        db.table("rotations")
        .select("ts, from_sector, to_sector, strength")
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=CACHE_TTL)
def load_rotations(limit: int = 20) -> list[dict]:
    return _safe(_fetch_rotations, get_db(), limit, default=[])


# ── Exposiciones indirectas ───────────────────────────────────────────────────

def _fetch_exposures(db) -> list[dict]:
    resp = (
        db.table("exposures")
        .select(
            "source_entity, exposed_ticker, exposure_type, "
            "relationship, confidence, sources, last_verified_at"
        )
        .order("source_entity")
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=CACHE_TTL)
def load_exposures() -> list[dict]:
    return _safe(_fetch_exposures, get_db(), default=[])


# ── Alertas ───────────────────────────────────────────────────────────────────

def _fetch_alerts(db, limit: int = 30) -> list[dict]:
    resp = (
        db.table("alerts")
        .select(
            "alert_type, entity, state, confidence, "
            "sent, not_sent_reason, ts, sent_at"
        )
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=CACHE_TTL)
def load_alerts(limit: int = 30) -> list[dict]:
    return _safe(_fetch_alerts, get_db(), limit, default=[])
