"""
Tests de MAREA Sesiones 1 y 2.
"""

from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient


# ──────────────────────────────────────────────────────────────────────────────
# Helpers compartidos
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_TICKERS = {
    "^GSPC", "^IXIC", "^IBEX", "^N225",
    "GC=F", "SI=F",
    "DX-Y.NYB", "^VIX", "^TNX",
    "SPY", "QQQ", "GLD", "SLV", "IBIT",
    "SOXX", "SMH", "XME", "GDX", "SIL",
    "ITA", "XAR", "XLE", "XLK", "XLF", "XLV",
}


def make_mock_yf_df(tickers: list[str]) -> pd.DataFrame:
    """DataFrame MultiIndex que imita yf.download() con múltiples tickers."""
    dates = pd.DatetimeIndex(["2024-01-15"], name="Date")
    fields = ["Open", "High", "Low", "Close", "Volume"]
    col_tuples = [(f, t) for f in fields for t in tickers]
    midx = pd.MultiIndex.from_tuples(col_tuples, names=["Price", "Ticker"])
    data = np.full((1, len(col_tuples)), 100.0)
    for i, (f, _) in enumerate(col_tuples):
        if f == "Volume":
            data[0, i] = 1_000_000.0
    return pd.DataFrame(data, index=dates, columns=midx)


def make_mock_db(tickers: list[str], source: str = None) -> MagicMock:
    """
    Mock del cliente Supabase.
    Soporta cadenas con 1 o 2 llamadas a .eq() (yfinance usa 2 desde S2).
    El parámetro source es solo documental; MagicMock ignora los argumentos.
    """
    mock_db = MagicMock()
    data = [{"id": i + 1, "ticker": t} for i, t in enumerate(tickers)]
    _eq1 = mock_db.table.return_value.select.return_value.eq.return_value
    _eq1.execute.return_value.data = data                          # una .eq()
    _eq1.eq.return_value.execute.return_value.data = data          # dos .eq()
    mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
    return mock_db


# ──────────────────────────────────────────────────────────────────────────────
# S1 — 1. Universo fijo yfinance
# ──────────────────────────────────────────────────────────────────────────────

class TestFixedUniverse:
    def test_all_expected_tickers_present(self):
        from app.universe.fixed import FIXED_TICKERS
        assert set(FIXED_TICKERS) == EXPECTED_TICKERS, (
            f"Faltan: {EXPECTED_TICKERS - set(FIXED_TICKERS)} | "
            f"Sobran: {set(FIXED_TICKERS) - EXPECTED_TICKERS}"
        )

    def test_no_duplicate_tickers(self):
        from app.universe.fixed import FIXED_TICKERS
        assert len(FIXED_TICKERS) == len(set(FIXED_TICKERS))

    def test_asset_class_valid(self):
        from app.universe.fixed import FIXED_ASSETS, VALID_ASSET_CLASSES
        for asset in FIXED_ASSETS:
            assert asset["asset_class"] in VALID_ASSET_CLASSES, (
                f"{asset['ticker']} tiene asset_class inválido: {asset['asset_class']}"
            )

    def test_required_fields_present(self):
        from app.universe.fixed import FIXED_ASSETS
        for asset in FIXED_ASSETS:
            assert "ticker" in asset
            assert "name" in asset
            assert "asset_class" in asset
            assert "sector" in asset


# ──────────────────────────────────────────────────────────────────────────────
# S1 — 2. Ingesta yfinance en lote
# ──────────────────────────────────────────────────────────────────────────────

