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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
