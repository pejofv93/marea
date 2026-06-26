from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_key: str = ""
    ingest_period: str = "5d"
    ingest_cron_hour: int = 6
    ingest_cron_minute: int = 0
    scheduler_enabled: bool = True
    # Scoring
    score_window_short: int = 7
    score_window_long: int = 30
    score_min_obs: int = 10
    # LLM — descubrimiento de exposición indirecta (Sesión 6)
    groq_api_key: str = ""
    # Telegram — alertas proactivas (Sesión 8)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Umbrales de alerta (todos configurables vía .env)
    flow_extreme_threshold: float = 0.7   # |score| que dispara alerta
    min_alert_confidence: float = 0.4     # confianza mínima numérica para enviar
    # Carril intradía (Sesión 9b)
    intraday_interval: str = "60m"        # '60m' | '15m'
    intraday_period: str = "5d"           # period de yfinance para barras intradía
    intraday_flow_threshold: float = 0.6  # |score intradía| que dispara alerta/análisis
    # Resumen por ciclo en Telegram (Sesión 11) — señal de vida + foto del mercado
    digest_enabled: bool = True           # envía el mensaje-resumen en cada ciclo
    # Indicadores de CONTEXTO de régimen (Bloque 1) — termómetros, no flujo.
    # Auto-activación: un indicador no modula el régimen ni se presenta como señal
    # sólida hasta acumular al menos context_min_obs observaciones propias.
    context_min_obs: int = 5
    # Ticker del 2Y para la curva 10Y-2Y (CBOE 2-Year yield future en yfinance).
    # Configurable por si cambia: ^FVX (5Y) o ^IRX (3M) sirven como alternativa.
    yield_curve_short_ticker: str = "2YY=F"
    # Credibilidad del flujo (Bloque 2) — penaliza fogonazos. La señal de
    # PERSISTENCIA se auto-activa al superar este nº de observaciones; por debajo,
    # la credibilidad se calcula solo con volumen+precio (activos desde el día 1).
    credibility_persist_min_obs: int = 10
    # Detección temprana (Bloque 4) — desacoples + volumen anómalo. Es el bloque
    # que MÁS depende de histórico (necesita una línea base de normalidad), así que
    # DESPIERTA MÁS TARDE por diseño: hasta acumular estas observaciones no muestra
    # nada (declara "estableciendo línea base"). Nunca señales falsas por falta de datos.
    early_corr_min_obs: int = 15      # barras en la ventana base para fiarse de una correlación
    early_volume_min_obs: int = 20    # observaciones para una media/σ de volumen fiables

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