class TestIngestFixedUniverse:
    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_single_batch_download(self, mock_download):
        """yfinance.download debe llamarse exactamente UNA vez con TODOS los tickers."""
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        mock_download.return_value = make_mock_yf_df(FIXED_TICKERS)
        ingestor = IngestFixedUniverse(db=make_mock_db(FIXED_TICKERS, "yfinance"))
        ingestor.run_sync()

        assert mock_download.call_count == 1, "yf.download debe llamarse solo una vez (lote)"

    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_download_receives_all_tickers(self, mock_download):
        """El único llamado a download debe incluir todos los tickers del universo fijo."""
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        mock_download.return_value = make_mock_yf_df(FIXED_TICKERS)
        ingestor = IngestFixedUniverse(db=make_mock_db(FIXED_TICKERS, "yfinance"))
        ingestor.run_sync()

        call_kwargs = mock_download.call_args.kwargs
        tickers_arg = call_kwargs.get("tickers") or mock_download.call_args.args[0]
        assert set(tickers_arg) == set(FIXED_TICKERS)

    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_upsert_records_structure(self, mock_download):
        """Los registros enviados a upsert deben tener los campos requeridos."""
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        mock_download.return_value = make_mock_yf_df(FIXED_TICKERS)
        mock_db = make_mock_db(FIXED_TICKERS, "yfinance")
        ingestor = IngestFixedUniverse(db=mock_db)
        ingestor.run_sync()

        upsert_calls = mock_db.table.return_value.upsert.call_args_list
        assert len(upsert_calls) >= 1

        required_fields = {"asset_id", "ts", "open", "high", "low", "close", "volume", "extra"}
        for c in upsert_calls:
            records = c.args[0]
            assert isinstance(records, list)
            for rec in records:
                missing = required_fields - set(rec.keys())
                assert not missing, f"Campos faltantes en registro: {missing}"

    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_result_counts(self, mock_download):
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        mock_download.return_value = make_mock_yf_df(FIXED_TICKERS)
        result = IngestFixedUniverse(db=make_mock_db(FIXED_TICKERS, "yfinance")).run_sync()

        assert result["assets_queried"] == len(FIXED_TICKERS)
        assert result["snapshots_inserted"] > 0

    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_missing_ticker_does_not_abort(self, mock_download):
        """Si falta un ticker en la respuesta de yfinance, la ingesta continúa."""
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        tickers_minus_one = [t for t in FIXED_TICKERS if t != "IBIT"]
        mock_download.return_value = make_mock_yf_df(tickers_minus_one)
        result = IngestFixedUniverse(db=make_mock_db(FIXED_TICKERS, "yfinance")).run_sync()

        assert result["snapshots_inserted"] > 0
        assert "IBIT" in result["tickers_missing"]


# ──────────────────────────────────────────────────────────────────────────────
# S1 — 3. Endpoint /health
# ──────────────────────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_ok(self):
        from app.main import app
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "version" in body


# ──────────────────────────────────────────────────────────────────────────────
# S2 — CoinGecko
# ──────────────────────────────────────────────────────────────────────────────

def _cg_response():
    return [
        {
            "id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
            "current_price": 45_000.0, "high_24h": 46_000.0, "low_24h": 44_000.0,
            "market_cap": 880_000_000_000, "total_volume": 25_000_000_000,
            "price_change_percentage_24h": 2.5, "circulating_supply": 19_500_000.0,
        },
        {
            "id": "ethereum", "symbol": "eth", "name": "Ethereum",
            "current_price": 2_500.0, "high_24h": 2_600.0, "low_24h": 2_400.0,
            "market_cap": 300_000_000_000, "total_volume": 15_000_000_000,
            "price_change_percentage_24h": 1.2, "circulating_supply": 120_000_000.0,
        },
    ]


