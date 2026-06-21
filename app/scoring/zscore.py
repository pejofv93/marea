"""
Utilidades matemáticas para flow scores.

Rolling z-score: (x - mean_N) / std_N, clipeado a [-1, +1].
n_obs = número de observaciones válidas (no-NaN) en la ventana.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

MIN_OBS_DEFAULT: int = 10
WINDOW_SHORT: int = 7
WINDOW_LONG: int = 30


@dataclass
class ZResult:
    zscore: Optional[float]     # sin clipear — para debug
    score: Optional[float]      # clipeado a [-1, +1]
    n_obs: int
    confidence: str             # 'ok' | 'low'


def rolling_zscore(
    series: pd.Series,
    window: int,
    min_obs: int = MIN_OBS_DEFAULT,
) -> ZResult:
    """
    Calcula el z-score del último valor de `series` usando una ventana móvil.

    - series: Serie temporal con índice DatetimeIndex, valores float.
              Se esperan observaciones diarias; el último valor es el «hoy».
    - window: Tamaño de la ventana en días.
    - min_obs: Mínimo de observaciones no-NaN para confidence='ok'.

    Devuelve ZResult con zscore sin clipear + score en [-1,+1] + metadatos.
    """
    clean = series.dropna()
    n_obs = int(clean.iloc[-window:].count()) if len(clean) >= 1 else 0

    if n_obs < 2:
        return ZResult(zscore=None, score=None, n_obs=n_obs, confidence="low")

    window_vals = clean.iloc[-window:]
    mean = float(window_vals.mean())
    std = float(window_vals.std(ddof=1))

    if std == 0.0 or np.isnan(std):
        raw = 0.0
    else:
        raw = (float(clean.iloc[-1]) - mean) / std

    clipped = float(np.clip(raw, -1.0, 1.0))
    confidence = "ok" if n_obs >= min_obs else "low"
    return ZResult(zscore=raw, score=clipped, n_obs=n_obs, confidence=confidence)


def series_from_snapshots(rows: list[dict], field: str = "close") -> pd.Series:
    """
    Convierte lista de filas raw_snapshots en pd.Series diaria ordenada.

    field puede ser 'close', 'volume', o una clave dentro de 'extra'.
    Si field no está en la fila raíz, busca en row['extra'].
    """
    records: list[tuple] = []
    for row in rows:
        val = row.get(field)
        if val is None and isinstance(row.get("extra"), dict):
            val = row["extra"].get(field)
        if val is not None:
            try:
                records.append((pd.Timestamp(row["ts"]), float(val)))
            except (TypeError, ValueError):
                continue

    if not records:
        return pd.Series(dtype=float)

    idx, vals = zip(*records)
    s = pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()
    # Colapsa duplicados de ts tomando el último
    return s[~s.index.duplicated(keep="last")]


def sign_from_price_direction(series: pd.Series, window: int) -> float:
    """
    Devuelve +1 si el precio subió en la ventana, -1 si bajó, 0 si plano.
    Usado para matizar el signo del z-score de volumen.
    """
    clean = series.dropna()
    if len(clean) < 2:
        return 0.0
    start = float(clean.iloc[max(0, len(clean) - window)])
    end = float(clean.iloc[-1])
    if end > start:
        return 1.0
    if end < start:
        return -1.0
    return 0.0
