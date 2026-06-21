from functools import lru_cache
from supabase import create_client, Client
from app.config import settings


@lru_cache(maxsize=1)
def get_db() -> Client:
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_KEY deben estar configurados en .env")
    return create_client(settings.supabase_url, settings.supabase_key)
