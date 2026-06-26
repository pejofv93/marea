"""
Capa de CREDIBILIDAD del flujo (Bloque 2).

El flow score sale casi siempre de UN proxy (volumen). El volumen solo no
distingue un flujo SANO de un FOGONAZO. Esta capa cruza varias señales para
juzgar si un flujo es creíble y devuelve un FACTOR [0..1] con el que el motor
penaliza el score:

    score_final = score_raw × credibility

Señales:
  1. VOLUMEN — la base del score (ya viene en score_raw).
  2. CONFIRMACIÓN DE PRECIO (día 1, sin histórico): ¿el precio se mueve en la
     dirección que sugiere el flujo? Volumen + precio acompañando = coherente
     (creíble). Volumen + precio plano = sospechoso (posible absorción). Volumen
     + precio EN CONTRA = flujo dudoso (posible distribución).
  3. PERSISTENCIA (AUTO-ACTIVADA, necesita histórico): ¿el flujo se sostiene
     varias barras o es un pico aislado? Solo influye cuando hay
     ≥ persist_min_obs observaciones; por debajo, degrada con elegancia (no
     influye y no se menciona).

DISTINTO de la confianza (cold start): 'confidence' (ok/low) mide si hay
SUFICIENTE HISTÓRICO; 'credibility' mide si ESTE flujo concreto es creíble. Son
ejes independientes y se guardan por separado.

Funciones PURAS sobre las filas de snapshots → testeables sin BD.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.scoring.zscore import series_from_snapshots

# ── Umbrales (documentados) ───────────────────────────────────────────────────
# Por debajo de este |score_raw| no hay flujo relevante que juzgar → sin etiqueta.
SCORE_EPS: float = 0.10
# |retorno de precio| por debajo de esto (0.2%) en la ventana → precio "plano".
PRICE_FLAT_EPS: float = 0.002

# Factores de confirmación de precio
PF_CONFIRM: float = 1.0    # precio acompaña al flujo
PF_FLAT: float = 0.6       # volumen sin que el precio confirme (posible absorción)
PF_CONTRA: float = 0.4     # precio en contra del flujo (posible distribución)
PF_NO_PRICE: float = 1.0   # no se puede evaluar el precio → sin penalización

# Persistencia
PERSIST_LOOKBACK: int = 3   # barras recientes para juzgar sostenido vs aislado
SPIKE_MULT: float = 1.15    # volumen > baseline×esto cuenta como "elevado"
PERSIST_FLOOR: float = 0.6  # factor mínimo de persistencia (pico totalmente aislado)

# Etiquetas (umbrales sobre la credibilidad final)
CONFIRM_THRESHOLD: float = 0.8
DOUBT_THRESHOLD: float = 0.5

LABEL_CONFIRMED = "confirmado"
LABEL_DOUBTFUL = "dudoso"
LABEL_SPIKE = "fogonazo"


@dataclass
class CredibilityResult:
    credibility: float                 # factor [0..1] aplicado al score
    label: str                         # 'confirmado' | 'dudoso' | 'fogonazo'
    reason: str                        # motivo corto en es (para auditoría/digest)
    price_factor: float                # componente de confirmación de precio
    persistence: Optional[float]       # componente de persistencia (None si inactiva)
    persistence_active: bool           # si la persistencia tenía histórico suficiente


# ══════════════════════════════════════════════════════════════════════════════
# Componentes
# ══════════════════════════════════════════════════════════════════════════════

def _price_factor(close, flow_dir: float, window: int) -> tuple[float, Optional[str]]:
    """
    Confirmación de precio en la misma ventana del flujo.
    Devuelve (factor, motivo). Si no hay precio suficiente, no penaliza.
    """
    clean = close.dropna() if close is not None else close
    if clean is None or len(clean) < 2:
        return PF_NO_PRICE, None

    start = float(clean.iloc[max(0, len(clean) - window)])
    end = float(clean.iloc[-1])
    if start == 0:
        return PF_NO_PRICE, None

    ret = (end - start) / abs(start)
    if abs(ret) < PRICE_FLAT_EPS:
        return PF_FLAT, "precio plano (sin confirmación)"

    price_dir = 1.0 if ret > 0 else -1.0
    if price_dir == flow_dir:
        return PF_CONFIRM, "precio confirma"
    return PF_CONTRA, "precio en contra del flujo"


def _persistence_factor(vol) -> tuple[float, Optional[str]]:
    """
    Persistencia del flujo: ¿el repunte de volumen se sostiene o es un pico
    aislado? Compara las últimas PERSIST_LOOKBACK barras con la mediana base.
    Devuelve (factor, motivo).
    """
    clean = vol.dropna() if vol is not None else vol
    if clean is None or len(clean) < 2:
        return 1.0, None

    n = len(clean)
    base_part = clean.iloc[:-PERSIST_LOOKBACK] if n > PERSIST_LOOKBACK else clean.iloc[:-1]
    baseline = float(base_part.median()) if len(base_part) else float(clean.median())
    if baseline <= 0:
        return 1.0, None

    recent = clean.iloc[-PERSIST_LOOKBACK:]
    elevated = sum(1 for v in recent if float(v) > baseline * SPIKE_MULT)
    ratio = elevated / len(recent)
    factor = float(np.clip(PERSIST_FLOOR + (1.0 - PERSIST_FLOOR) * ratio, 0.0, 1.0))

    if elevated <= 1:
        reason = "pico aislado (no sostenido)"
    elif ratio >= 0.99:
        reason = "flujo sostenido"
    else:
        reason = None
    return factor, reason


def _label_and_reason(
    credibility: float,
    price_reason: Optional[str],
    persist_reason: Optional[str],
) -> tuple[str, str]:
    reasons = [r for r in (price_reason, persist_reason) if r]
    joined = ", ".join(reasons)
    if credibility >= CONFIRM_THRESHOLD:
        return LABEL_CONFIRMED, joined or "volumen y precio coherentes"
    if credibility >= DOUBT_THRESHOLD:
        return LABEL_DOUBTFUL, joined or "confirmación parcial"
    return LABEL_SPIKE, joined or "señal de volumen no confirmada"


# ══════════════════════════════════════════════════════════════════════════════
# Evaluación principal
# ══════════════════════════════════════════════════════════════════════════════

def assess_credibility(
    rows: list[dict],
    score_raw: Optional[float],
    window: int,
    *,
    persist_min_obs: int,
    price_field: str = "close",
    volume_field: str = "volume",
) -> Optional[CredibilityResult]:
    """
    Evalúa la credibilidad del flujo de un asset en una ventana.

    Devuelve None si no hay flujo relevante que juzgar (score None o |score| muy
    pequeño): en ese caso el motor no penaliza ni etiqueta nada.
    """
    if score_raw is None or abs(score_raw) < SCORE_EPS:
        return None

    flow_dir = 1.0 if score_raw > 0 else -1.0

    # 1+2. Confirmación de precio (activa desde el día 1)
    close = series_from_snapshots(rows, price_field)
    price_factor, price_reason = _price_factor(close, flow_dir, window)

    # 3. Persistencia (auto-activada por histórico)
    vol = series_from_snapshots(rows, volume_field)
    if vol.empty:
        vol = series_from_snapshots(rows, "volume_24h")
    n_vol = int(vol.dropna().count())
    persistence_active = n_vol >= persist_min_obs
    if persistence_active:
        persistence, persist_reason = _persistence_factor(vol)
    else:
        persistence, persist_reason = None, None

    persist_component = persistence if persistence is not None else 1.0
    credibility = float(np.clip(price_factor * persist_component, 0.0, 1.0))

    label, reason = _label_and_reason(credibility, price_reason, persist_reason)
    return CredibilityResult(
        credibility=round(credibility, 4),
        label=label,
        reason=reason,
        price_factor=round(price_factor, 4),
        persistence=round(persistence, 4) if persistence is not None else None,
        persistence_active=persistence_active,
    )


def penalized_score(score_raw: Optional[float], cred: Optional[CredibilityResult]) -> Optional[float]:
    """score_final = score_raw × credibility (intacto si no hay credibilidad)."""
    if score_raw is None or cred is None:
        return score_raw
    return round(float(np.clip(score_raw * cred.credibility, -1.0, 1.0)), 6)
