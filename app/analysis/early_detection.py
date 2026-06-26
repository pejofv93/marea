"""
Detección TEMPRANA de señales nacientes (Bloque 4).

Dos lecturas que avisan antes de que algo sea obvio, ambas con AUTO-ACTIVACIÓN
(aquí CRÍTICA: necesitan una "línea base de normalidad" antes de poder juzgar lo
anormal, así que despiertan MÁS TARDE que los demás bloques — y eso es correcto):

  1. CORRELACIONES QUE SE ROMPEN (desacoples): dos activos que históricamente se
     movían juntos se SEPARAN → señal temprana de rotación. Vigila PARES CLÁSICOS
     con sentido económico (oro/plata, S&P/Nasdaq, semis, BTC/ETH, oro/mineras…)
     y AÑADE los pares que la propia matriz detecte como fuerte y establemente
     correlacionados. Cierra el círculo: dice qué hace CADA lado (entra/sale).

  2. VOLUMEN ANÓMALO: un activo con volumen muy por encima de lo NORMAL *para él*
     (vs su propia media/desviación histórica). Es una SEÑAL DE ATENCIÓN ("mira
     aquí"), no un flujo: se combina con la dirección del flujo para decir si esa
     atención es de entrada o de salida.

POR QUÉ SIN MIGRACIÓN NUEVA (se calcula al vuelo)
  · Desacoples: se correlacionan las SERIES de flow score (penalizado, Bloque 2)
    por ticker, que ya viven en flow_scores. Reutilizamos la maquinaria de la
    matriz de correlación (Sesión 5) sobre un pivot a nivel de ticker — la matriz
    existente agrega por CLASE (gold, equities, crypto…), lo que colapsa pares
    como S&P/Nasdaq o BTC/ETH en una sola columna; aquí necesitamos el detalle
    por ticker, así que pivotamos por ticker pero con los MISMOS umbrales.
  · Volumen: la distribución histórica de volumen de cada activo ya está en
    raw_snapshots.volume. Media/σ se calculan al vuelo.
  Persistir líneas base en una tabla nueva solo duplicaría datos ya almacenados.
  La auto-activación vive en los umbrales de observaciones, no en una tabla.

AUTO-ACTIVACIÓN
  · Correlación: un par no se vigila hasta tener ≥ `corr_min_obs` observaciones en
    la ventana base (si no, su correlación "estable" no es fiable). Si NINGÚN par
    llega → corr_baseline_ready=False ("estableciendo línea base"): no se muestra
    NADA (jamás un desacople falso por falta de datos).
  · Volumen: un activo no se juzga hasta tener ≥ `vol_min_obs` observaciones para
    una media/σ fiables. Si ninguno llega → vol_baseline_ready=False.

RESPETA LO EXISTENTE
  · Score PENALIZADO por credibilidad como base (lo que guarda flow_scores.score).
  · Excluye termómetros de sentimiento (^VIX, CRYPTO_FNG) — un desacople del VIX o
    una anomalía de volumen del Fear&Greed no tienen sentido. Los indicadores
    macro del Bloque 1 viven en context_indicators (no en flow_scores/raw_snapshots),
    así que quedan excluidos POR CONSTRUCCIÓN.
  · Regla madre "nada a medias": cada desacople nombra los dos lados y qué hace
    cada uno; cada anomalía dice de qué activo y en qué dirección apunta el flujo.

Funciones PURAS sobre estructuras → testeables sin BD. La presentación (nombres
legibles) vive en el digest, que renderiza estos resultados.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

# Fuente ÚNICA de verdad de los termómetros excluidos (definida en el digest y
# reutilizada por las alertas). Importarla aquí no crea ciclo: digest.py solo
# importa este módulo de forma diferida (dentro de send_daily_digest).
from app.alerts.digest import SENTIMENT_TICKERS

logger = logging.getLogger("marea.analysis.early_detection")

# ── Pares clásicos con sentido económico (configurable) ───────────────────────
# Se normalizan a orden alfabético al usarlos. Los que no estén en los datos
# (p. ej. bancos que el universo dinámico no haya incorporado aún) simplemente no
# producen correlación y degradan en silencio.
CLASSIC_PAIRS: list[tuple[str, str]] = [
    ("GC=F", "SI=F"),   # oro / plata (futuros)
    ("GLD", "SLV"),     # oro / plata (ETF)
    ("^GSPC", "^IXIC"),  # S&P 500 / Nasdaq
    ("SPY", "QQQ"),     # S&P 500 / Nasdaq (ETF)
    ("SOXX", "SMH"),    # semiconductores
    ("BTC", "ETH"),     # cripto majors
    ("GLD", "GDX"),     # oro / mineras de oro
    ("SLV", "SIL"),     # plata / mineras de plata
    ("ITA", "XAR"),     # defensa
    # Bancos entre sí (si el universo dinámico los incorpora)
    ("JPM", "BAC"), ("JPM", "WFC"), ("JPM", "C"),
    ("BAC", "WFC"), ("BAC", "C"), ("C", "WFC"),
]

# ── Umbrales de desacople (documentados; alineados con correlation.py) ─────────
# Ventana BASE (larga, estable) y RECIENTE (corta, sensible), en nº de barras.
BASE_WINDOW: int = 30
RECENT_WINDOW: int = 7
# Observaciones mínimas por par para calcular una correlación fiable en una ventana.
MIN_CORR_OBS: int = 4
# Un par se desacopla si su correlación BASE era alta (|base| ≥ esto)…
BASE_CORR_THRESHOLD: float = 0.7
# …y la correlación RECIENTE cae claramente respecto a la base (|base − recent| ≥ esto).
DECOUPLE_DROP_THRESHOLD: float = 0.5
# Auto-descubrimiento: además de los clásicos, se vigilan los pares que la matriz
# detecte como fuerte y establemente correlacionados (|base| ≥ esto).
DISCOVERY_CORR_THRESHOLD: float = 0.85

# ── Umbrales de volumen anómalo (documentados) ────────────────────────────────
# Volumen "muy por encima de lo normal" = al menos estas desviaciones típicas por
# encima de la media histórica del PROPIO activo.
VOLUME_ANOMALY_SIGMA: float = 2.5

# |score penalizado| por encima del cual la atención se etiqueta entrada/salida.
DIR_EPS: float = 0.10

# Días de histórico a cargar.
LOOKBACK_DAYS: int = 45


# ══════════════════════════════════════════════════════════════════════════════
# Resultados estructurados (el digest los renderiza con nombres legibles)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Decouple:
    ticker_a: str
    ticker_b: str
    base_corr: float            # correlación histórica (ventana base)
    recent_corr: float          # correlación reciente (ventana corta)
    score_a: float              # último flow score PENALIZADO de cada lado
    score_b: float
    source: str                 # 'classic' | 'discovered'


@dataclass
class VolumeAnomaly:
    ticker: str
    asset_class: Optional[str]
    sigma: float                # nº de desviaciones típicas por encima de lo normal
    direction: str              # 'inflow' | 'outflow' | 'neutral'
    score: float                # último flow score PENALIZADO (dirección de la atención)


@dataclass
class EarlyDetectionResult:
    decouples: list[Decouple] = field(default_factory=list)
    anomalies: list[VolumeAnomaly] = field(default_factory=list)
    corr_baseline_ready: bool = False   # ¿hay histórico para fiarse de las correlaciones?
    vol_baseline_ready: bool = False    # ¿hay histórico para fiarse de las medias de volumen?
    n_pairs_watched: int = 0
    errors: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# Correlaciones que se rompen (desacoples) — PURO
# ══════════════════════════════════════════════════════════════════════════════

def build_ticker_pivot(records: list[dict]) -> pd.DataFrame:
    """
    Pivot ts × ticker con el flow score (penalizado) de cada activo por día.
    `records`: dicts {ts, ticker, score, ...}. Las filas de termómetros se dejan
    pasar aquí; el filtrado de exclusión se hace en la detección.
    """
    rows = [
        {"ts": r["ts"], "ticker": r.get("ticker", ""), "score": r["score"]}
        for r in records
        if r.get("ticker") and r.get("score") is not None
    ]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.normalize()
    pivot = df.groupby(["ts", "ticker"])["score"].mean().unstack("ticker")
    pivot.sort_index(inplace=True)
    return pivot


def _corr_of(frame: pd.DataFrame, a: str, b: str) -> tuple[Optional[float], int]:
    """Correlación de Pearson del par (a,b) sobre las filas comunes (sin NaN) de `frame`."""
    sub = frame[[a, b]].dropna()
    n = len(sub)
    if n < MIN_CORR_OBS:
        return None, n
    val = sub[a].corr(sub[b])
    return (None if pd.isna(val) else float(round(val, 6))), n


def _base_recent_corr(pivot: pd.DataFrame, a: str, b: str) -> tuple[tuple[Optional[float], int], tuple[Optional[float], int]]:
    """
    Correlación BASE (histórico estable, PREVIO al periodo reciente) y RECIENTE
    (últimas RECENT_WINDOW barras). Clave: la base EXCLUYE la ventana reciente, así
    representa "cómo iban históricamente" y el desacople = base alta − reciente baja.
    Si la base incluyera el periodo reciente, una ruptura brusca se auto-anularía.
    """
    if a not in pivot.columns or b not in pivot.columns:
        return (None, 0), (None, 0)
    common = pivot[[a, b]].dropna()
    recent = common.iloc[-RECENT_WINDOW:]
    prior = common.iloc[:-RECENT_WINDOW]                      # todo lo anterior a lo reciente
    base = prior.iloc[-BASE_WINDOW:] if len(prior) >= BASE_WINDOW else prior
    return _corr_of(base, a, b), _corr_of(recent, a, b)


def _discover_stable_pairs(pivot: pd.DataFrame, min_obs: int) -> list[tuple[str, str]]:
    """
    Pares fuerte y establemente correlacionados en el HISTÓRICO PREVIO (|corr| ≥
    umbral), que pasan a vigilarse por si se desacoplan. Usa la misma ventana base
    (previa a lo reciente) que la detección, por coherencia.
    """
    prior = pivot.iloc[:-RECENT_WINDOW] if len(pivot) > RECENT_WINDOW else pivot.iloc[0:0]
    base = prior.iloc[-BASE_WINDOW:] if len(prior) >= BASE_WINDOW else prior
    if len(base) < min_obs:
        return []
    cm = base.corr(min_periods=min_obs)   # NaN si no hay suficiente solape → se descarta
    pairs: list[tuple[str, str]] = []
    cols = list(cm.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = cm.loc[a, b]
            if pd.notna(v) and abs(float(v)) >= DISCOVERY_CORR_THRESHOLD:
                pairs.append(tuple(sorted([a, b])))   # type: ignore[arg-type]
    return pairs


def detect_decouples(
    pivot: pd.DataFrame,
    latest_scores: dict[str, float],
    *,
    classic_pairs: list[tuple[str, str]] = CLASSIC_PAIRS,
    exclude: set[str] = SENTIMENT_TICKERS,
    min_obs: int,
) -> tuple[list[Decouple], bool, int]:
    """
    Detecta desacoples sobre los pares clásicos + los descubiertos como estables.

    Devuelve (decouples, baseline_ready, n_pairs_watched). baseline_ready=True si
    AL MENOS un par tuvo histórico suficiente (≥ min_obs) en la ventana base; si
    no, seguimos "estableciendo línea base" y no se emite ninguna señal.
    """
    if pivot is None or pivot.empty:
        return [], False, 0

    classic = {tuple(sorted(p)) for p in classic_pairs}
    discovered = set(_discover_stable_pairs(pivot, min_obs))
    watch = classic | discovered

    decouples: list[Decouple] = []
    baseline_ready = False
    n_watched = 0

    for pair in sorted(watch):
        a, b = pair
        if a in exclude or b in exclude:
            continue
        if a not in pivot.columns or b not in pivot.columns:
            continue
        n_watched += 1

        (base, n_base), (recent, _) = _base_recent_corr(pivot, a, b)
        if base is None or n_base < min_obs:
            continue                      # línea base de ESTE par aún no fiable
        baseline_ready = True             # al menos un par tiene base fiable
        if recent is None:
            continue

        if abs(base) >= BASE_CORR_THRESHOLD and abs(base - recent) >= DECOUPLE_DROP_THRESHOLD:
            decouples.append(Decouple(
                ticker_a=a, ticker_b=b,
                base_corr=round(base, 2), recent_corr=round(recent, 2),
                score_a=round(float(latest_scores.get(a, 0.0)), 3),
                score_b=round(float(latest_scores.get(b, 0.0)), 3),
                source="discovered" if pair in discovered and pair not in classic else "classic",
            ))

    # Orden estable: mayor caída de correlación primero.
    decouples.sort(key=lambda d: abs(d.base_corr - d.recent_corr), reverse=True)
    return decouples, baseline_ready, n_watched


# ══════════════════════════════════════════════════════════════════════════════
# Volumen anómalo — PURO
# ══════════════════════════════════════════════════════════════════════════════

def _direction(score: float) -> str:
    if score > DIR_EPS:
        return "inflow"
    if score < -DIR_EPS:
        return "outflow"
    return "neutral"


def detect_volume_anomalies(
    vol_history: dict[str, list[float]],
    asset_class_map: dict[str, Optional[str]],
    latest_scores: dict[str, float],
    *,
    exclude: set[str] = SENTIMENT_TICKERS,
    min_obs: int,
) -> tuple[list[VolumeAnomaly], bool]:
    """
    Marca anomalías de volumen vs la propia distribución histórica de cada activo.

    `vol_history`: {ticker: [volúmenes en orden temporal asc]} (último = actual).
    Devuelve (anomalies, baseline_ready). baseline_ready=True si algún activo tuvo
    ≥ min_obs observaciones de histórico (media/σ fiables).
    """
    anomalies: list[VolumeAnomaly] = []
    baseline_ready = False

    for ticker, vols in vol_history.items():
        if ticker in exclude:
            continue
        clean = [float(v) for v in (vols or []) if v is not None and v > 0]
        # Necesitamos histórico (min_obs) MÁS la observación actual.
        if len(clean) < min_obs + 1:
            continue
        baseline_ready = True

        current = clean[-1]
        history = clean[:-1]
        mean = float(np.mean(history))
        std = float(np.std(history))
        if std <= 0:
            continue

        z = (current - mean) / std
        if z >= VOLUME_ANOMALY_SIGMA:
            score = float(latest_scores.get(ticker, 0.0))
            anomalies.append(VolumeAnomaly(
                ticker=ticker,
                asset_class=asset_class_map.get(ticker),
                sigma=round(z, 2),
                direction=_direction(score),
                score=round(score, 3),
            ))

    anomalies.sort(key=lambda a: a.sigma, reverse=True)
    return anomalies, baseline_ready


# ══════════════════════════════════════════════════════════════════════════════
# Carga de BD + fachada de alto nivel (best-effort, nunca lanza)
# ══════════════════════════════════════════════════════════════════════════════

def _latest_scores(records: list[dict]) -> dict[str, float]:
    """Último flow score (penalizado) por ticker, según el ts más reciente."""
    latest_ts: dict[str, str] = {}
    out: dict[str, float] = {}
    for r in records:
        t = r.get("ticker")
        ts = r.get("ts")
        if not t or ts is None or r.get("score") is None:
            continue
        if t not in latest_ts or ts > latest_ts[t]:
            latest_ts[t] = ts
            out[t] = float(r["score"])
    return out


def _load_volume_history(db, lookback_days: int = LOOKBACK_DAYS) -> tuple[dict[str, list[float]], dict[str, Optional[str]]]:
    """
    Carga el histórico de volumen por ticker desde raw_snapshots (orden temporal).
    Devuelve ({ticker: [volúmenes asc]}, {ticker: asset_class}). [] ante error.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        resp = (
            db.table("raw_snapshots")
            .select("ts,volume,assets(ticker,asset_class)")
            .gte("ts", cutoff)
            .order("ts")
            .execute()
        )
        rows = resp.data or []
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer raw_snapshots para volumen anómalo: %s", e)
        return {}, {}

    vols: dict[str, list[float]] = {}
    classes: dict[str, Optional[str]] = {}
    for r in rows:
        a = r.get("assets") or {}
        t = a.get("ticker")
        if not t:
            continue
        v = r.get("volume")
        vols.setdefault(t, []).append(v)
        classes[t] = a.get("asset_class")
    return vols, classes


