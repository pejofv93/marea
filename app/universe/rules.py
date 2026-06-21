"""
Reglas y parámetros del universo dinámico MAREA.

STOCK_POOL — criterio de selección:
  S&P 500 large-cap (≈ top 200 por market cap a mediados de 2025) más
  instrumentos de alto volumen habitual fuera del S&P 500 (PLTR, SNOW, ARM…).
  Este pool captura >98% de las acciones que aparecen en el top-50 por volumen
  en cualquier sesión NYSE/NASDAQ. No incluye todos los 500 componentes del
  S&P para mantener la llamada yfinance manejable (~140 tickers).
  Revisión manual recomendada cada trimestre para incluir IPOs grandes.
"""

TOP_CRYPTO_N: int = 20   # top-N por market cap (CoinGecko)
TOP_STOCK_N: int = 50    # top-N por volumen promedio 5d (yfinance)

STOCK_POOL: list[str] = [
    # ── Megacap tecnología ────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO",
    # ── Tecnología large-cap ──────────────────────────────────────────────────
    "AMD", "ORCL", "ADBE", "CRM", "INTC", "CSCO", "IBM", "QCOM", "TXN", "ADI",
    "AMAT", "LRCX", "MU", "KLAC", "MRVL", "INTU", "NOW", "PANW", "FTNT",
    # ── Tecnología alto crecimiento / alto volumen ────────────────────────────
    "SNOW", "PLTR", "ARM", "SMCI", "CRWD", "ZS", "DDOG", "NET",
    # ── Servicios financieros ─────────────────────────────────────────────────
    "JPM", "V", "MA", "BAC", "GS", "MS", "WFC", "BLK", "SPGI", "AXP",
    "C", "USB", "PNC", "COF", "ICE", "CME", "SCHW", "CB",
    # ── Salud ─────────────────────────────────────────────────────────────────
    "UNH", "JNJ", "ABBV", "MRK", "LLY", "BMY", "GILD", "AMGN", "TMO", "MDT",
    "ABT", "SYK", "ISRG", "REGN", "VRTX", "ZTS", "HUM", "CI", "ELV", "CVS",
    # ── Consumo discrecional / básicos ────────────────────────────────────────
    "WMT", "HD", "MCD", "SBUX", "NKE", "KO", "PEP", "PM", "MO", "TGT",
    "LOW", "TJX", "COST", "DIS", "NFLX", "EBAY", "PYPL", "UBER", "ABNB",
    "PG", "CL", "KMB",
    # ── Industriales / Defensa ────────────────────────────────────────────────
    "RTX", "HON", "CAT", "DE", "LMT", "GE", "BA", "UPS", "FDX", "EMR", "ETN",
    # ── Energía ───────────────────────────────────────────────────────────────
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "VLO", "MPC",
    # ── Materiales ────────────────────────────────────────────────────────────
    "LIN", "APD", "SHW", "FCX", "NEM",
    # ── Telecom ───────────────────────────────────────────────────────────────
    "VZ", "T",
]
