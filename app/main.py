import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("marea.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.scheduler_enabled:
        from app.scheduler import setup_scheduler, scheduler
        setup_scheduler()
        scheduler.start()
        logger.info("Scheduler iniciado")
    yield
    from app.scheduler import scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido")


app = FastAPI(
    title="MAREA",
    description="Monitor de flujos de liquidez intermercado",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/ingest/run", summary="Dispara ingesta completa: yfinance + crypto + on-chain")
async def ingest_run():
    from app.ingest.run_all import IngestAll

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, IngestAll().run_sync)
    return result


@app.get("/universe/recompute", summary="Recalcula universo dinámico top-N (soft-delete)")
async def universe_recompute():
    from app.universe.dynamic import UniverseRecomputer

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, UniverseRecomputer().run_sync)
    return result


@app.get("/universe/active", summary="Lista los assets activos del universo")
async def universe_active():
    from app.db import get_db

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("assets")
            .select("id,ticker,name,asset_class,ingest_source,is_fixed")
            .eq("is_active", True)
            .execute()
        )
        return {"assets": resp.data or [], "total": len(resp.data or [])}

    return await loop.run_in_executor(None, _query)


@app.get("/scores/compute", summary="Calcula flow scores para todos los assets activos")
async def scores_compute():
    from app.scoring.engine import ScoreEngine
    from app.config import settings

    loop = asyncio.get_event_loop()
    engine = ScoreEngine(min_obs=settings.score_min_obs)
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get("/scores/latest", summary="Últimos flow scores por asset activo")
async def scores_latest():
    from app.db import get_db
    from app.ingest._base import day_ts

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        # Últimos scores de hoy (o del día más reciente disponible) para cada asset
        resp = (
            db.table("flow_scores")
            .select("asset_id,ts,win,score,raw_zscore,proxy_used,n_obs,confidence,assets(ticker,asset_class,sector)")
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        return {"scores": resp.data or [], "total": len(resp.data or [])}

    return await loop.run_in_executor(None, _query)


# ── Análisis intermercado (Sesión 5) ──────────────────────────────────────────

@app.get(
    "/analysis/run",
    summary="Corre análisis completo: correlaciones, régimen y rotación sectorial",
)
async def analysis_run():
    from app.analysis.engine import AnalysisEngine

    loop = asyncio.get_event_loop()
    engine = AnalysisEngine()
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get(
    "/analysis/regime/latest",
    summary="Último régimen detectado con las señales que lo dispararon",
)
async def analysis_regime_latest():
    from app.db import get_db

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("regimes")
            .select("ts,win,regime,confidence,signals")
            .order("ts", desc=True)
            .limit(2)   # 7d y 30d del último día
            .execute()
        )
        return {"regimes": resp.data or [], "total": len(resp.data or [])}

    return await loop.run_in_executor(None, _query)