def evaluate_early_detection(
    db,
    *,
    corr_min_obs: int | None = None,
    vol_min_obs: int | None = None,
) -> EarlyDetectionResult:
    """
    Fachada: carga histórico, detecta desacoples y volúmenes anómalos. Nunca lanza
    (degradación elegante: ante error devuelve un resultado vacío con el error
    anotado, y el digest simplemente no muestra los bloques).
    """
    if corr_min_obs is None or vol_min_obs is None:
        from app.config import settings
        corr_min_obs = corr_min_obs if corr_min_obs is not None else settings.early_corr_min_obs
        vol_min_obs = vol_min_obs if vol_min_obs is not None else settings.early_volume_min_obs

    result = EarlyDetectionResult()

    # 1. Desacoples — reutiliza el cargador de la matriz de correlación (flow_scores 7d).
    try:
        from app.analysis.correlation import CorrelationBuilder

        records = CorrelationBuilder(db=db).load_scores(lookback_days=LOOKBACK_DAYS)
        pivot = build_ticker_pivot(records)
        latest = _latest_scores(records)
        result.decouples, result.corr_baseline_ready, result.n_pairs_watched = detect_decouples(
            pivot, latest, min_obs=corr_min_obs,
        )
        if not result.corr_baseline_ready:
            logger.info("Desacoples: estableciendo línea base (histórico de correlación insuficiente).")
    except Exception as e:  # noqa: BLE001
        logger.warning("Detección de desacoples no disponible: %s", e)
        result.errors.append(f"decouples: {e}")
        latest = {}

    # 2. Volumen anómalo — distribución histórica del propio activo (raw_snapshots).
    try:
        vol_history, classes = _load_volume_history(db)
        if not latest:
            latest = {}
        result.anomalies, result.vol_baseline_ready = detect_volume_anomalies(
            vol_history, classes, latest, min_obs=vol_min_obs,
        )
        if not result.vol_baseline_ready:
            logger.info("Volumen anómalo: estableciendo línea base (histórico de volumen insuficiente).")
    except Exception as e:  # noqa: BLE001
        logger.warning("Detección de volumen anómalo no disponible: %s", e)
        result.errors.append(f"volume: {e}")

    logger.info(
        "Detección temprana: %d desacoples (%d pares vigilados), %d anomalías de volumen "
        "(corr_base=%s, vol_base=%s)",
        len(result.decouples), result.n_pairs_watched, len(result.anomalies),
        result.corr_baseline_ready, result.vol_baseline_ready,
    )
    return result
