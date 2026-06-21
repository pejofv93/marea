"""
Descubrimiento de exposición indirecta via LLM con búsqueda web.

Construye prompts que OBLIGAN al LLM a buscar en la web (nunca memoria).
Parsea la respuesta JSON estructurada extrayendo candidatos con sus URLs.

Dos tipos de objetivo:
  pre_ipo  — cotizados/ETFs con exposición a empresas privadas pre-IPO.
  crypto   — acciones/ETFs con exposición directa a una criptomoneda.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.exposure.llm_client import LLMResponse, query_with_web_search

logger = logging.getLogger("marea.exposure.discovery")

# ── Objetivos predeterminados ─────────────────────────────────────────────────
# Privadas de alto interés (se investigan sus participaciones en cotizados)
DEFAULT_PRE_IPO_TARGETS: list[str] = [
    "OpenAI", "Anthropic", "SpaceX", "Stripe", "Databricks",
]

# Criptos principales (se extienden con el universo dinámico de S3 si hay BD)
DEFAULT_CRYPTO_TARGETS: list[str] = ["BTC", "ETH"]


@dataclass
class RawCandidate:
    """Candidato crudo salido del LLM, AÚN SIN VERIFICAR."""
    source_entity: str     # empresa privada o cripto investigada
    exposed_ticker: str    # cotizado/ETF afectado
    exposure_type: str     # 'pre_ipo' | 'crypto'
    relationship: str      # descripción de la exposición
    sources: list[str]     # URLs recibidas (pueden ser inválidas o inventadas)
    llm_engine: str        # 'groq' | 'gemini'


# ── Construcción de prompts ───────────────────────────────────────────────────

def build_pre_ipo_prompt(entity: str) -> str:
    return f"""Search the web RIGHT NOW for verified, current information about publicly traded
companies or ETFs that have direct equity stakes or material indirect exposure to {entity}.

CRITICAL INSTRUCTIONS:
- Use web search. Do NOT answer from training data alone.
- For EACH company or ETF found, include the EXACT URL of a real source
  (SEC filing, official press release, credible news article).
- Only include relationships you can back with a specific, retrievable URL.
- If you cannot find verified information with real sources, return: []

Return ONLY a valid JSON array (no preamble, no markdown, no explanation):
[
  {{
    "exposed_ticker": "MSFT",
    "relationship": "equity stake via multi-billion investment in Azure partnership",
    "source_urls": ["https://actual-url-you-retrieved.com/article-or-filing"]
  }}
]

Search specifically for:
1. Which public companies have invested in or hold equity stakes in {entity}?
2. Which ETFs include companies with documented exposure to {entity}?
3. What is the approximate size or nature of the stake (from public filings or press)?

DO NOT invent relationships. DO NOT fabricate URLs. Return [] if not found."""


def build_crypto_prompt(crypto: str) -> str:
    return f"""Search the web RIGHT NOW for verified, current information about publicly traded
companies and ETFs whose business or balance sheet has significant exposure to {crypto}.

CRITICAL INSTRUCTIONS:
- Use web search. Do NOT answer from training data alone.
- For EACH company or ETF, include the EXACT URL of a real, retrievable source.
- Only include entities with documented, material {crypto} exposure.
- Return [] if no verified information is available.

Return ONLY a valid JSON array:
[
  {{
    "exposed_ticker": "MSTR",
    "relationship": "holds large BTC treasury position disclosed in SEC filings",
    "source_urls": ["https://actual-url-you-retrieved.com/filing-or-report"]
  }}
]

Search for:
1. Companies holding {crypto} on their balance sheet (treasury).
2. Mining companies (for BTC/ETH/PoW coins) or staking operators (PoS).
3. ETFs tracking {crypto} price directly.
4. Exchanges, custodians, or infrastructure companies with {crypto} as core revenue.

DO NOT fabricate. Return [] if no verified, sourced information found."""


# ── Parsing del response ──────────────────────────────────────────────────────

def parse_candidates(
    response: LLMResponse,
    source_entity: str,
    exposure_type: str,
) -> list[RawCandidate]:
    """
    Extrae candidatos de exposición del response del LLM.

    Combina las source_urls declaradas por el LLM dentro del JSON con las URLs
    globales extraídas de los metadatos de búsqueda del response (response.sources).

    IMPORTANTE: estos candidatos son CRUDOS — aún pueden tener URLs inválidas
    o inventadas. verify.py es quien filtra.
    """
    global_sources = list(response.sources)   # URLs del motor de búsqueda
    items = _extract_json_array(response.text)
    candidates: list[RawCandidate] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        ticker = str(item.get("exposed_ticker", "")).strip().upper()
        if not ticker:
            continue

        relationship = str(item.get("relationship", "")).strip()
        inline_urls = [
            str(u) for u in (item.get("source_urls") or [])
            if u and isinstance(u, (str, bytes))
        ]

        # Combina URLs del JSON (específicas del candidato) + URLs globales del search
        all_sources = list(dict.fromkeys(inline_urls + global_sources))

        candidates.append(RawCandidate(
            source_entity=source_entity,
            exposed_ticker=ticker,
            exposure_type=exposure_type,
            relationship=relationship,
            sources=all_sources,
            llm_engine=response.engine,
        ))

    if not candidates:
        logger.info("Sin candidatos parseables para %s [%s]", source_entity, exposure_type)

    return candidates


def _extract_json_array(text: str) -> list:
    """
    Extrae el primer array JSON válido del texto de respuesta del LLM.
    Tolera preamble y postamble de texto (el LLM añade a veces explicaciones).
    """
    # Greedy: captura desde el primer '[' hasta el último ']' — el array exterior.
    # Non-greedy capturaría el primer sub-array (ej. source_urls).
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        # Puede que el modelo devuelva un objeto JSON en vez de array
        logger.debug("No se encontró array JSON en la respuesta: %s…", text[:150])
        return []
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.warning("JSONDecodeError parseando respuesta LLM: %s — texto: %s…", e, text[:200])
        return []


# ── DiscoveryService ──────────────────────────────────────────────────────────

class DiscoveryService:
    """
    Orquesta el descubrimiento de exposición llamando al LLM con búsqueda web.
    El `llm_fn` es inyectable para tests (mock).
    """

    def __init__(self, llm_fn=None):
        self._query_fn = llm_fn or query_with_web_search

    def discover_pre_ipo(self, entity: str) -> list[RawCandidate]:
        """Descubre cotizados con exposición a la empresa privada `entity`."""
        try:
            response = self._query_fn(build_pre_ipo_prompt(entity))
            return parse_candidates(response, entity, "pre_ipo")
        except Exception as e:
            logger.error("Error descubriendo pre-IPO '%s': %s", entity, e)
            return []

    def discover_crypto(self, crypto: str) -> list[RawCandidate]:
        """Descubre cotizados con exposición a la cripto `crypto`."""
        try:
            response = self._query_fn(build_crypto_prompt(crypto))
            return parse_candidates(response, crypto, "crypto")
        except Exception as e:
            logger.error("Error descubriendo crypto '%s': %s", crypto, e)
            return []