@app.get(
    "/analysis/correlations",
    summary="Matriz de correlaciones actual (intermarket o sector)",
)
async def analysis_correlations(type: str = "intermarket"):
    from app.db import get_db

    if type not in ("intermarket", "sector"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type debe ser 'intermarket' o 'sector'")

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("correlations")
            .select("ts,win,matrix_type,pair_a,pair_b,corr,is_decoupling")
            .eq("matrix_type", type)
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
        decouplings = [r for r in rows if r.get("is_decoupling")]
        return {
            "matrix_type": type,
            "correlations": rows,
            "total": len(rows),
            "decouplings": len(decouplings),
        }

    return await loop.run_in_executor(None, _query)


# ── Mapa de exposición indirecta (Sesión 6) ───────────────────────────────────

_HYPOTHESIS_WARNING = (
    "SIN VERIFICAR — hipótesis especulativa basada en búsqueda web automatizada. "
    "No constituye asesoramiento de inversión. Verifique las fuentes antes de operar."
)


@app.get(
    "/exposure/discover",
    summary="Dispara descubrimiento de exposición indirecta vía LLM + búsqueda web",
)
async def exposure_discover():
    from app.exposure.engine import ExposureEngine

    loop = asyncio.get_event_loop()
    engine = ExposureEngine()
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


# ── Narrativa LLM (Sesión 7) ──────────────────────────────────────────────────

_NARRATIVE_DISCLAIMER = (
    "Interpretación automática de datos · no es consejo de inversión."
)


@app.get(
    "/narrative/generate",
    summary="Construye snapshot, genera narrativa LLM (sin web), persiste y devuelve texto + sello",
)
async def narrative_generate():
    from app.narrative.engine import NarrativeEngine

    loop = asyncio.get_event_loop()
    engine = NarrativeEngine()
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get(
    "/narrative/latest",
    summary="Última narrativa persisitida con sello de interpretación y nivel de confianza",
)
async def narrative_latest():
    from app.db import get_db

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("narratives")
            .select("ts,regime_at_ts,confidence,text,llm_engine,created_at")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return {"narrative": None, "disclaimer": _NARRATIVE_DISCLAIMER}
        row = dict(rows[0])
        row["disclaimer"] = _NARRATIVE_DISCLAIMER
        return {"narrative": row}

    return await loop.run_in_executor(None, _query)


# ── Motor de alertas + bot de Telegram (Sesión 8) ────────────────────────────

@app.get(
    "/alerts/run",
    summary="Evalúa las 4 reglas de alerta y (si procede) envía mensajes a Telegram",
)
async def alerts_run():
    from app.alerts.engine import AlertEngine

    loop = asyncio.get_event_loop()
    engine = AlertEngine()
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get(
    "/alerts/recent",
    summary="Últimas alertas registradas (enviadas y no enviadas, con razón)",
)
async def alerts_recent(limit: int = 50):
    from app.db import get_db

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("alerts")
            .select("id,alert_type,entity,state,confidence,sent,not_sent_reason,ts,sent_at")
            .order("ts", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
        return {"alerts": rows, "total": len(rows)}

    return await loop.run_in_executor(None, _query)


@app.post(
    "/alerts/test",
    summary="Envía un mensaje de prueba a Telegram para verificar token + chat_id",
)
async def alerts_test():
    from app.alerts.telegram import send_message
    from app.config import settings

    loop = asyncio.get_event_loop()

    def _send():
        text = (
            "✅ <b>MAREA — Test de conectividad</b>\n"
            "El bot de alertas está configurado correctamente.\n"
            "⚠️ Las alertas no son consejo de inversión."
        )
        ok = send_message(text, token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id)
        if ok:
            return {"ok": True, "message": "Mensaje de prueba enviado correctamente"}
        return {
            "ok": False,
            "message": "No se pudo enviar el mensaje. Verifica TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.",
        }

    return await loop.run_in_executor(None, _send)


# ── Carril intradía (Sesión 9b) ──────────────────────────────────────────────

@app.get(
    "/ingest/intraday/run",
    summary="Dispara ingesta intradía: yfinance barras 60m/15m + crypto actual + FNG",
)
async def ingest_intraday_run():
    from app.ingest.intraday_runner import IntradayRunner

    loop = asyncio.get_event_loop()
    runner = IntradayRunner()
    result = await loop.run_in_executor(None, runner.run_sync)
    return result


@app.get(
    "/scores/intraday/compute",
    summary="Calcula flow_scores_intraday para todos los assets activos",
)
async def scores_intraday_compute():
    from app.scoring.intraday_engine import IntradayScoreEngine
    from app.config import settings

    loop = asyncio.get_event_loop()
    engine = IntradayScoreEngine(min_obs=settings.score_min_obs)
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get(
    "/analysis/intraday/run",
    summary="Detecta movimientos de liquidez intradía en curso (inflow/outflow por activo)",
)
async def analysis_intraday_run():
    from app.analysis.intraday import IntradayAnalysisEngine

    loop = asyncio.get_event_loop()
    engine = IntradayAnalysisEngine()
    result = await loop.run_in_executor(None, engine.run_sync)
    return result


@app.get(
    "/scores/intraday/latest",
    summary="Últimos flow scores intradía por activo (ambas ventanas: 4h y 1d_intraday)",
)
async def scores_intraday_latest():
    from app.db import get_db
    from app.config import settings

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("flow_scores_intraday")
            .select(
                "asset_id,ts,interval,win,score,raw_zscore,"
                "proxy_used,n_obs,confidence,"
                "assets(ticker,asset_class,sector)"
            )
            .eq("interval", settings.intraday_interval)
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        return {"scores": resp.data or [], "total": len(resp.data or [])}

    return await loop.run_in_executor(None, _query)


@app.get(
    "/exposure/map",
    summary="Mapa actual de exposición (pre_ipo o crypto). Baja confianza = hipótesis.",
)
async def exposure_map(type: str = "pre_ipo"):
    from app.db import get_db

    if type not in ("pre_ipo", "crypto"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type debe ser 'pre_ipo' o 'crypto'")

    loop = asyncio.get_event_loop()

    def _query():
        db = get_db()
        resp = (
            db.table("exposures")
            .select("source_entity,exposed_ticker,exposure_type,relationship,confidence,sources,llm_engine,last_verified_at")
            .eq("exposure_type", type)
            .order("confidence")        # confirmado_oficial primero (alfabético)
            .execute()
        )
        rows = resp.data or []

        # Añade advertencia visible para hipótesis no verificadas
        enriched = []
        for row in rows:
            entry = dict(row)
            if entry.get("confidence") != "confirmado_oficial":
                entry["hypothesis_warning"] = _HYPOTHESIS_WARNING
            enriched.append(entry)

        confirmed = sum(1 for r in rows if r.get("confidence") == "confirmado_oficial")
        hypothesis = len(rows) - confirmed

        return {
            "exposure_type": type,
            "exposures": enriched,
            "total": len(rows),
            "confirmed": confirmed,
            "hypothesis_count": hypothesis,
            "disclaimer": (
                "Las exposiciones de baja confianza son hipótesis generadas por IA "
                "y NO han sido verificadas manualmente. No constituyen asesoramiento de inversión."
            ),
        }

    return await loop.run_in_executor(None, _query)
