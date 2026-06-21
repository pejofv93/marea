import os

# Fija variables de entorno ANTES de cualquier import de app.*
# Así pydantic-settings no falla por falta de credenciales reales.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key-for-testing")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("INGEST_PERIOD", "5d")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key-for-testing")
