FIXED_ASSETS: list[dict] = [
    # ── Índices macro ──────────────────────────────────────────────────────────
    {"ticker": "^GSPC",    "name": "S&P 500",                            "asset_class": "index",     "sector": None},
    {"ticker": "^IXIC",    "name": "Nasdaq Composite",                   "asset_class": "index",     "sector": None},
    {"ticker": "^IBEX",    "name": "IBEX 35",                            "asset_class": "index",     "sector": None},
    {"ticker": "^N225",    "name": "Nikkei 225",                         "asset_class": "index",     "sector": None},
    # ── Commodities ───────────────────────────────────────────────────────────
    {"ticker": "GC=F",     "name": "Gold Futures",                       "asset_class": "commodity", "sector": "metals"},
    {"ticker": "SI=F",     "name": "Silver Futures",                     "asset_class": "commodity", "sector": "metals"},
    # ── Macro / divisas / tipos ───────────────────────────────────────────────
    {"ticker": "DX-Y.NYB", "name": "US Dollar Index",                   "asset_class": "macro",     "sector": "currency"},
    {"ticker": "^VIX",     "name": "CBOE Volatility Index",              "asset_class": "macro",     "sector": "volatility"},
    {"ticker": "^TNX",     "name": "10-Year Treasury Yield",             "asset_class": "macro",     "sector": "rates"},
    # ── ETFs principales ──────────────────────────────────────────────────────
    {"ticker": "SPY",      "name": "SPDR S&P 500 ETF",                  "asset_class": "etf",       "sector": "broad_market"},
    {"ticker": "QQQ",      "name": "Invesco QQQ Trust",                  "asset_class": "etf",       "sector": "broad_market"},
    {"ticker": "GLD",      "name": "SPDR Gold Shares",                   "asset_class": "etf",       "sector": "commodities"},
    {"ticker": "SLV",      "name": "iShares Silver Trust",               "asset_class": "etf",       "sector": "commodities"},
    {"ticker": "IBIT",     "name": "iShares Bitcoin Trust",              "asset_class": "etf",       "sector": "crypto"},
    # ── ETFs sectoriales ──────────────────────────────────────────────────────
    {"ticker": "SOXX",     "name": "iShares Semiconductor ETF",          "asset_class": "etf",       "sector": "semiconductor"},
    {"ticker": "SMH",      "name": "VanEck Semiconductor ETF",           "asset_class": "etf",       "sector": "semiconductor"},
    {"ticker": "XME",      "name": "SPDR S&P Metals & Mining ETF",       "asset_class": "etf",       "sector": "metals_mining"},
    {"ticker": "GDX",      "name": "VanEck Gold Miners ETF",             "asset_class": "etf",       "sector": "gold_miners"},
    {"ticker": "SIL",      "name": "Global X Silver Miners ETF",         "asset_class": "etf",       "sector": "silver_miners"},
    {"ticker": "ITA",      "name": "iShares US Aerospace & Defense ETF", "asset_class": "etf",       "sector": "aerospace_defense"},
    {"ticker": "XAR",      "name": "SPDR S&P Aerospace & Defense ETF",   "asset_class": "etf",       "sector": "aerospace_defense"},
    {"ticker": "XLE",      "name": "Energy Select Sector SPDR ETF",      "asset_class": "etf",       "sector": "energy"},
    {"ticker": "XLK",      "name": "Technology Select Sector SPDR ETF",  "asset_class": "etf",       "sector": "technology"},
    {"ticker": "XLF",      "name": "Financial Select Sector SPDR ETF",   "asset_class": "etf",       "sector": "financials"},
    {"ticker": "XLV",      "name": "Health Care Select Sector SPDR ETF", "asset_class": "etf",       "sector": "healthcare"},
]

FIXED_TICKERS: list[str] = [a["ticker"] for a in FIXED_ASSETS]

VALID_ASSET_CLASSES = {"index", "etf", "commodity", "macro", "crypto", "onchain"}

# ── Assets crypto y on-chain (Sesión 2) ───────────────────────────────────────
CRYPTO_ASSETS: list[dict] = [
    {"ticker": "BTC",          "name": "Bitcoin",                       "asset_class": "crypto",  "sector": "l1",               "source": "coingecko"},
    {"ticker": "ETH",          "name": "Ethereum",                      "asset_class": "crypto",  "sector": "l1",               "source": "coingecko"},
    {"ticker": "BTC_PERP",     "name": "Bitcoin Perpetual (Binance)",   "asset_class": "crypto",  "sector": "perp",             "source": "binance"},
    {"ticker": "ETH_PERP",     "name": "Ethereum Perpetual (Binance)",  "asset_class": "crypto",  "sector": "perp",             "source": "binance"},
    {"ticker": "STABLES_USDT", "name": "USDT Circulating Supply",       "asset_class": "onchain", "sector": "stablecoin",       "source": "defillama"},
    {"ticker": "STABLES_USDC", "name": "USDC Circulating Supply",       "asset_class": "onchain", "sector": "stablecoin",       "source": "defillama"},
    {"ticker": "CRYPTO_FNG",   "name": "Crypto Fear & Greed Index",     "asset_class": "macro",   "sector": "crypto_sentiment", "source": "alternative_me"},
]

CRYPTO_TICKERS: list[str] = [a["ticker"] for a in CRYPTO_ASSETS]
