"""
Construye el snapshot estructurado para la narrativa LLM (Sesión 7).

Lee SOLO datos internos de MAREA:
  flow_scores   → top inflows/outflows, scores por clase, cold_start
  regimes       → régimen actual + confianza + señales
  correlations  → desacoples detectados
  rotations     → rotaciones sectoriales recientes
  exposures     → exposiciones de alta confianza

El snapshot es deliberadamente compacto (no cientos de filas) para no
disparar tokens innecesarios. El LLM solo verá lo que está aquí.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("marea.narrative.snapshot")

_TOP_N = 3          # top/bottom scores individuales
_TOP_ROTATIONS = 5
_TOP_EXPOSURES = 5
_LOW_CONF_THRESHOLD = 0.4   # confidence < esto → advertencia de incertidumbre


def build_snapshot(db) -> dict:
    """
    Construye el snapshot compacto del estado actual del mercado.
    Cada sección captura sus propias excepciones para que un fallo parcial
    no aborte el snapshot completo.
    """
    from app.ingest._base import day_ts

    snapshot: dict = {}

    # 1. Régimen actual (window='7d', el más reciente)
    try:
        resp = (
            db.table("regimes")
            .select("ts,win,regime,confidence,signals")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if rows:
            r = rows[0]
            snapshot["regime"] = {
                "name": r["regime"],
                "confidence": float(r["confidence"]),
                "signals": r.get("signals") or [],
                "ts": r["ts"],
            }
        else:
            snapshot["regime"] = None
    except Exception as e:
        logger.warning("Error cargando régimen: %s", e)
        snapshot["regime"] = None

    # 2. Flow scores (window='7d', dedup por asset, ordenados por score)
    try:
        resp = (
            db.table("flow_scores")
            .select("asset_id,ts,win,score,confidence,assets(ticker,asset_class,sector)")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
        raw = resp.data or []

        # Dedup por asset_id: mantener el más reciente (ya ordenado desc)
        seen: dict = {}
        for row in raw:
            aid = row["asset_id"]
            if aid not in seen:
                seen[aid] = row
        unique = list(seen.values())

        # Ordenar por score (mayor a menor)
        sorted_scores = sorted(unique, key=lambda r: r.get("score") or 0.0, reverse=True)

        def _fmt(row: dict) -> dict:
            ai = row.get("assets") or {}
            return {
                "ticker": ai.get("ticker", "?"),
                "asset_class": ai.get("asset_class", "?"),
                "sector": ai.get("sector"),
                "score": round(row.get("score") or 0.0, 3),
                "confidence": row.get("confidence", "low"),
            }

        snapshot["top_inflow"] = [_fmt(r) for r in sorted_scores[:_TOP_N]]
        snapshot["top_outflow"] = (
            [_fmt(r) for r in reversed(sorted_scores[-_TOP_N:])]
            if len(sorted_scores) >= _TOP_N
            else [_fmt(r) for r in reversed(sorted_scores)]
        )

        # Scores por clase (promedio aritmético dentro de cada asset_class)
        class_sums: dict[str, list[float]] = {}
        for row in unique:
            ac = (row.get("assets") or {}).get("asset_class", "unknown")
            class_sums.setdefault(ac, []).append(row.get("score") or 0.0)
        snapshot["class_scores"] = {
            ac: round(sum(vs) / len(vs), 3)
            for ac, vs in class_sums.items()
        }

        # Cold start: verdadero si no hay datos o >50 % tienen confianza "low"
        low_count = sum(1 for r in unique if r.get("confidence") == "low")
        total = len(unique)
        snapshot["cold_start"] = total == 0 or (low_count / total) > 0.5

    except Exception as e:
        logger.warning("Error cargando flow_scores: %s", e)
        snapshot["top_inflow"] = []
        snapshot["top_outflow"] = []
        snapshot["class_scores"] = {}
        snapshot["cold_start"] = True

    # 3. Desacoples detectados (correlaciones con is_decoupling=True, window='7d')
    try:
        resp = (
            db.table("correlations")
            .select("ts,pair_a,pair_b,corr,matrix_type")
            .eq("is_decoupling", True)
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(10)
            .execute()
        )
        snapshot["decouplings"] = [
            {
                "pair": f"{r['pair_a']}/{r['pair_b']}",
                "corr": round(r.get("corr") or 0.0, 3),
                "type": r.get("matrix_type", ""),
            }
            for r in (resp.data or [])
        ]
    except Exception as e:
        logger.warning("Error cargando desacoples: %s", e)
        snapshot["decouplings"] = []

    # 4. Rotaciones sectoriales recientes
    try:
        resp = (
            db.table("rotations")
            .select("ts,from_sector,to_sector,strength")
            .order("ts", desc=True)
            .limit(_TOP_ROTATIONS)
            .execute()
        )
        snapshot["rotations"] = [
            {
                "from": r.get("from_sector", "?"),
                "to": r.get("to_sector", "?"),
                "strength": round(r.get("strength") or 0.0, 3),
            }
            for r in (resp.data or [])
        ]
    except Exception as e:
        logger.warning("Error cargando rotaciones: %s", e)
        snapshot["rotations"] = []

    # 5. Exposiciones de mayor confianza (confirmado_oficial primero)
    try:
        resp = (
            db.table("exposures")
            .select("source_entity,exposed_ticker,exposure_type,confidence")
            .order("confidence")
            .limit(_TOP_EXPOSURES)
            .execute()
        )
        snapshot["exposures"] = [
            {
                "entity": r.get("source_entity", "?"),
                "ticker": r.get("exposed_ticker", "?"),
                "type": r.get("exposure_type", "?"),
                "confidence": r.get("confidence", "especulacion"),
            }
            for r in (resp.data or [])
        ]
    except Exception as e:
        logger.warning("Error cargando exposiciones: %s", e)
        snapshot["exposures"] = []

    snapshot["generated_at"] = day_ts()
    return snapshot