class TestCoinGecko:
    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_single_call_for_btc_and_eth(self, mock_fetch):
        """CoinGecko debe ser llamada UNA sola vez para BTC+ETH conjuntamente."""
        from app.ingest.crypto_coingecko import IngestCoinGecko
        mock_fetch.return_value = _cg_response()
        IngestCoinGecko(db=make_mock_db(["BTC", "ETH"], "coingecko")).run_sync()
        assert mock_fetch.call_count == 1

    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_ids_param_contains_both_coins(self, mock_fetch):
        """El param ids debe contener bitcoin y ethereum en una sola llamada."""
        from app.ingest.crypto_coingecko import IngestCoinGecko
        mock_fetch.return_value = _cg_response()
        IngestCoinGecko(db=make_mock_db(["BTC", "ETH"], "coingecko")).run_sync()
        params = mock_fetch.call_args.kwargs.get("params", {})
        ids = params.get("ids", "")
        assert "bitcoin" in ids
        assert "ethereum" in ids

    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_extra_has_market_cap_and_volume(self, mock_fetch):
        """Los registros deben tener market_cap y volume_24h en extra."""
        from app.ingest.crypto_coingecko import IngestCoinGecko
        mock_fetch.return_value = _cg_response()
        db = make_mock_db(["BTC", "ETH"], "coingecko")
        IngestCoinGecko(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        for rec in records:
            assert "market_cap" in rec["extra"]
            assert "volume_24h" in rec["extra"]

    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_close_is_current_price(self, mock_fetch):
        from app.ingest.crypto_coingecko import IngestCoinGecko
        mock_fetch.return_value = _cg_response()
        db = make_mock_db(["BTC", "ETH"], "coingecko")
        IngestCoinGecko(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        closes = {r["close"] for r in records}
        assert 45_000.0 in closes
        assert 2_500.0 in closes

    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_two_snapshots_inserted(self, mock_fetch):
        from app.ingest.crypto_coingecko import IngestCoinGecko
        mock_fetch.return_value = _cg_response()
        result = IngestCoinGecko(db=make_mock_db(["BTC", "ETH"], "coingecko")).run_sync()
        assert result["snapshots_inserted"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# S2 — DefiLlama
# ──────────────────────────────────────────────────────────────────────────────

def _dl_response():
    return {
        "peggedAssets": [
            {
                "id": "1", "name": "Tether", "symbol": "USDT",
                "circulating": {"peggedUSD": 139_000_000_000},
                "circulatingPrevDay": {"peggedUSD": 138_000_000_000},
            },
            {
                "id": "2", "name": "USD Coin", "symbol": "USDC",
                "circulating": {"peggedUSD": 43_000_000_000},
                "circulatingPrevDay": {"peggedUSD": 42_500_000_000},
            },
            # DAI debe ser ignorado
            {"id": "99", "name": "DAI", "symbol": "DAI",
             "circulating": {"peggedUSD": 5_000_000_000}},
        ]
    }


class TestDefiLlama:
    @patch("app.ingest.crypto_defillama.fetch_json")
    def test_only_usdt_and_usdc_inserted(self, mock_fetch):
        """Extrae USDT y USDC; ignora DAI y cualquier otro stablecoin."""
        from app.ingest.crypto_defillama import IngestDefiLlama
        mock_fetch.return_value = _dl_response()
        result = IngestDefiLlama(
            db=make_mock_db(["STABLES_USDT", "STABLES_USDC"], "defillama")
        ).run_sync()
        assert result["snapshots_inserted"] == 2

    @patch("app.ingest.crypto_defillama.fetch_json")
    def test_supply_stored_in_close(self, mock_fetch):
        """El supply circulante en USD debe estar en close (no en extra)."""
        from app.ingest.crypto_defillama import IngestDefiLlama
        mock_fetch.return_value = _dl_response()
        db = make_mock_db(["STABLES_USDT", "STABLES_USDC"], "defillama")
        IngestDefiLlama(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        for rec in records:
            assert rec["close"] is not None
            assert rec["close"] > 0

    @patch("app.ingest.crypto_defillama.fetch_json")
    def test_extra_has_prev_day_supply(self, mock_fetch):
        from app.ingest.crypto_defillama import IngestDefiLlama
        mock_fetch.return_value = _dl_response()
        db = make_mock_db(["STABLES_USDT", "STABLES_USDC"], "defillama")
        IngestDefiLlama(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        for rec in records:
            assert "supply_prev_day" in rec["extra"]
            assert "change_usd" in rec["extra"]


# ──────────────────────────────────────────────────────────────────────────────
# S2 — Binance
# ──────────────────────────────────────────────────────────────────────────────

def _binance_funding():
    return [
        {"symbol": "BTCUSDT", "markPrice": "45000.5", "indexPrice": "45001.0",
         "lastFundingRate": "0.00010000", "nextFundingTime": 1705363200000},
        {"symbol": "ETHUSDT", "markPrice": "2500.5", "indexPrice": "2501.0",
         "lastFundingRate": "0.00020000", "nextFundingTime": 1705363200000},
        # Par extra que debe ser ignorado
        {"symbol": "SOLUSDT", "markPrice": "100.0", "lastFundingRate": "0.0001"},
    ]


class TestBinance:
    @patch("app.ingest.crypto_binance.fetch_json")
    def test_funding_and_oi_in_extra(self, mock_fetch):
        """funding_rate y open_interest deben aparecer en extra."""
        from app.ingest.crypto_binance import IngestBinance
        mock_fetch.side_effect = [
            _binance_funding(),
            {"openInterest": "12345.0", "symbol": "BTCUSDT"},
            {"openInterest": "67890.0", "symbol": "ETHUSDT"},
        ]
        db = make_mock_db(["BTC_PERP", "ETH_PERP"], "binance")
        IngestBinance(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        for rec in records:
            assert "funding_rate" in rec["extra"]
            assert "open_interest" in rec["extra"]

    @patch("app.ingest.crypto_binance.fetch_json")
    def test_only_btc_eth_perp_inserted(self, mock_fetch):
        """Solo procesa BTCUSDT y ETHUSDT; ignora SOLUSDT y otros."""
        from app.ingest.crypto_binance import IngestBinance
        mock_fetch.side_effect = [
            _binance_funding(),
            {"openInterest": "12345.0", "symbol": "BTCUSDT"},
            {"openInterest": "67890.0", "symbol": "ETHUSDT"},
        ]
        result = IngestBinance(
            db=make_mock_db(["BTC_PERP", "ETH_PERP"], "binance")
        ).run_sync()
        assert result["snapshots_inserted"] == 2

    @patch("app.ingest.crypto_binance.fetch_json")
    def test_close_is_mark_price(self, mock_fetch):
        """close debe ser el mark price del perpetuo."""
        from app.ingest.crypto_binance import IngestBinance
        mock_fetch.side_effect = [
            _binance_funding(),
            {"openInterest": "12345.0", "symbol": "BTCUSDT"},
            {"openInterest": "67890.0", "symbol": "ETHUSDT"},
        ]
        db = make_mock_db(["BTC_PERP", "ETH_PERP"], "binance")
        IngestBinance(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        closes = {r["close"] for r in records}
        assert 45000.5 in closes
        assert 2500.5 in closes

    @patch("app.ingest.crypto_binance.fetch_json")
    def test_three_http_calls_made(self, mock_fetch):
        """Binance hace 3 llamadas: 1 premiumIndex + 2 openInterest."""
        from app.ingest.crypto_binance import IngestBinance
        mock_fetch.side_effect = [
            _binance_funding(),
            {"openInterest": "12345.0", "symbol": "BTCUSDT"},
            {"openInterest": "67890.0", "symbol": "ETHUSDT"},
        ]
        IngestBinance(db=make_mock_db(["BTC_PERP", "ETH_PERP"], "binance")).run_sync()
        assert mock_fetch.call_count == 3


# ──────────────────────────────────────────────────────────────────────────────
# S2 — Fear & Greed
# ──────────────────────────────────────────────────────────────────────────────

def _fng_response():
    return {
        "data": [{"value": "72", "value_classification": "Greed", "timestamp": "1705363200"}],
        "metadata": {"error": None},
    }


class TestFNG:
    @patch("app.ingest.crypto_fng.fetch_json")
    def test_value_and_classification_in_extra(self, mock_fetch):
        """Valor 0-100 en close, clasificación en extra."""
        from app.ingest.crypto_fng import IngestFNG
        mock_fetch.return_value = _fng_response()
        db = make_mock_db(["CRYPTO_FNG"], "alternative_me")
        IngestFNG(db=db).run_sync()
        records = db.table.return_value.upsert.call_args_list[0].args[0]
        assert len(records) == 1
        assert records[0]["close"] == 72.0
        assert records[0]["extra"]["value_classification"] == "Greed"

    @patch("app.ingest.crypto_fng.fetch_json")
    def test_one_snapshot_inserted(self, mock_fetch):
        from app.ingest.crypto_fng import IngestFNG
        mock_fetch.return_value = _fng_response()
        result = IngestFNG(db=make_mock_db(["CRYPTO_FNG"], "alternative_me")).run_sync()
        assert result["snapshots_inserted"] == 1

    @patch("app.ingest.crypto_fng.fetch_json")
    def test_empty_data_handled_gracefully(self, mock_fetch):
        """Si la API devuelve data vacío, no crashea."""
        from app.ingest.crypto_fng import IngestFNG
        mock_fetch.return_value = {"data": [], "metadata": {"error": None}}
        result = IngestFNG(db=make_mock_db(["CRYPTO_FNG"], "alternative_me")).run_sync()
        assert result["snapshots_inserted"] == 0
        assert "CRYPTO_FNG" in result["tickers_missing"]


# ──────────────────────────────────────────────────────────────────────────────
# S2 — Orquestador (run_all)
# ──────────────────────────────────────────────────────────────────────────────

def _empty_source_result(source: str) -> dict:
    return {"source": source, "snapshots_inserted": 0, "tickers_missing": [], "errors": [], "ok": True}


class TestIngestAll:
    @patch("app.ingest.run_all.time.sleep")        # evita la pausa _HTTP_PAUSE_S en tests
    @patch("app.ingest.run_all.IngestFNG")
    @patch("app.ingest.run_all.IngestBinance")
    @patch("app.ingest.run_all.IngestDefiLlama")
    @patch("app.ingest.run_all.IngestCoinGecko")
    @patch("app.ingest.run_all.IngestFixedUniverse")
    def test_all_five_sources_run(self, mock_yf, mock_cg, mock_dl, mock_bn, mock_fng, mock_sleep):
        """El orquestador ejecuta las 5 fuentes y devuelve by_source con todas."""
        from app.ingest.run_all import IngestAll
        _names = ["yfinance_fixed", "coingecko", "defillama", "binance", "fng"]
        for mock_cls, name in zip((mock_yf, mock_cg, mock_dl, mock_bn, mock_fng), _names):
            mock_cls.return_value.run_sync.return_value = _empty_source_result(name)

        result = IngestAll().run_sync()

        assert set(result["by_source"].keys()) == {
            "yfinance_fixed", "coingecko", "defillama", "binance", "fng"
        }
        assert "total_snapshots" in result
        assert "ok" in result

    @patch("app.ingest.run_all.time.sleep")
    @patch("app.ingest.run_all.IngestFNG")
    @patch("app.ingest.run_all.IngestBinance")
    @patch("app.ingest.run_all.IngestDefiLlama")
    @patch("app.ingest.run_all.IngestCoinGecko")
    @patch("app.ingest.run_all.IngestFixedUniverse")
    def test_single_source_crash_doesnt_abort_others(
        self, mock_yf, mock_cg, mock_dl, mock_bn, mock_fng, mock_sleep
    ):
        """Si una fuente lanza excepción no capturada, el resto continúa ejecutándose."""
        from app.ingest.run_all import IngestAll

        mock_yf.return_value.run_sync.side_effect = RuntimeError("yfinance caído")
        _names = ["coingecko", "defillama", "binance", "fng"]
        for mock_cls, name in zip((mock_cg, mock_dl, mock_bn, mock_fng), _names):
            mock_cls.return_value.run_sync.return_value = _empty_source_result(name)

        result = IngestAll().run_sync()

        # yfinance_fixed debe estar marcado como fallido
        assert result["by_source"]["yfinance_fixed"]["ok"] is False
        # Las otras 4 fuentes deben estar en by_source
        for source in ("coingecko", "defillama", "binance", "fng"):
            assert source in result["by_source"]
        # El error de yfinance debe aparecer en la lista global
        assert any("yfinance_fixed" in e for e in result["errors"])

    @patch("app.ingest.run_all.time.sleep")
    @patch("app.ingest.run_all.IngestFNG")
    @patch("app.ingest.run_all.IngestBinance")
    @patch("app.ingest.run_all.IngestDefiLlama")
    @patch("app.ingest.run_all.IngestCoinGecko")
    @patch("app.ingest.run_all.IngestFixedUniverse")
    def test_total_snapshots_is_sum(
        self, mock_yf, mock_cg, mock_dl, mock_bn, mock_fng, mock_sleep
    ):
        """total_snapshots debe ser la suma de todos los by_source."""
        from app.ingest.run_all import IngestAll

        counts = {"yfinance_fixed": 25, "coingecko": 2, "defillama": 2, "binance": 2, "fng": 1}
        for mock_cls, name in zip(
            (mock_yf, mock_cg, mock_dl, mock_bn, mock_fng),
            counts.keys()
        ):
            r = _empty_source_result(name)
            r["snapshots_inserted"] = counts[name]
            mock_cls.return_value.run_sync.return_value = r

        result = IngestAll().run_sync()
        assert result["total_snapshots"] == sum(counts.values())


# ──────────────────────────────────────────────────────────────────────────────
# S3 — Helpers para UniverseRecomputer
# ──────────────────────────────────────────────────────────────────────────────

def make_universe_db(existing_assets: list[dict]) -> tuple[MagicMock, MagicMock, MagicMock]:
    """
    Mock del cliente Supabase para UniverseRecomputer.
    Devuelve (db, assets_mock, history_mock) para poder inspeccionar
    llamadas en cada tabla por separado.
    """
    mock_db = MagicMock()
    assets_mock = MagicMock()
    history_mock = MagicMock()

    def _table(name):
        return assets_mock if name == "assets" else history_mock

    mock_db.table.side_effect = _table

    # _apply_changes: .select(...).eq(ingest_source).eq(asset_class).execute()
    eq1 = assets_mock.select.return_value.eq.return_value
    eq1.eq.return_value.execute.return_value.data = existing_assets

    # _insert_asset: .insert(...).execute().data = [{"id": 999}]
    assets_mock.insert.return_value.execute.return_value.data = [{"id": 999}]

    return mock_db, assets_mock, history_mock


def _top20_coins(exclude: set[str] | None = None) -> list[dict]:
    """20 coins falsos para CoinGecko. exclude: símbolos a omitir."""
    exclude = exclude or set()
    return [
        {"symbol": f"COIN{i}", "name": f"Coin{i}", "id": f"coin{i}"}
        for i in range(30)
        if f"COIN{i}" not in exclude
    ][:20]


# ──────────────────────────────────────────────────────────────────────────────
# S3 — 1. Refactor de filtros (is_active / ingest_source)
# ──────────────────────────────────────────────────────────────────────────────

class TestIngestSourceRefactor:
    """Verifica que TODOS los módulos de ingesta usan is_active e ingest_source."""

    @patch("app.ingest.yfinance_fixed.yf.download")
    def test_yfinance_uses_is_active_and_ingest_source(self, mock_download):
        from app.universe.fixed import FIXED_TICKERS
        from app.ingest.yfinance_fixed import IngestFixedUniverse

        mock_download.return_value = make_mock_yf_df(FIXED_TICKERS)
        mock_db = make_mock_db(FIXED_TICKERS, "yfinance")
        IngestFixedUniverse(db=mock_db).run_sync()

        select_mock = mock_db.table.return_value.select.return_value
        # Primera .eq() debe ser is_active=True (no is_fixed=True)
        assert call("is_active", True) in select_mock.eq.call_args_list
        # No debe haber referencias al campo viejo
        old_field_used = any(
            "is_fixed" in str(c) or c == call("is_fixed", True)
            for c in select_mock.eq.call_args_list
        )
        assert not old_field_used, "yfinance_fixed todavía usa is_fixed (campo viejo)"
        # Segunda .eq() debe ser ingest_source (no source)
        assert call("ingest_source", "yfinance") in select_mock.eq.return_value.eq.call_args_list

    @patch("app.ingest.crypto_coingecko.fetch_json")
    def test_coingecko_uses_is_active_and_ingest_source(self, mock_fetch):
        from app.ingest.crypto_coingecko import IngestCoinGecko

        mock_fetch.return_value = _cg_response()
        mock_db = make_mock_db(["BTC", "ETH"], "coingecko")
        IngestCoinGecko(db=mock_db).run_sync()

        select_mock = mock_db.table.return_value.select.return_value
        assert call("is_active", True) in select_mock.eq.call_args_list
        assert call("ingest_source", "coingecko") in select_mock.eq.return_value.eq.call_args_list

    @patch("app.ingest.crypto_defillama.fetch_json")
    def test_defillama_uses_is_active_and_ingest_source(self, mock_fetch):
        from app.ingest.crypto_defillama import IngestDefiLlama

        mock_fetch.return_value = _dl_response()
        mock_db = make_mock_db(["STABLES_USDT", "STABLES_USDC"], "defillama")
        IngestDefiLlama(db=mock_db).run_sync()

        select_mock = mock_db.table.return_value.select.return_value
        assert call("is_active", True) in select_mock.eq.call_args_list
        assert call("ingest_source", "defillama") in select_mock.eq.return_value.eq.call_args_list

    @patch("app.ingest.crypto_binance.fetch_json")
    def test_binance_uses_is_active_and_ingest_source(self, mock_fetch):
        from app.ingest.crypto_binance import IngestBinance

        mock_fetch.side_effect = [
            _binance_funding(),
            {"openInterest": "123.0", "symbol": "BTCUSDT"},
            {"openInterest": "456.0", "symbol": "ETHUSDT"},
        ]
        mock_db = make_mock_db(["BTC_PERP", "ETH_PERP"], "binance")
        IngestBinance(db=mock_db).run_sync()

        select_mock = mock_db.table.return_value.select.return_value
        assert call("is_active", True) in select_mock.eq.call_args_list
        assert call("ingest_source", "binance") in select_mock.eq.return_value.eq.call_args_list

    @patch("app.ingest.crypto_fng.fetch_json")
    def test_fng_uses_is_active_and_ingest_source(self, mock_fetch):
        from app.ingest.crypto_fng import IngestFNG

        mock_fetch.return_value = _fng_response()
        mock_db = make_mock_db(["CRYPTO_FNG"], "alternative_me")
        IngestFNG(db=mock_db).run_sync()

        select_mock = mock_db.table.return_value.select.return_value
        assert call("is_active", True) in select_mock.eq.call_args_list
        assert call("ingest_source", "alternative_me") in select_mock.eq.return_value.eq.call_args_list


# ──────────────────────────────────────────────────────────────────────────────
# S3 — 2. UniverseRecomputer
# ──────────────────────────────────────────────────────────────────────────────

class TestUniverseRecomputer:

    @patch("app.universe.dynamic.fetch_json")
    def test_crypto_top20_single_batch_call(self, mock_fetch):
        """CoinGecko debe llamarse UNA vez con per_page=20 (anti-baneo)."""
        from app.universe.dynamic import UniverseRecomputer
        from app.universe.rules import TOP_CRYPTO_N

        mock_fetch.return_value = _top20_coins()
        mock_db, _, _ = make_universe_db([])
        UniverseRecomputer(db=mock_db).recompute_top_crypto()

        assert mock_fetch.call_count == 1, "CoinGecko llamada más de una vez"
        params = mock_fetch.call_args.kwargs.get("params", {})
        assert params.get("per_page") == str(TOP_CRYPTO_N)

    @patch("app.universe.dynamic.fetch_json")
    def test_reactivation_no_duplicate_insert(self, mock_fetch):
        """
        Asset que existía inactivo y vuelve al top → UPDATE is_active=True.
        NO debe insertarse una fila nueva en assets.
        """
        from app.universe.dynamic import UniverseRecomputer

        # SOL existe en BD como inactivo
        existing = [{"id": 50, "ticker": "SOL", "is_active": False, "is_fixed": False}]
        top20 = [{"symbol": "SOL", "name": "Solana"}] + _top20_coins(exclude={"SOL"})[:19]
        mock_fetch.return_value = top20

        mock_db, assets_mock, _ = make_universe_db(existing)
        result = UniverseRecomputer(db=mock_db).recompute_top_crypto()

        # SOL debe aparecer en activated
        assert "SOL" in result["activated"]
        # update() debe haberse llamado (reactivación)
        assets_mock.update.assert_called()
        # insert() en assets NO debe haberse llamado para SOL (ya existe)
        # insert() SÍ puede llamarse para los otros 19 coins nuevos
        insert_calls = assets_mock.insert.call_args_list
        inserted_tickers = [
            c.args[0].get("ticker") for c in insert_calls if c.args
        ]
        assert "SOL" not in inserted_tickers, "SOL fue insertado de nuevo (duplicado)"

    @patch("app.universe.dynamic.fetch_json")
    def test_is_fixed_immune_to_deactivation(self, mock_fetch):
        """
        Asset con is_fixed=True NUNCA se desactiva, aunque no esté en el top-N.
        """
        from app.universe.dynamic import UniverseRecomputer

        # BTC está fijo y activo, NO aparece en el top-20 devuelto
        existing = [{"id": 1, "ticker": "BTC", "is_active": True, "is_fixed": True}]
        top20 = _top20_coins()  # no contiene BTC
        mock_fetch.return_value = top20

        mock_db, assets_mock, _ = make_universe_db(existing)
        result = UniverseRecomputer(db=mock_db).recompute_top_crypto()

        # BTC NO debe aparecer en deactivated
        assert "BTC" not in result["deactivated"]
        # update() no debe llamarse para desactivar (is_active=False)
        for c in assets_mock.update.call_args_list:
            payload = c.args[0] if c.args else c.kwargs.get("json", {})
            assert payload.get("is_active") is not False, (
                "Se intentó desactivar un asset (posiblemente BTC con is_fixed=True)"
            )

    @patch("app.universe.dynamic.fetch_json")
    def test_new_asset_inserted(self, mock_fetch):
        """Ticker nuevo que no existe en BD → INSERT en assets con is_active=True."""
        from app.universe.dynamic import UniverseRecomputer

        # BD vacía: ningún crypto de coingecko
        mock_fetch.return_value = [{"symbol": "DOGE", "name": "Dogecoin"}]

        mock_db, assets_mock, _ = make_universe_db([])
        result = UniverseRecomputer(db=mock_db).recompute_top_crypto()

        assert "DOGE" in result["activated"]
        assets_mock.insert.assert_called_once()
        inserted = assets_mock.insert.call_args.args[0]
        assert inserted["ticker"] == "DOGE"
        assert inserted["is_active"] is True
        assert inserted["is_fixed"] is False
        assert inserted["ingest_source"] == "coingecko"

    @patch("app.universe.dynamic.yf.download")
    def test_stock_recompute_single_yf_call(self, mock_download):
        """yf.download llamado UNA vez con todos los tickers de STOCK_POOL (anti-baneo)."""
        from app.universe.dynamic import UniverseRecomputer
        from app.universe.rules import STOCK_POOL

        # DataFrame con los primeros 5 tickers del pool (resto tendrán volumen 0)
        mock_download.return_value = make_mock_yf_df(STOCK_POOL[:5])
        mock_db, _, _ = make_universe_db([])
        UniverseRecomputer(db=mock_db).recompute_top_stocks()

        assert mock_download.call_count == 1, "yf.download llamado más de una vez"
        call_kwargs = mock_download.call_args.kwargs
        assert set(call_kwargs.get("tickers", [])) == set(STOCK_POOL)
        assert call_kwargs.get("threads") is False

    @patch("app.universe.dynamic.fetch_json")
    def test_inactive_asset_excluded_from_ingest(self, mock_fetch):
        """
        Activo desactivado (is_active=False) no debe recibir snapshots.
        Verifica indirectamente: load_asset_map filtra por is_active=True.
        """
        from app.ingest.crypto_coingecko import IngestCoinGecko

        # BTC inactivo en BD → asset_map vacío → ingest no inserta nada
        mock_fetch.return_value = _cg_response()
        mock_db = MagicMock()
        # Simula BD donde BTC está is_active=False → load_asset_map devuelve {}
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        result = IngestCoinGecko(db=mock_db).run_sync()

        assert result["snapshots_inserted"] == 0
        mock_db.table.return_value.upsert.assert_not_called()
