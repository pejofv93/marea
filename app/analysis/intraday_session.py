"""
Inteligencia INTRADÍA de sesión (Bloque 3).

Explota los distintos MOMENTOS de la sesión (apertura / media sesión / cierre)
capturados a lo largo del día para producir tres lecturas, todas con
AUTO-ACTIVACIÓN (degradan con elegancia si faltan momentos del día):

  1. CIERRE COMO JUEZ DEL DÍA (veredicto): para los flujos FUERTES de la
     apertura/media, dictamina al cierre si se han CONFIRMADO (siguen en la
     misma dirección), REVERTIDO (se han dado la vuelta) o AGOTADO (misma
     dirección pero perdieron fuelle).
  2. GIROS INTRADÍA: activos que CAMBIAN DE SIGNO entre dos momentos
     consecutivos (entraba → sale), solo si tuvieron movimiento FUERTE en al
     menos uno de los dos (no ruido de activos planos).
  3. VELOCIDAD DEL FLUJO (ritmo): si la entrada/salida se ACELERA o se FRENA
     entre dos momentos consecutivos (la "derivada" del flujo).

DE DÓNDE SALEN LOS MOMENTOS (sin migración nueva)
  La capa de comparación temporal (migración 011, digest_cycles) ya persiste,
  por ciclo, los flow scores que el parte REALMENTE usó, marcados con su
  `moment` ('apertura'|'media'|'cierre') y con clave única (ts, rail, moment).
  Para el carril intradía, un mismo día (ts = medianoche) acumula hasta tres
  filas — una por momento —, que SON los "momentos del día". Reutilizamos esa
  infraestructura en vez de duplicarla; la única ampliación es guardar también
  `credibility_label` dentro del JSONB de scores (esquema-libre, retrocompatible:
  las filas antiguas simplemente no lo traen). Por eso NO hace falta migración 014.

PRINCIPIOS HEREDADOS (Bloques 1 y 2, Sesión 12)
  · Score PENALIZADO por credibilidad como base (lo que ya guarda flow_scores_
    intraday.score), nunca el bruto: un veredicto/giro/ritmo sobre un FOGONAZO no
    es fiable, así que se OMITE cuando el flujo base es un fogonazo.
  · Afirmativo en lo observado (los giros y veredictos hablan de flujos que YA
    pasaron); nada de salto a precio futuro.
  · Termómetros de sentimiento (^VIX, CRYPTO_FNG) excluidos — no son vasijas de
    liquidez (misma fuente única de verdad que el digest y las alertas).
  · "Nada a medias": un giro nombra el activo y las DOS direcciones; un veredicto
    cierra con confirmado/revertido/agotado; el ritmo dice acelera o frena.

Funciones PURAS sobre listas de scores → testeables sin BD. La presentación
(nombres legibles, intensidad) vive en el digest, que renderiza estos resultados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Fuente ÚNICA de verdad de los termómetros excluidos (definida en el digest y
# reutilizada por las alertas). Importarla aquí no crea ciclo: digest.py no
# importa este módulo en el nivel superior (solo de forma diferida al enviar).
from app.alerts.digest import SENTIMENT_TICKERS

# ── Momentos del día ──────────────────────────────────────────────────────────
MOMENT_ORDER: tuple[str, ...] = ("apertura", "media", "cierre")
# Mínimo de momentos del día para que CUALQUIER función pueda compararse. Por
# debajo (cold start, o día con ciclos de cron saltados) todo degrada con
# elegancia: sin veredicto / sin giros / sin ritmo (nunca se inventa nada).
MIN_MOMENTS: int = 2

# ── Umbrales (documentados) ───────────────────────────────────────────────────
# |score penalizado| que cuenta como movimiento "fuerte" (alineado con el umbral
# de flujo intradía, settings.intraday_flow_threshold). Filtra el ruido: solo se
# dictamina veredicto sobre flujos fuertes de la apertura y solo se señalan giros
# de activos con movimiento fuerte en al menos uno de los dos momentos.
STRONG_MOVE: float = 0.6
# Por debajo de esto el flujo es prácticamente plano: ni cuenta como "ahora sale"
# en un giro, ni como flujo vivo en el ritmo, ni como flujo presente al cierre.
RELEVANT_EPS: float = 0.2
# Al cierre, misma dirección pero magnitud < apertura×esto → AGOTADO (perdió
# fuelle claramente). Por encima, sigue vivo → CONFIRMADO.
FADE_RATIO: float = 0.5
# Cambio mínimo de |score| entre dos momentos para hablar de acelera/frena; por
# debajo el ritmo es "estable" (y no se muestra: solo acelera/frena son señal).
VELOCITY_EPS: float = 0.10

# credibility_label (Bloque 2) que invalida un veredicto/giro/ritmo.
LABEL_SPIKE: str = "fogonazo"

# Veredictos del día
VERDICT_CONFIRMED: str = "confirmado"
VERDICT_REVERSED: str = "revertido"
VERDICT_EXHAUSTED: str = "agotado"


# ══════════════════════════════════════════════════════════════════════════════
# Resultados estructurados (el digest los renderiza con nombres legibles)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Verdict:
    ticker: str
    asset_class: Optional[str]
    verdict: str                # confirmado | revertido | agotado
    early_moment: str           # 'apertura' | 'media' (referencia del veredicto)
    early_score: float          # score penalizado del momento temprano
    close_score: float          # score penalizado al cierre (0.0 si ya no aparece)


@dataclass
class Giro:
    ticker: str
    asset_class: Optional[str]
    prev_moment: str
    now_moment: str
    prev_score: float           # dirección en el momento previo
    now_score: float            # dirección actual (signo opuesto al previo)


@dataclass
class Ritmo:
    ticker: str
    asset_class: Optional[str]
    direction: str              # 'entrada' | 'salida'
    trend: str                  # 'acelera' | 'frena'
    prev_score: float
    now_score: float


@dataclass
class SessionAnalysis:
    n_moments: int                              # momentos del día disponibles
    moments_present: list[str] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    giros: list[Giro] = field(default_factory=list)
    ritmo: list[Ritmo] = field(default_factory=list)
    # True solo si el parte es de CIERRE y hubo momentos suficientes para
    # dictaminar (distingue "no hay suficientes momentos" de "los hubo pero sin
    # flujos fuertes que juzgar"). El digest usa esto para el mensaje correcto.
    verdict_ready: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Clasificadores PUROS (testeables aisladamente)
# ══════════════════════════════════════════════════════════════════════════════

def _sign_flip(a: float, b: float) -> bool:
    """¿Cambió el signo del flujo entre a y b? (entra↔sale)."""
    return (a > 0 > b) or (a < 0 < b)


def classify_verdict(early_score: float, close_score: float) -> str:
    """
    Veredicto del cierre sobre un flujo fuerte de la apertura/media.
      · El flujo se ha apagado (cierre casi plano)        → AGOTADO.
      · El flujo se ha dado la vuelta (signo opuesto vivo) → REVERTIDO.
      · Misma dirección pero magnitud cae < apertura×FADE  → AGOTADO.
      · Misma dirección y mantiene/intensifica fuerza      → CONFIRMADO.
    """
    if abs(close_score) < RELEVANT_EPS:
        return VERDICT_EXHAUSTED
    if _sign_flip(early_score, close_score):
        return VERDICT_REVERSED
    if abs(close_score) < abs(early_score) * FADE_RATIO:
        return VERDICT_EXHAUSTED
    return VERDICT_CONFIRMED


def classify_velocity(prev_score: float, now_score: float) -> str:
    """'acelera' | 'frena' | 'estable' según cómo cambia |score| entre momentos."""
    d = abs(now_score) - abs(prev_score)
    if abs(d) < VELOCITY_EPS:
        return "estable"
    return "acelera" if d > 0 else "frena"


# ══════════════════════════════════════════════════════════════════════════════
# Detección sobre los índices de momentos
# ══════════════════════════════════════════════════════════════════════════════

def _score(asset: dict) -> float:
    try:
        return float(asset.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _index(assets: list[dict] | None, exclude: set[str]) -> dict[str, dict]:
    """
    Indexa los activos de un momento por ticker, excluyendo termómetros.
    Guarda el score PENALIZADO y la etiqueta de credibilidad (Bloque 2).
    """
    out: dict[str, dict] = {}
    for a in assets or []:
        t = a.get("ticker")
        if not t or t in exclude:
            continue
        out[t] = {
            "score":             _score(a),
            "asset_class":       a.get("asset_class"),
            "credibility_label": a.get("credibility_label"),
        }
    return out


def _strong_label(prev: dict, now: dict) -> Optional[str]:
    """Etiqueta de credibilidad del lado con mayor |score| (el que define el giro)."""
    return now["credibility_label"] if abs(now["score"]) >= abs(prev["score"]) else prev["credibility_label"]


def _detect_giros(prev_moment: str, prev_idx: dict, now_moment: str, now_idx: dict) -> list[Giro]:
    giros: list[Giro] = []
    for t, now in now_idx.items():
        prev = prev_idx.get(t)
        if prev is None:
            continue
        ps, ns = prev["score"], now["score"]
        if not _sign_flip(ps, ns):
            continue
        # La nueva dirección debe ser REAL ("ahora sale" no puede ser ~plano)…
        if abs(ns) < RELEVANT_EPS:
            continue
        # …y el activo tuvo movimiento FUERTE en al menos uno de los dos momentos.
        if max(abs(ps), abs(ns)) < STRONG_MOVE:
            continue
        # El flujo que define el giro no puede ser un fogonazo (poco creíble).
        if _strong_label(prev, now) == LABEL_SPIKE:
            continue
        giros.append(Giro(t, now["asset_class"], prev_moment, now_moment, ps, ns))
    return giros


def _detect_ritmo(prev_idx: dict, now_idx: dict) -> list[Ritmo]:
    ritmo: list[Ritmo] = []
    for t, now in now_idx.items():
        prev = prev_idx.get(t)
        if prev is None:
            continue
        ps, ns = prev["score"], now["score"]
        if _sign_flip(ps, ns):
            continue                       # un cambio de signo es un GIRO, no ritmo
        if abs(ns) < RELEVANT_EPS:
            continue                       # flujo actual sin fuerza → no hay ritmo que medir
        if now["credibility_label"] == LABEL_SPIKE:
            continue                       # fogonazo → no fiable
        vel = classify_velocity(ps, ns)
        if vel == "estable":
            continue                       # solo acelera/frena son señal
        direction = "entrada" if ns >= 0 else "salida"
        ritmo.append(Ritmo(t, now["asset_class"], direction, vel, ps, ns))
    return ritmo


def _detect_verdicts(day: list[tuple[str, dict]]) -> list[Verdict]:
    """
    Dictamina el veredicto del cierre. `day` viene ordenado por MOMENT_ORDER y su
    último elemento es el cierre. La referencia temprana es la apertura (preferida)
    o, si falta, la media sesión.
    """
    day_map = dict(day)
    close_idx = day_map["cierre"]
    early_moment = next((m for m in ("apertura", "media") if m in day_map and m != "cierre"), None)
    if early_moment is None:
        return []
    early_idx = day_map[early_moment]

    verdicts: list[Verdict] = []
    for t, early in early_idx.items():
        es = early["score"]
        if abs(es) < STRONG_MOVE:                         # solo flujos FUERTES de la apertura
            continue
        if early["credibility_label"] == LABEL_SPIKE:     # no se dictamina sobre fogonazos
            continue
        close = close_idx.get(t)
        cs = close["score"] if close else 0.0             # si ya no aparece → flujo apagado
        verdicts.append(Verdict(t, early["asset_class"], classify_verdict(es, cs), early_moment, es, cs))
    return verdicts


# ══════════════════════════════════════════════════════════════════════════════
# Fachada PURA
# ══════════════════════════════════════════════════════════════════════════════

def analyze_session(
    prior_moments: list[dict] | None,
    current_moment: str,
    current_assets: list[dict] | None,
    *,
    exclude: set[str] = SENTIMENT_TICKERS,
) -> SessionAnalysis:
    """
    Compone el análisis de sesión a partir de los momentos del día.

    `prior_moments`: momentos YA persistidos del día, cada uno
        {"moment": 'apertura'|'media'|'cierre', "assets": [ {ticker, score, …} ]}.
    `current_moment`: el momento del parte que se está componiendo.
    `current_assets`: los activos (score penalizado) del momento actual, aún sin
        persistir; sustituyen a cualquier fila previa del mismo momento.

    Degrada con elegancia: con < MIN_MOMENTS momentos del día devuelve un análisis
    vacío (verdict_ready=False) y el digest no muestra giros/ritmo y declara
    explícitamente la falta de momentos en el veredicto.
    """
    day_map: dict[str, dict] = {}
    for m in prior_moments or []:
        mom = m.get("moment")
        if mom in MOMENT_ORDER:
            day_map[mom] = _index(m.get("assets"), exclude)
    if current_moment in MOMENT_ORDER:
        day_map[current_moment] = _index(current_assets, exclude)   # el actual manda

    present = [m for m in MOMENT_ORDER if m in day_map]
    sa = SessionAnalysis(n_moments=len(present), moments_present=present)
    if len(present) < MIN_MOMENTS:
        return sa

    day = [(m, day_map[m]) for m in present]
    prev_moment, prev_idx = day[-2]
    now_moment, now_idx = day[-1]

    sa.giros = _detect_giros(prev_moment, prev_idx, now_moment, now_idx)
    sa.ritmo = _detect_ritmo(prev_idx, now_idx)

    # El veredicto es competencia del parte de CIERRE (el ciclo de la tarde USA).
    if current_moment == "cierre" and now_moment == "cierre":
        sa.verdicts = _detect_verdicts(day)
        sa.verdict_ready = True

    return sa
