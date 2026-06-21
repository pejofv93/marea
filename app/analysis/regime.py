"""
Clasificador de régimen de mercado — SOLO POR REGLAS, determinista.

Regímenes detectables:
  risk_on          — inflow a crypto + acciones, DXY bajando, VIX bajo.
  risk_off         — outflow de crypto + acciones, DXY subiendo, VIX alto.
  flight_to_safety — inflow a oro + bonos (+ posible dollar), outflow de crypto + acciones.
  sector_rotation  — rotación entre sectores sin señal macro clara (alimentado desde sector.py).
  neutral          — señales débiles o contradictorias, sin régimen claro.

DXY y VIX son CONTEXTO MODULADOR: aumentan la confianza cuando están alineados
con el régimen, pero NO pueden disparar un régimen por sí solos. Si todos los
flow scores son neutrales, el resultado es siempre 'neutral' independientemente
del valor de DXY/VIX.

Cada resultado incluye:
  regime     — nombre del régimen
  confidence — [0, 1]: qué proporción de señales esperadas se cumplieron
  signals    — lista de condiciones disparadas (explicabilidad)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("marea.analysis.regime")

# ── Umbrales ──────────────────────────────────────────────────────────────────
# Un score debe superar este umbral para contar como señal de flujo
FLOW_THRESHOLD: float = 0.15

# Confianza máxima alcanzable sólo con condiciones core (sin moduladores)
CORE_MAX_CONF: float = 0.70

# Bonus de confianza por cada modulador (DXY/VIX) alineado con el régimen
MODULATOR_BONUS: float = 0.15

# Confianza mínima del factor de datos cuando todos los scores son 'low'.
# Con structural=1.0 y factor=FLOOR → confidence_final=0.35 (zona roja <0.4).
# Refleja que las señales existen pero los datos subyacentes no son fiables.
DATA_CONFIDENCE_FLOOR: float = 0.35


# ── Tipos de datos ────────────────────────────────────────────────────────────

@dataclass
class ClassScores:
    """Scores agregados por clase de activo para el clasificador."""
    crypto: float   # inflow(+) / outflow(-) agregado de activos crypto
    equity: float   # inflow(+) / outflow(-) agregado de índices + acciones
    gold: float     # inflow(+) / outflow(-) oro
    silver: float   # inflow(+) / outflow(-) plata
    bonds: float    # inflow(+) / outflow(-) bonos (signo ya invertido en scoring)
    dxy: float      # modulador: + = dólar fuerte (risk-off context)
    vix: float      # modulador: + = VIX bajo/calmo (ya invertido en scoring)


@dataclass
class RegimeResult:
    regime: str
    confidence: float             # confianza final = structural × data_factor
    signals: list[str] = field(default_factory=list)
    structural_confidence: float = 0.0   # componente estructural (señales alineadas)
    data_confidence_factor: float = 1.0  # penalización por cold start en scores


# ── Lógica de clasificación ───────────────────────────────────────────────────

def classify_regime(
    scores: ClassScores,
    has_sector_rotation: bool = False,
    rotation_confidence: float = 0.0,
    data_confidence_factor: float = 1.0,
) -> RegimeResult:
    """
    Clasifica el régimen de mercado usando reglas deterministas.

    `has_sector_rotation` y `rotation_confidence` son inyectados por el engine
    cuando sector.py detecta rotación activa.

    `data_confidence_factor` ∈ [DATA_CONFIDENCE_FLOOR, 1.0]: penaliza la
    confianza estructural cuando los flow scores subyacentes tienen pocos datos
    (cold start). Ver compute_data_confidence_factor().

    Fórmula final:
      confidence = structural_confidence × data_confidence_factor

    Prioridad de regímenes (en caso de ambigüedad):
      flight_to_safety > risk_on | risk_off > sector_rotation > neutral
    """
    T = FLOW_THRESHOLD
    candidates: list[tuple[str, float, list[str]]] = []

    def _eval(
        regime: str,
        core_checks: list[tuple[bool, str]],
        mod_checks: list[tuple[bool, str]],
        min_core: int,
    ) -> None:
        core_fired = [label for cond, label in core_checks if cond]
        mod_fired = [label for cond, label in mod_checks if cond]

        if len(core_fired) < min_core:
            return

        core_conf = (len(core_fired) / len(core_checks)) * CORE_MAX_CONF
        bonus = len(mod_fired) * MODULATOR_BONUS
        confidence = min(1.0, core_conf + bonus)
        candidates.append((regime, round(confidence, 4), core_fired + mod_fired))

    # ── Risk-ON: inflow crypto + acciones, DXY bajando, VIX tranquilo ─────────
    _eval(
        "risk_on",
        core_checks=[
            (scores.crypto > T, "crypto_inflow"),
            (scores.equity > T, "equity_inflow"),
        ],
        mod_checks=[
            (scores.dxy < -T, "dxy_falling"),    # dólar debilitándose
            (scores.vix > T, "vix_calm"),         # VIX bajo (score ya invertido: + = calmo)
        ],
        min_core=1,
    )

    # ── Risk-OFF: outflow crypto + acciones, DXY subiendo, VIX alto ───────────
    _eval(
        "risk_off",
        core_checks=[
            (scores.crypto < -T, "crypto_outflow"),
            (scores.equity < -T, "equity_outflow"),
        ],
        mod_checks=[
            (scores.dxy > T, "dxy_rising"),       # dólar fortaleciéndose
            (scores.vix < -T, "vix_fearful"),     # VIX alto (score invertido: - = miedo)
        ],
        min_core=1,
    )

    # ── Flight-to-safety: inflow oro + bonos, outflow crypto + acciones ────────
    # Requiere OBLIGATORIAMENTE al menos un inflow de activo seguro (oro o bonos)
    # Y al menos un outflow de activo de riesgo (crypto o acciones).
    # Sin inflow a activos seguros es risk_off, no flight_to_safety.
    _core_fts = [
        (scores.gold > T, "gold_inflow"),
        (scores.bonds > T, "bonds_inflow"),
        (scores.crypto < -T, "crypto_outflow"),
        (scores.equity < -T, "equity_outflow"),
    ]
    _safe_inflow = scores.gold > T or scores.bonds > T
    _risky_outflow = scores.crypto < -T or scores.equity < -T
    if _safe_inflow and _risky_outflow:
        _eval(
            "flight_to_safety",
            core_checks=_core_fts,
            mod_checks=[(scores.dxy > T, "dxy_rising")],
            min_core=2,   # mínimo: 1 safe inflow + 1 risky outflow (ya garantizados)
        )

    if not candidates:
        # Sin señales de flujo suficientes → neutral, salvo si hay rotación sectorial
        if has_sector_rotation:
            # La rotación sectorial no depende de flow scores con confidence='ok'/'low',
            # por eso no se penaliza con data_confidence_factor.
            rot_conf = round(rotation_confidence, 4)
            return RegimeResult(
                "sector_rotation",
                rot_conf,
                ["sector_rotation_detected"],
                structural_confidence=rot_conf,
                data_confidence_factor=1.0,
            )
        return RegimeResult("neutral", 0.0, [], structural_confidence=0.0, data_confidence_factor=data_confidence_factor)

    # Flight-to-safety tiene prioridad sobre risk_on/risk_off (más específico)
    fs_candidates = [c for c in candidates if c[0] == "flight_to_safety"]
    if fs_candidates:
        best = max(fs_candidates, key=lambda x: x[1])
    else:
        best = max(candidates, key=lambda x: x[1])

    regime, structural_conf, signals = best

    # Aplicar penalización por calidad de datos:
    # confidence_final = structural_confidence × data_confidence_factor
    final_conf = round(min(1.0, structural_conf * data_confidence_factor), 4)

    return RegimeResult(
        regime,
        final_conf,
        signals,
        structural_confidence=round(structural_conf, 4),
        data_confidence_factor=round(data_confidence_factor, 4),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_data_confidence_factor(records: list[dict]) -> float:
    """
    Factor de penalización de confianza basado en la calidad de los flow scores.

    Solo considera activos que alimentan el clasificador de régimen (los que
    ticker_to_intermarket_class() no descarta). Los ETFs sectoriales se excluyen.

    Fórmula:
      ok_ratio = count(confidence=='ok') / count(all scored)  ∈ [0, 1]
      factor   = DATA_CONFIDENCE_FLOOR + (1 - DATA_CONFIDENCE_FLOOR) × ok_ratio
               ∈ [DATA_CONFIDENCE_FLOOR, 1.0]

    score=None se descarta; confidence ausente se trata como 'low'.
    Sin records válidos → DATA_CONFIDENCE_FLOOR (máxima precaución).
    """
    from app.analysis.correlation import ticker_to_intermarket_class

    # Tomar el score más reciente por activo (evita contar el mismo activo varias veces)
    latest: dict[str, dict] = {}
    for r in records:
        ticker = r.get("ticker", "")
        asset_class = r.get("asset_class", "")
        sector = r.get("sector")

        if ticker_to_intermarket_class(ticker, asset_class, sector) is None:
            continue  # sector ETF u otro activo no usado en el clasificador

        ts = r.get("ts", "")
        if ticker not in latest or ts > latest[ticker]["ts"]:
            latest[ticker] = {
                "ts": ts,
                "confidence": r.get("confidence") or "low",
                "score": r.get("score"),
            }

    scored = [v for v in latest.values() if v["score"] is not None]
    if not scored:
        return DATA_CONFIDENCE_FLOOR

    ok_count = sum(1 for v in scored if v["confidence"] == "ok")
    ok_ratio = ok_count / len(scored)
    return round(DATA_CONFIDENCE_FLOOR + (1.0 - DATA_CONFIDENCE_FLOOR) * ok_ratio, 4)


def class_scores_from_df_row(row: "pd.Series") -> ClassScores:  # type: ignore[name-defined]
    """Construye ClassScores desde una fila del DataFrame de scores por clase."""
    def _get(col: str) -> float:
        val = row.get(col, 0.0) if hasattr(row, "get") else getattr(row, col, 0.0)
        if val is None or (isinstance(val, float) and val != val):  # NaN check
            return 0.0
        return float(val)

    return ClassScores(
        crypto=_get("crypto"),
        equity=_get("equities"),
        gold=_get("gold"),
        silver=_get("silver"),
        bonds=_get("bonds"),
        dxy=_get("dollar"),
        vix=_get("vix"),
    )
