"""
Ciclo INTRADÍA de MAREA — punto de entrada para GitHub Actions.

Ejecuta, EN ORDEN, la cadena intradía reutilizando los engines existentes
(sin pasar por HTTP):

  1. ingesta intradía .......... IntradayRunner       (yfinance barras + crypto re-lectura + FNG)
  2. scores intradía ........... IntradayScoreEngine  (flow_scores_intraday)
  3. análisis intradía ......... IntradayAnalysisEngine (movimientos en curso)
  4. evaluación de alertas ..... AlertEngine          (→ Telegram lo que proceda)

El motor de alertas respeta TODO lo ya construido: anti-duplicado por cambio
de estado, histéresis y el umbral de confianza mínimo (MIN_ALERT_CONFIDENCE).
En cold start / baja confianza es NORMAL que no se envíe nada.

Uso:
    python -m scripts.run_intraday_cycle
    python scripts/run_intraday_cycle.py     # también funciona (bootstrap de path abajo)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Permite ejecutar el archivo directamente (`python scripts/run_intraday_cycle.py`)
# además de como módulo (`python -m scripts.run_intraday_cycle`): asegura que la
# raíz del repo esté en sys.path para poder importar `app` y `scripts`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._common import run_cycle  # noqa: E402

logger = logging.getLogger("marea.cycle.intraday")

# Ventana de la GUARDA anti-duplicado. Los GRUPOS de cron de intraday.yml ponen
# varios disparos juntos (p. ej. 13:30, 13:45 y 14:00 UTC) como red de seguridad
# ante los saltos del scheduler de GitHub Actions. Si GitHub dispara más de uno
# del mismo grupo, solo queremos PROCESAR uno: si ya se ejecutó un ciclo en los
# últimos GUARD_WINDOW_MINUTES minutos, los siguientes se omiten limpiamente.
# 30 min > separación máxima dentro de un grupo (~30 min) y << hueco entre
# grupos (~2 h), así que absorbe los duplicados sin tragarse ciclos legítimos.
GUARD_WINDOW_MINUTES = 30


def build_steps(db=None):
    """
    Construye la cadena de pasos del ciclo intradía.

    Cada paso es ``(nombre, callable)`` donde el callable instancia el engine
    correspondiente y llama a su ``run_sync()`` (que ya captura errores por
    fuente/paso y devuelve un dict con ``ok``/``errors``).

    ``db`` se puede inyectar en tests; en producción cada engine abre su propia
    conexión a Supabase si es None.
    """
    from app.analysis.intraday import IntradayAnalysisEngine
    from app.alerts.engine import AlertEngine
    from app.config import settings
    from app.ingest.intraday_runner import IntradayRunner
    from app.scoring.intraday_engine import IntradayScoreEngine

    # Estado compartido entre pasos: el análisis intradía guarda aquí su
    # resultado (movimientos en curso) para que el resumen final lo use sin
    # recalcularlo.
    shared: dict = {}

    def _analisis():
        res = IntradayAnalysisEngine(db=db).run_sync()
        shared["intraday_analysis"] = res
        return res

    def _resumen():
        # Paso final: resumen-señal de vida en Telegram (SIEMPRE, haya o no alertas).
        from app.alerts.digest import send_intraday_digest
        return send_intraday_digest(db=db, analysis=shared.get("intraday_analysis"))

    return [
        ("ingesta_intradia",  lambda: IntradayRunner(db=db).run_sync()),
        ("scores_intradia",   lambda: IntradayScoreEngine(db=db, min_obs=settings.score_min_obs).run_sync()),
        ("analisis_intradia", _analisis),
        ("alertas",           lambda: AlertEngine(db=db).run_sync()),
        ("resumen_telegram",  _resumen),
    ]


def _parse_ts(raw: str) -> datetime | None:
    """Parsea un timestamp ISO de la BD a datetime *aware* en UTC. None si no puede."""
    if not raw:
        return None
    try:
        # Supabase/Postgres devuelve a veces 'Z' en vez de '+00:00'.
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def recently_ran(db, within_minutes: int = GUARD_WINDOW_MINUTES, now: datetime | None = None) -> bool:
    """
    GUARDA anti-duplicado: ¿se ejecutó YA un ciclo intradía hace poco?

    Mira el timestamp más reciente de ``raw_snapshots_intraday`` (cada ciclo
    re-escribe ahí precio/volumen/FNG con la hora REAL del momento). Si ese ts
    cae dentro de la ventana ``within_minutes``, asumimos que otro cron del mismo
    grupo ya procesó el ciclo y devolvemos True para omitir este.

    Ante CUALQUIER problema (sin datos, ts ilegible, error de lectura de BD)
    devuelve False: preferimos procesar de más a perder un ciclo por un fallo
    transitorio. La red de seguridad nunca debe convertirse en un agujero.
    """
    now = now or datetime.now(timezone.utc)
    try:
        resp = (
            db.table("raw_snapshots_intraday")
            .select("ts")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:  # noqa: BLE001 — si no podemos comprobar, procedemos
        logger.warning("Guarda anti-duplicado: no se pudo leer raw_snapshots_intraday (%s); se procede.", e)
        return False

    if not rows:
        return False
    last = _parse_ts(rows[0].get("ts"))
    if last is None:
        return False

    delta = now - last
    # Solo cuenta como "reciente" un ts en el PASADO dentro de la ventana. Un ts
    # futuro (reloj desfasado) no debe bloquear el ciclo.
    return timedelta(0) <= delta < timedelta(minutes=within_minutes)


def _guard_skips() -> bool:
    """
    Resuelve la BD y consulta la guarda. Si la BD no está disponible, NO bloquea
    (devuelve False): la guarda es una protección, no un requisito para correr.
    """
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:  # noqa: BLE001
        logger.warning("Guarda anti-duplicado: BD no disponible (%s); se procede con el ciclo.", e)
        return False
    return recently_ran(db)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,   # GitHub Actions captura stdout
    )

    # GUARDA anti-duplicado (red de seguridad de los GRUPOS de cron): si otro
    # cron del mismo grupo ya procesó un ciclo hace < GUARD_WINDOW_MINUTES min,
    # salimos LIMPIAMENTE (exit 0, sin error, sin enviar otro parte).
    if _guard_skips():
        logger.info(
            "Ciclo ya ejecutado hace poco (< %d min): omito para evitar duplicado.",
            GUARD_WINDOW_MINUTES,
        )
        return 0

    return run_cycle("INTRADÍA", build_steps())


if __name__ == "__main__":
    sys.exit(main())
