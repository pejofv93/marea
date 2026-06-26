"""
Calendario macro — el "por qué" del día (Bloque 5).

Avisa de los DATOS ECONÓMICOS de alto impacto del día para que el usuario
entienda POR QUÉ puede haber movimiento en la liquidez (una decisión de tipos,
un dato de inflación, el empleo…). Es CONTEXTO informativo, NUNCA una predicción:
dice qué evento hay y a qué hora, y que "suele traer volatilidad" (condicional y
genérico); jamás afirma dirección de precio.

FUENTE (Paso 0 — verificado en vivo, junio 2026): CALENDARIO ESTÁTICO CURADO a
partir de fuentes OFICIALES, consultado al vuelo (sin API, sin coste, sin clave,
sin red en runtime → no se puede caer). Decisión tomada con el usuario tras
comparar FRED (oficial pero sin hora/importancia/FOMC/BCE) y FMP (esquema
completo pero requiere clave y su tier gratuito es de estabilidad incierta).
Fuentes de las fechas 2026:
  · FOMC (Reserva Federal): federalreserve.gov/monetarypolicy/fomccalendars.htm
  · BCE (Consejo de Gobierno): ecb.europa.eu/press/calendars
  · CPI / Empleo / PIB / PCE (EE.UU.): OMB "Schedule of Release Dates for
    Principal Federal Economic Indicators CY2026" (statspolicy.gov / whitehouse.gov)
    + BLS (bls.gov/schedule) + BEA.

LÍMITE CONOCIDO (documentado, no es un fallo): al ser una tabla curada, hay que
REFRESCARLA UNA VEZ AL AÑO (añadir las fechas del año siguiente). A diferencia de
otros bloques NO necesita acumular histórico (un calendario es información de
futuro inmediato): funciona desde el día 1. Si la tabla se queda sin fechas
futuras, el bloque simplemente deja de aparecer y se registra un aviso en el log;
nunca rompe el parte.

DEGRADACIÓN ELEGANTE (best-effort): cualquier problema (tz no disponible, tabla
vacía, hoy sin eventos) → lista vacía y el bloque no aparece. Nunca lanza.

HORA: las fechas/horas se guardan en su zona ORIGEN (ET para EE.UU., CET para el
BCE) y se convierten a HORA DE MADRID con zoneinfo, que maneja el horario de
verano correctamente — incluidas las ~2 semanas al año en que los cambios de hora
de EE.UU. y la UE no coinciden (p. ej. el IPC del 11-mar sale a las 13:30 Madrid,
no 14:30). Por eso NO se hardcodea el desfase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("marea.analysis.macro_calendar")

# Zonas origen de cada evento y zona de presentación (el usuario es de España).
TZ_US = "America/New_York"   # publicaciones de EE.UU. (8:30 ET, FOMC 14:00 ET)
TZ_EZ = "Europe/Berlin"      # BCE (14:15 CET, Fráncfort) — mismo huso que Madrid
DISPLAY_TZ = "Europe/Madrid"

# Etiqueta legible (es) por tipo de evento; todos son de ALTO IMPACTO.
KIND_LABEL = {
    "fomc": "decisión de tipos de la Fed",
    "ecb":  "decisión de tipos del BCE",
    "cpi":  "IPC de EE.UU. (inflación)",
    "nfp":  "empleo de EE.UU. (nóminas no agrícolas)",
    "pce":  "PCE de EE.UU. (inflación favorita de la Fed)",
    "gdp":  "PIB de EE.UU. (primer avance)",
}
# Conjunto de tipos admitidos (alto impacto USA/eurozona). Cualquier otro se ignora.
HIGH_IMPACT_KINDS = frozenset(KIND_LABEL)

# ── Tabla curada 2026 (REFRESCAR ANUALMENTE) ──────────────────────────────────
# (fecha ISO, hora local en zona ORIGEN, tz origen, tipo). Solo eventos de PRIMER
# ORDEN para EE.UU. y la eurozona. Las revisiones (2.ª/3.ª estimación del PIB) y
# los datos menores se OMITEN a propósito (filtrar el ruido).
MACRO_EVENTS: list[tuple[str, str, str, str]] = [
    # Fed — FOMC (día de la decisión, comunicado 14:00 ET)
    ("2026-01-28", "14:00", TZ_US, "fomc"),
    ("2026-03-18", "14:00", TZ_US, "fomc"),
    ("2026-04-29", "14:00", TZ_US, "fomc"),
    ("2026-06-17", "14:00", TZ_US, "fomc"),
    ("2026-07-29", "14:00", TZ_US, "fomc"),
    ("2026-09-16", "14:00", TZ_US, "fomc"),
    ("2026-10-28", "14:00", TZ_US, "fomc"),
    ("2026-12-09", "14:00", TZ_US, "fomc"),
    # BCE — Consejo de Gobierno de política monetaria (comunicado 14:15 CET)
    ("2026-03-19", "14:15", TZ_EZ, "ecb"),
    ("2026-04-30", "14:15", TZ_EZ, "ecb"),
    ("2026-06-11", "14:15", TZ_EZ, "ecb"),
    ("2026-07-23", "14:15", TZ_EZ, "ecb"),
    ("2026-09-10", "14:15", TZ_EZ, "ecb"),
    ("2026-10-29", "14:15", TZ_EZ, "ecb"),
    ("2026-12-17", "14:15", TZ_EZ, "ecb"),
    # EE.UU. — IPC / CPI (8:30 ET)
    ("2026-01-13", "08:30", TZ_US, "cpi"), ("2026-02-11", "08:30", TZ_US, "cpi"),
    ("2026-03-11", "08:30", TZ_US, "cpi"), ("2026-04-10", "08:30", TZ_US, "cpi"),
    ("2026-05-12", "08:30", TZ_US, "cpi"), ("2026-06-10", "08:30", TZ_US, "cpi"),
    ("2026-07-14", "08:30", TZ_US, "cpi"), ("2026-08-12", "08:30", TZ_US, "cpi"),
    ("2026-09-11", "08:30", TZ_US, "cpi"), ("2026-10-14", "08:30", TZ_US, "cpi"),
    ("2026-11-10", "08:30", TZ_US, "cpi"), ("2026-12-10", "08:30", TZ_US, "cpi"),
    # EE.UU. — Empleo / Employment Situation / NFP (8:30 ET)
    ("2026-01-09", "08:30", TZ_US, "nfp"), ("2026-02-06", "08:30", TZ_US, "nfp"),
    ("2026-03-06", "08:30", TZ_US, "nfp"), ("2026-04-03", "08:30", TZ_US, "nfp"),
    ("2026-05-08", "08:30", TZ_US, "nfp"), ("2026-06-05", "08:30", TZ_US, "nfp"),
    ("2026-07-02", "08:30", TZ_US, "nfp"), ("2026-08-07", "08:30", TZ_US, "nfp"),
    ("2026-09-04", "08:30", TZ_US, "nfp"), ("2026-10-02", "08:30", TZ_US, "nfp"),
    ("2026-11-06", "08:30", TZ_US, "nfp"), ("2026-12-04", "08:30", TZ_US, "nfp"),
    # EE.UU. — PCE / Personal Income & Outlays (8:30 ET) — inflación favorita de la Fed
    ("2026-01-29", "08:30", TZ_US, "pce"), ("2026-02-26", "08:30", TZ_US, "pce"),
    ("2026-03-27", "08:30", TZ_US, "pce"), ("2026-04-30", "08:30", TZ_US, "pce"),
    ("2026-05-28", "08:30", TZ_US, "pce"), ("2026-06-25", "08:30", TZ_US, "pce"),
    ("2026-07-30", "08:30", TZ_US, "pce"), ("2026-08-26", "08:30", TZ_US, "pce"),
    ("2026-09-30", "08:30", TZ_US, "pce"), ("2026-10-29", "08:30", TZ_US, "pce"),
    ("2026-11-25", "08:30", TZ_US, "pce"), ("2026-12-23", "08:30", TZ_US, "pce"),
    # EE.UU. — PIB / GDP (8:30 ET) — solo el PRIMER AVANCE de cada trimestre (alto impacto)
    ("2026-01-29", "08:30", TZ_US, "gdp"),   # 4T'25 avance
    ("2026-04-30", "08:30", TZ_US, "gdp"),   # 1T'26 avance
    ("2026-07-30", "08:30", TZ_US, "gdp"),   # 2T'26 avance
    ("2026-10-29", "08:30", TZ_US, "gdp"),   # 3T'26 avance
]


@dataclass
class MacroEvent:
    time_madrid: str    # hora de Madrid "HH:MM" (DST resuelto)
    kind: str           # fomc | ecb | cpi | nfp | pce | gdp
    label: str          # etiqueta legible (es)
    region: str         # 'US' | 'EZ'
    when_utc: datetime  # instante en UTC (para ordenar)


def _region(kind: str) -> str:
    return "EZ" if kind == "ecb" else "US"


# ══════════════════════════════════════════════════════════════════════════════
# Consulta PURA (testeable; `now` y `table` inyectables)
# ══════════════════════════════════════════════════════════════════════════════

def events_on(
    target_madrid_date,
    table: list[tuple[str, str, str, str]] = MACRO_EVENTS,
) -> list[MacroEvent]:
    """
    Eventos de alto impacto cuyo instante, en HORA DE MADRID, cae en
    `target_madrid_date` (un date). Ordenados por hora. Filtra tipos no admitidos.
    """
    from zoneinfo import ZoneInfo

    disp = ZoneInfo(DISPLAY_TZ)
    out: list[MacroEvent] = []
    for d, hhmm, tz, kind in table:
        if kind not in HIGH_IMPACT_KINDS:
            continue
        local = datetime.fromisoformat(f"{d}T{hhmm}").replace(tzinfo=ZoneInfo(tz))
        in_madrid = local.astimezone(disp)
        if in_madrid.date() == target_madrid_date:
            out.append(MacroEvent(
                time_madrid=in_madrid.strftime("%H:%M"),
                kind=kind,
                label=KIND_LABEL[kind],
                region=_region(kind),
                when_utc=local.astimezone(timezone.utc),
            ))
    out.sort(key=lambda e: e.when_utc)
    return out


def _last_covered_date(table: list[tuple[str, str, str, str]]) -> Optional[str]:
    return max((d for d, *_ in table), default=None)


# ══════════════════════════════════════════════════════════════════════════════
# Fachada best-effort (nunca lanza)
# ══════════════════════════════════════════════════════════════════════════════

def todays_macro_events(now: Optional[datetime] = None) -> list[MacroEvent]:
    """
    Eventos macro de HOY (en hora de Madrid). Best-effort: ante cualquier problema
    (tz no disponible, etc.) devuelve []. Si la tabla se ha quedado sin fechas
    futuras (necesita refresco anual), lo registra en el log pero no rompe nada.
    """
    try:
        from zoneinfo import ZoneInfo

        disp = ZoneInfo(DISPLAY_TZ)
        now = now or datetime.now(disp)
        today = now.astimezone(disp).date()

        last = _last_covered_date(MACRO_EVENTS)
        if last is not None and today.isoformat() > last:
            logger.warning(
                "Calendario macro desactualizado: hoy (%s) supera la última fecha curada (%s). "
                "Refresca la tabla MACRO_EVENTS con el año siguiente.", today, last,
            )

        return events_on(today)
    except Exception as e:  # noqa: BLE001 — el calendario nunca debe romper el parte
        logger.warning("Calendario macro no disponible: %s", e)
        return []
