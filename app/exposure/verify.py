"""
Verificación y clasificación de candidatos de exposición indirecta.

MÓDULO CRÍTICO — DEFENSA CONTRA ALUCINACIONES LLM.

El LLM se trata como SOSPECHOSO. Un candidato solo pasa si:
  1. Trae al menos UNA URL válida (http/https con dominio real).
  2. Esa URL clasifica en un nivel de confianza determinado.

Sin URL → DESCARTADO. Sin excepciones. Callar es más seguro que inventar.

Niveles de confianza (por dominio de la fuente):
  confirmado_oficial — SEC (sec.gov), IR corporativo (ir.*), newsroom oficial.
  rumor_prensa       — medio reputado (Reuters, Bloomberg, FT, WSJ…).
  especulacion       — cualquier otra URL válida; siempre marcado como hipótesis.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("marea.exposure.verify")

# ── Fuentes oficiales / regulatorias ─────────────────────────────────────────
# Dominios cuya autoridad es definitiva (SEC filings, EDGAR)
_OFFICIAL_DOMAINS: frozenset[str] = frozenset([
    "sec.gov",
    "edgar.sec.gov",
    "efts.sec.gov",
    "investor.gov",
])

# Subdominios que indican relaciones de inversores / comunicación corporativa
_OFFICIAL_SUBDOMAIN_RE = re.compile(
    r"^(ir|investor|investors|newsroom|press|media)\b"
)

# Patrones en el path que indican documentos corporativos oficiales
_OFFICIAL_PATH_RE = re.compile(
    r"/(press-release|investor-relation|ir|newsroom|annual-report"
    r"|sec-filing|10-k|10-q|8-k|proxy-statement)s?[/\-]",
    re.IGNORECASE,
)

# ── Medios de prensa reconocidos ──────────────────────────────────────────────
_REPUTABLE_MEDIA: frozenset[str] = frozenset([
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "apnews.com",
    "nytimes.com", "techcrunch.com", "theinformation.com", "axios.com",
    "cnbc.com", "marketwatch.com", "barrons.com", "forbes.com",
    "businessinsider.com", "economist.com", "wired.com", "arstechnica.com",
    "seekingalpha.com", "thestreet.com", "financialtimes.com",
    "bbc.com", "bbc.co.uk", "theguardian.com", "washingtonpost.com",
    "yahoo.com",          # Yahoo Finance tiene filings y noticias verificadas
    "finance.yahoo.com",
])

# ── Tipos de datos ────────────────────────────────────────────────────────────

@dataclass
class VerifiedCandidate:
    source_entity: str
    exposed_ticker: str
    exposure_type: str    # 'pre_ipo' | 'crypto'
    relationship: str
    confidence: str       # 'confirmado_oficial' | 'rumor_prensa' | 'especulacion'
    sources: list[str]    # invariante: nunca vacío
    llm_engine: str
    is_hypothesis: bool   # True si confidence != 'confirmado_oficial'


# ── Funciones puras (sin BD, testeables) ─────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Extrae el dominio raíz (sin 'www.') de una URL."""
    try:
        host = urlparse(url.strip()).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _is_valid_url(url: str) -> bool:
    """
    URL válida para este módulo:
    - Esquema http/https
    - Netloc no vacío y con al menos un punto (dominio real, no localhost)
    """
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.lower()
        if not host or "." not in host:
            return False
        if host.startswith("localhost") or host.startswith("127.") or host.startswith("0.0.0."):
            return False
        return True
    except Exception:
        return False


def _is_official_source(url: str) -> bool:
    """
    True si la URL proviene de una fuente oficial/regulatoria:
    - Dominios SEC/regulatorios
    - Subdominio ir./investor./newsroom. de cualquier empresa
    - Path que indica documento corporativo oficial (10-K, press-release…)
    """
    try:
        parsed = urlparse(url.strip())
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path

        # Dominios regulatorios absolutos
        if any(host == d or host.endswith("." + d) for d in _OFFICIAL_DOMAINS):
            return True

        # Subdominio de IR / investor / newsroom (ej. ir.microsoft.com)
        subdomain_part = host.split(".")[0] if "." in host else ""
        if _OFFICIAL_SUBDOMAIN_RE.match(subdomain_part):
            return True

        # Path con documentos corporativos oficiales
        if _OFFICIAL_PATH_RE.search(path):
            return True

        return False
    except Exception:
        return False


def _is_reputable_media(url: str) -> bool:
    """True si la URL proviene de un medio de comunicación reconocido."""
    domain = _extract_domain(url)
    for media in _REPUTABLE_MEDIA:
        if domain == media or domain.endswith("." + media):
            return True
    return False


def classify_confidence(sources: list[str]) -> str:
    """
    Clasifica el nivel de confianza basándose en los dominios de las fuentes.

    Prioridad: confirmado_oficial > rumor_prensa > especulacion.
    Evalúa TODAS las fuentes y devuelve el nivel MÁS ALTO encontrado.

    Raises ValueError si sources está vacío (invariante del módulo).
    """
    if not sources:
        raise ValueError("classify_confidence requiere al menos una URL — candidato debe descartarse antes")

    # Fuente oficial → nivel máximo, retorno inmediato
    for url in sources:
        if _is_official_source(url):
            return "confirmado_oficial"

    # Fuente de prensa reconocida
    for url in sources:
        if _is_reputable_media(url):
            return "rumor_prensa"

    # Cualquier otra URL válida → especulación
    return "especulacion"


def verify_candidate(
    source_entity: str,
    exposed_ticker: str,
    exposure_type: str,
    relationship: str,
    sources: list[str],
    llm_engine: str,
) -> Optional[VerifiedCandidate]:
    """
    Validación dura de un candidato de exposición.

    REGLA ABSOLUTA: sin URL válida → devuelve None (candidato DESCARTADO).
    Esta función es la última línea de defensa contra alucinaciones LLM.
    """
    # Filtra sólo URLs válidas (descarta strings vacíos, paths relativos, localhost, etc.)
    valid_urls = [u for u in (sources or []) if _is_valid_url(u)]

    if not valid_urls:
        logger.warning(
            "DESCARTADO %s→%s [%s]: sin URL válida. Fuentes recibidas: %s",
            source_entity, exposed_ticker, exposure_type, sources,
        )
        return None

    confidence = classify_confidence(valid_urls)
    is_hypothesis = confidence != "confirmado_oficial"

    if is_hypothesis:
        logger.info(
            "Candidato %s→%s marcado como HIPÓTESIS (confidence=%s, fuentes=%d)",
            source_entity, exposed_ticker, confidence, len(valid_urls),
        )

    return VerifiedCandidate(
        source_entity=source_entity,
        exposed_ticker=exposed_ticker,
        exposure_type=exposure_type,
        relationship=relationship,
        confidence=confidence,
        sources=valid_urls,
        llm_engine=llm_engine,
        is_hypothesis=is_hypothesis,
    )
