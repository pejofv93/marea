"""
Generador de narrativa LLM (Sesión 7).

DECISIONES DE DISEÑO:
  - Usa query_direct (NO query_with_web_search) → sin compound-beta, sin herramientas.
  - El snapshot ya contiene todos los datos; el LLM solo explica lo que recibe.
  - El prompt prohíbe explícitamente predicciones, consejos y causas externas.
  - Si confidence < 40 % o cold_start → el prompt ordena marcar la incertidumbre.
"""

from __future__ import annotations

import logging

from app.exposure.llm_client import LLMResponse, query_direct  # noqa: F401 (re-export para tests)

logger = logging.getLogger("marea.narrative.generator")

DISCLAIMER = "Interpretación automática de datos · no es consejo de inversión."

_SYSTEM_PROMPT = (
    "Eres un analista de flujos de liquidez intermercado.\n"
    "Tu tarea es explicar en lenguaje natural los datos de flujo que se te proporcionan.\n"
    "\n"
    "REGLAS ESTRICTAS que DEBES seguir:\n"
    "1. NO hagas predicciones de precio ('va a subir', 'caerá', 'alcanzará X').\n"
    "2. NO des consejos de inversión ('comprar', 'vender', 'acumular', 'all-in').\n"
    "3. NO afirmes causas externas que no estén en los datos "
    "('por la Fed', 'por la guerra en X', 'por la decisión del banco central').\n"
    "4. NO uses lenguaje de certeza ('definitivamente', 'seguro que', 'sin duda').\n"
    "5. USA siempre lenguaje observacional: "
    "'los datos muestran...', 'el patrón es consistente con...', 'sugiere...'.\n"
    "6. Si la confianza del régimen o de los scores es baja, "
    "DEBES indicarlo explícitamente en el texto.\n"
    "7. Si los datos son preliminares (cold_start o confianza < 40 %), "
    "DEBES escribir que el histórico es insuficiente para alta confianza.\n"
    "8. Escribe en español, máximo 200 palabras, como un párrafo continuo "
    "sin encabezados ni listas."
)

_UNCERTAINTY_INSTRUCTION = (
    "IMPORTANTE: La confianza de los datos actuales es baja. "
    "Debes indicar en el texto que las señales son débiles o contradictorias "
    "y que el histórico es insuficiente para alta confianza."
)

_COLD_START_INSTRUCTION = (
    "IMPORTANTE: Sistema en cold_start — histórico de datos insuficiente. "
    "Debes indicar explícitamente que los datos son preliminares "
    "y que la confianza es limitada."
)


def build_prompt(snapshot: dict) -> str:
    """
    Construye el prompt con el snapshot estructurado.
    Añade instrucción de incertidumbre cuando:
      - cold_start es True
      - confidence del régimen < 40 %
    """
    regime = snapshot.get("regime") or {}
    regime_name = regime.get("name", "desconocido")
    regime_conf = float(regime.get("confidence", 0.0))
    regime_signals = regime.get("signals") or []
    cold_start = snapshot.get("cold_start", False)

    lines = [
        f"RÉGIMEN ACTUAL: {regime_name} (confianza: {regime_conf:.0%})",
        f"Señales activas: {', '.join(regime_signals) if regime_signals else 'ninguna'}",
        "",
        "SCORES DE FLUJO — mayores inflows:",
    ]

    for item in snapshot.get("top_inflow") or []:
        lines.append(
            f"  {item['ticker']} ({item['asset_class']}): "
            f"score={item['score']:+.3f}, confianza={item['confidence']}"
        )

    lines.append("SCORES DE FLUJO — mayores outflows:")
    for item in snapshot.get("top_outflow") or []:
        lines.append(
            f"  {item['ticker']} ({item['asset_class']}): "
            f"score={item['score']:+.3f}, confianza={item['confidence']}"
        )

    lines += ["", "SCORES POR CLASE DE ACTIVO (promedio):"]
    for cls, score in (snapshot.get("class_scores") or {}).items():
        lines.append(f"  {cls}: {score:+.3f}")

    if snapshot.get("decouplings"):
        lines += ["", "DESACOPLES DETECTADOS:"]
        for d in snapshot["decouplings"]:
            lines.append(f"  {d['pair']}: corr={d['corr']:.2f} ({d['type']})")

    if snapshot.get("rotations"):
        lines += ["", "ROTACIONES SECTORIALES RECIENTES:"]
        for r in snapshot["rotations"]:
            lines.append(f"  {r['from']} → {r['to']}: fuerza={r['strength']:.2f}")

    # Instrucción de incertidumbre — siempre presente cuando la confianza lo requiere
    if cold_start:
        lines += ["", _COLD_START_INSTRUCTION]
    elif regime_conf < 0.4:
        lines += ["", _UNCERTAINTY_INSTRUCTION]

    lines += [
        "",
        "Con estos datos, escribe un párrafo explicando por qué se mueve la liquidez. "
        "Sigue las REGLAS ESTRICTAS del sistema.",
    ]

    return "\n".join(lines)


def generate_narrative(snapshot: dict) -> str:
    """
    Genera el texto narrativo usando Groq SIN búsqueda web.
    Lanza excepción si Groq falla — NarrativeEngine decide qué hacer.
    """
    prompt = build_prompt(snapshot)
    response = query_direct(prompt, system=_SYSTEM_PROMPT)
    text = response.text.strip()
    logger.debug("Narrativa generada: %d chars", len(text))
    return text
