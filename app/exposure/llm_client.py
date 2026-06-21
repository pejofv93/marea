"""
Cliente LLM para descubrimiento de exposición indirecta.

Motor único: Groq compound-beta — web search ejecutada del lado servidor.
La respuesta incluye citas/search_results estructurados.

Si Groq falla, la excepción se propaga al llamador (DiscoveryService),
que la captura, registra el error y devuelve [] para ese ciclo.
No se persiste nada en ciclos fallidos; se reintenta en la siguiente ejecución.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("marea.exposure.llm")


@dataclass
class LLMResponse:
    text: str
    sources: list[str]   # URLs extraídas de la búsqueda web del response
    engine: str          # siempre 'groq'


def query_with_web_search(prompt: str) -> LLMResponse:
    """
    Ejecuta prompt con Groq compound-beta (búsqueda web del lado servidor).
    Lanza excepción si Groq falla — el llamador decide qué hacer (normalmente
    registrar el error y no persistir nada en ese ciclo).
    """
    return _query_groq(prompt)


def _query_groq(prompt: str) -> LLMResponse:
    """
    Groq compound-beta: el servidor ejecuta la búsqueda web internamente.
    Extrae URLs de: metadatos x_groq.executed_tools, tool_calls y texto.
    """
    from groq import Groq
    from app.config import settings

    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY no configurado")

    client = Groq(api_key=settings.groq_api_key)
    response = client.chat.completions.create(
        model="compound-beta",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,   # máximo determinismo para extracción estructurada
    )

    content = response.choices[0].message.content or ""
    sources = _extract_sources_groq(response, content)

    logger.debug("Groq: %d chars, %d fuentes extraídas", len(content), len(sources))
    return LLMResponse(text=content, sources=sources, engine="groq")


# ── Extracción de fuentes ─────────────────────────────────────────────────────

def _extract_sources_groq(response, content: str) -> list[str]:
    """
    Extrae URLs del response Groq compound-beta.
    Prioridad: metadatos x_groq → tool_calls → URLs en el texto.
    """
    sources: list[str] = []

    # 1. Metadatos x_groq (executed_tools de la búsqueda servidor)
    x_groq = getattr(response, "x_groq", None)
    if x_groq:
        for tool in getattr(x_groq, "executed_tools", None) or []:
            result = getattr(tool, "result", None)
            if result:
                sources.extend(_urls_from_text(str(result)))

    # 2. tool_calls explícitos en el mensaje
    tool_calls = getattr(response.choices[0].message, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if fn:
            sources.extend(_urls_from_text(getattr(fn, "arguments", "") or ""))
        sources.extend(_urls_from_text(getattr(tc, "result", "") or ""))

    # 3. URLs citadas explícitamente en el texto de respuesta
    sources.extend(_urls_from_text(content))

    return list(dict.fromkeys(sources))   # dedup, preserva orden


# ── API sin búsqueda web (narrativa S7) ──────────────────────────────────────

# Modelo estándar para llamadas directas — deliberadamente distinto de compound-beta
# para garantizar que no se activa ninguna herramienta de búsqueda servidor-lado.
_GROQ_DIRECT_MODEL = "llama-3.3-70b-versatile"


def query_direct(prompt: str, system: str = "") -> LLMResponse:
    """
    Llama a Groq con modelo estándar (NO compound-beta) y sin herramientas.
    Usar cuando el contexto ya viene estructurado internamente y no se necesita
    búsqueda web — garantiza narrativa cerrada sin alucinaciones de causas externas.
    Lanza excepción si Groq falla — el llamador decide qué hacer.
    """
    return _query_groq_direct(prompt, system)


def _query_groq_direct(prompt: str, system: str) -> LLMResponse:
    """Groq con modelo estándar — sin compound-beta, sin tools, sin web search."""
    from groq import Groq
    from app.config import settings

    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY no configurado")

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    client = Groq(api_key=settings.groq_api_key)
    response = client.chat.completions.create(
        model=_GROQ_DIRECT_MODEL,
        messages=messages,
        temperature=0.3,
    )

    content = response.choices[0].message.content or ""
    logger.debug("Groq direct: %d chars", len(content))
    return LLMResponse(text=content, sources=[], engine="groq")


# ── Extracción de fuentes ─────────────────────────────────────────────────────

def _urls_from_text(text: str) -> list[str]:
    """Extrae URLs HTTP/HTTPS del texto usando regex."""
    pattern = r"https?://[^\s\)\]\"\'\,\;\<\>\|\\]+"
    return re.findall(pattern, text or "")
