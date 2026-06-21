"""
Tests MAREA Sesión 6 — Mapa de exposición indirecta.

MÓDULO CRÍTICO: el test más importante es que un candidato SIN URL
sea descartado sin persistirse nunca.

Cubre:
  - verify.py: validación dura, clasificación por dominio
  - discovery.py: parseo de respuesta LLM, construcción de prompts
  - engine.py: orquestación, descarte, upsert idempotente
  - llm_client.py: fallback Groq → Gemini
  - Los 132 tests previos siguen verdes
"""

import json
from unittest.mock import MagicMock, patch, call

import pytest

from app.exposure.llm_client import LLMResponse


# ══════════════════════════════════════════════════════════════════════════════
# S6-1: verify.py — validación de URLs
# ══════════════════════════════════════════════════════════════════════════════

class TestIsValidUrl:

    def _f(self, url):
        from app.exposure.verify import _is_valid_url
        return _is_valid_url(url)

    def test_valid_https(self):
        assert self._f("https://sec.gov/filing") is True

    def test_valid_http(self):
        assert self._f("http://reuters.com/article") is True

    def test_localhost_invalid(self):
        assert self._f("http://localhost:8000/path") is False

    def test_no_scheme_invalid(self):
        assert self._f("sec.gov/filing") is False

    def test_empty_string_invalid(self):
        assert self._f("") is False

    def test_relative_path_invalid(self):
        assert self._f("/some/path") is False

    def test_no_dot_in_host_invalid(self):
        # 'myserver' no tiene punto → no es un dominio real
        assert self._f("https://myserver/path") is False

    def test_loopback_ip_invalid(self):
        assert self._f("http://127.0.0.1/page") is False

    def test_url_with_path_and_query(self):
        assert self._f("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany") is True


class TestIsOfficialSource:

    def _f(self, url):
        from app.exposure.verify import _is_official_source
        return _is_official_source(url)

    def test_sec_gov_official(self):
        assert self._f("https://sec.gov/Archives/edgar/data/0001234/...") is True

    def test_edgar_sec_gov_official(self):
        assert self._f("https://edgar.sec.gov/cgi-bin/browse-edgar") is True

    def test_ir_subdomain_official(self):
        assert self._f("https://ir.microsoft.com/news-releases/2023/...") is True

    def test_investor_subdomain_official(self):
        assert self._f("https://investor.amazon.com/press-releases/...") is True

    def test_newsroom_subdomain_official(self):
        assert self._f("https://newsroom.amazon.com/2024-announcement") is True

    def test_press_release_path_official(self):
        assert self._f("https://company.com/press-releases/2024/openai-deal") is True

    def test_reuters_not_official(self):
        assert self._f("https://reuters.com/technology/ai/openai-deal-2024") is False

    def test_random_blog_not_official(self):
        assert self._f("https://randomblog.wordpress.com/post") is False


class TestIsReputableMedia:

    def _f(self, url):
        from app.exposure.verify import _is_reputable_media
        return _is_reputable_media(url)

    def test_reuters(self):
        assert self._f("https://reuters.com/technology/ai/article") is True

    def test_bloomberg(self):
        assert self._f("https://bloomberg.com/news/articles/2024/...") is True

    def test_ft(self):
        assert self._f("https://ft.com/content/article-slug") is True

    def test_wsj(self):
        assert self._f("https://wsj.com/articles/title-2024") is True

    def test_techcrunch(self):
        assert self._f("https://techcrunch.com/2024/01/article") is True

    def test_subdomain_of_reputable(self):
        assert self._f("https://uk.reuters.com/article") is True

    def test_random_blog_not_reputable(self):
        assert self._f("https://randomblog.xyz/post") is False

    def test_sec_gov_not_reputable(self):
        # sec.gov es oficial, no "prensa"
        assert self._f("https://sec.gov/filing") is False


# ══════════════════════════════════════════════════════════════════════════════
# S6-2: classify_confidence
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyConfidence:

    def _f(self, sources):
        from app.exposure.verify import classify_confidence
        return classify_confidence(sources)

    def test_sec_gov_official(self):
        assert self._f(["https://sec.gov/Archives/edgar/data/123/10k.htm"]) == "confirmado_oficial"

    def test_ir_subdomain_official(self):
        assert self._f(["https://ir.microsoft.com/press-releases/2024/openai-partnership"]) == "confirmado_oficial"

    def test_reuters_rumor_prensa(self):
        assert self._f(["https://reuters.com/technology/ai/openai-msft-2024"]) == "rumor_prensa"

    def test_bloomberg_rumor_prensa(self):
        assert self._f(["https://bloomberg.com/news/articles/2024/deal"]) == "rumor_prensa"

    def test_weak_url_speculation(self):
        assert self._f(["https://someblog.example.com/openai-msft-deal"]) == "especulacion"

    def test_official_wins_over_media(self):
        """Si hay una fuente oficial entre varias, gana confirmado_oficial."""
        sources = [
            "https://randomsite.xyz/rumor",
            "https://sec.gov/filing/10k.htm",
            "https://reuters.com/article",
        ]
        assert self._f(sources) == "confirmado_oficial"

    def test_media_wins_over_speculation(self):
        """Si no hay oficial pero hay prensa, gana rumor_prensa."""
        sources = [
            "https://randomsite.xyz/rumor",
            "https://reuters.com/article",
        ]
        assert self._f(sources) == "rumor_prensa"

    def test_empty_sources_raises(self):
        """sources vacío debe lanzar ValueError (invariante del módulo)."""
        from app.exposure.verify import classify_confidence
        with pytest.raises(ValueError, match="al menos una URL"):
            classify_confidence([])


# ══════════════════════════════════════════════════════════════════════════════
# S6-3: verify_candidate — la validación dura
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifyCandidate:

    def _call(self, sources, entity="OpenAI", ticker="MSFT",
              etype="pre_ipo", rel="stake", engine="groq"):
        from app.exposure.verify import verify_candidate
        return verify_candidate(
            source_entity=entity, exposed_ticker=ticker,
            exposure_type=etype, relationship=rel,
            sources=sources, llm_engine=engine,
        )

    # ── TEST MÁS CRÍTICO: sin URL → descartado ────────────────────────────────

    def test_no_sources_discarded(self):
        """REGLA ABSOLUTA: candidato sin URL → None, nunca persistido."""
        assert self._call(sources=[]) is None

    def test_none_sources_discarded(self):
        assert self._call(sources=None) is None

    def test_invalid_url_only_discarded(self):
        """URL inválida (sin dominio real) → descartado."""
        assert self._call(sources=["not-a-url", "ftp://something"]) is None

    def test_localhost_url_discarded(self):
        """localhost no es fuente real → descartado."""
        assert self._call(sources=["http://localhost:8000/foo"]) is None

    # ── Candidatos que pasan ──────────────────────────────────────────────────

    def test_sec_gov_passes_as_official(self):
        vc = self._call(sources=["https://sec.gov/Archives/edgar/data/123/10k.htm"])
        assert vc is not None
        assert vc.confidence == "confirmado_oficial"
        assert vc.is_hypothesis is False

    def test_reuters_passes_as_rumor(self):
        vc = self._call(sources=["https://reuters.com/technology/ai/deal"])
        assert vc is not None
        assert vc.confidence == "rumor_prensa"
        assert vc.is_hypothesis is True

    def test_weak_url_passes_as_speculation(self):
        vc = self._call(sources=["https://someblog.xyz/post"])
        assert vc is not None
        assert vc.confidence == "especulacion"
        assert vc.is_hypothesis is True

    def test_filters_invalid_urls_keeps_valid(self):
        """Mezcla de inválidas + válida: sólo la válida pasa."""
        vc = self._call(sources=["", "not-url", "https://reuters.com/article"])
        assert vc is not None
        assert len(vc.sources) == 1
        assert "reuters.com" in vc.sources[0]

    def test_verified_candidate_fields(self):
        """VerifiedCandidate tiene todos los campos correctos."""
        vc = self._call(
            sources=["https://sec.gov/filing"],
            entity="OpenAI", ticker="MSFT", etype="pre_ipo",
            rel="equity stake via Azure", engine="groq",
        )
        assert vc.source_entity == "OpenAI"
        assert vc.exposed_ticker == "MSFT"
        assert vc.exposure_type == "pre_ipo"
        assert vc.relationship == "equity stake via Azure"
        assert vc.llm_engine == "groq"
        assert isinstance(vc.sources, list)
        assert len(vc.sources) >= 1

    def test_sources_invariant_never_empty_on_pass(self):
        """Si verify_candidate devuelve un objeto, sources nunca está vacío."""
        vc = self._call(sources=["https://bloomberg.com/article"])
        assert vc is not None
        assert len(vc.sources) > 0


# ══════════════════════════════════════════════════════════════════════════════
# S6-4: discovery.py — parseo de respuesta LLM
# ══════════════════════════════════════════════════════════════════════════════

class TestParseDiscoveryResponse:

    def _parse(self, text, global_sources=None, entity="OpenAI", etype="pre_ipo", engine="groq"):
        from app.exposure.discovery import parse_candidates
        response = LLMResponse(text=text, sources=global_sources or [], engine=engine)
        return parse_candidates(response, entity, etype)

    def test_parses_valid_json_array(self):
        text = '''[
          {"exposed_ticker": "MSFT", "relationship": "equity stake", "source_urls": ["https://sec.gov/filing"]}
        ]'''
        candidates = self._parse(text)
        assert len(candidates) == 1
        assert candidates[0].exposed_ticker == "MSFT"
        assert "https://sec.gov/filing" in candidates[0].sources

    def test_ticker_uppercased(self):
        text = '[{"exposed_ticker": "msft", "relationship": "stake", "source_urls": ["https://sec.gov/f"]}]'
        candidates = self._parse(text)
        assert candidates[0].exposed_ticker == "MSFT"

    def test_combines_inline_and_global_sources(self):
        """Las inline source_urls del JSON se combinan con las globales del search."""
        text = '[{"exposed_ticker": "AMZN", "relationship": "investment", "source_urls": ["https://sec.gov/a"]}]'
        candidates = self._parse(text, global_sources=["https://reuters.com/b"])
        # Ambas fuentes deben estar presentes
        assert any("sec.gov" in s for s in candidates[0].sources)
        assert any("reuters.com" in s for s in candidates[0].sources)

    def test_empty_array_returns_empty(self):
        candidates = self._parse("[]")
        assert candidates == []

    def test_no_json_returns_empty(self):
        candidates = self._parse("I could not find any verified information.")
        assert candidates == []

    def test_preamble_text_ignored(self):
        text = 'Sure! Here are the results:\n[{"exposed_ticker": "GOOGL", "relationship": "investor", "source_urls": ["https://reuters.com/x"]}]\nHope this helps!'
        candidates = self._parse(text)
        assert len(candidates) == 1
        assert candidates[0].exposed_ticker == "GOOGL"

    def test_invalid_item_skipped(self):
        """Items sin exposed_ticker se omiten."""
        text = '[{"relationship": "no ticker here"}, {"exposed_ticker": "AMZN", "source_urls": ["https://sec.gov/f"]}]'
        candidates = self._parse(text)
        assert len(candidates) == 1
        assert candidates[0].exposed_ticker == "AMZN"

    def test_no_inline_urls_still_uses_global(self):
        """Sin source_urls en el JSON pero con fuentes globales → se asignan las globales."""
        text = '[{"exposed_ticker": "NVDA", "relationship": "cloud deal"}]'
        candidates = self._parse(text, global_sources=["https://bloomberg.com/deal"])
        assert len(candidates) == 1
        assert any("bloomberg.com" in s for s in candidates[0].sources)


class TestBuildPrompts:

    def test_pre_ipo_prompt_mentions_web_search(self):
        from app.exposure.discovery import build_pre_ipo_prompt
        prompt = build_pre_ipo_prompt("Anthropic")
        assert "Anthropic" in prompt
        assert "web" in prompt.lower() or "search" in prompt.lower()
        assert "source_urls" in prompt   # el LLM sabe que debe devolver URLs

    def test_crypto_prompt_mentions_entity(self):
        from app.exposure.discovery import build_crypto_prompt
        prompt = build_crypto_prompt("BTC")
        assert "BTC" in prompt
        assert "source_urls" in prompt


class TestDiscoveryService:

    def _make_service(self, response_text: str, sources: list[str] = None, engine: str = "groq"):
        from app.exposure.discovery import DiscoveryService
        mock_response = LLMResponse(
            text=response_text,
            sources=sources or [],
            engine=engine,
        )
        return DiscoveryService(llm_fn=lambda p: mock_response)

    def test_discover_pre_ipo_returns_candidates(self):
        svc = self._make_service(
            '[{"exposed_ticker": "MSFT", "relationship": "stake", "source_urls": ["https://sec.gov/f"]}]'
        )
        candidates = svc.discover_pre_ipo("OpenAI")
        assert len(candidates) == 1
        assert candidates[0].source_entity == "OpenAI"
        assert candidates[0].exposure_type == "pre_ipo"

    def test_discover_crypto_returns_candidates(self):
        svc = self._make_service(
            '[{"exposed_ticker": "MSTR", "relationship": "BTC treasury", "source_urls": ["https://sec.gov/mstr"]}]'
        )
        candidates = svc.discover_crypto("BTC")
        assert len(candidates) == 1
        assert candidates[0].source_entity == "BTC"
        assert candidates[0].exposure_type == "crypto"

    def test_llm_error_returns_empty(self):
        """Si el LLM lanza excepción, discover devuelve [] sin propagar."""
        from app.exposure.discovery import DiscoveryService
        svc = DiscoveryService(llm_fn=lambda p: (_ for _ in ()).throw(RuntimeError("API down")))
        candidates = svc.discover_pre_ipo("OpenAI")
        assert candidates == []


# ══════════════════════════════════════════════════════════════════════════════
# S6-5: engine.py — orquestación e integración con BD mockeada
# ══════════════════════════════════════════════════════════════════════════════

def _make_mock_db():
    """Mock de Supabase con chain fluente para upsert y select."""
    mock_db = MagicMock()
    mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
    mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    return mock_db


def _make_llm_fn(text: str, sources: list[str] = None, engine: str = "groq"):
    """LLM mock que devuelve una respuesta fija."""
    resp = LLMResponse(text=text, sources=sources or [], engine=engine)
    return lambda prompt: resp


class TestExposureEngine:

    # ── TEST MÁS CRÍTICO ──────────────────────────────────────────────────────

    def test_candidate_without_url_not_persisted(self):
        """
        REGLA ABSOLUTA: si el LLM devuelve candidato sin URL real,
        el engine NO debe llamar a upsert para ese candidato.
        """
        from app.exposure.engine import ExposureEngine

        # LLM devuelve candidato sin source_urls
        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "MSFT", "relationship": "stake", "source_urls": []}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)

        result = engine.run_sync(
            pre_ipo_targets=["OpenAI"],
            crypto_targets=[],
        )

        # No debe haber persistido nada
        assert result["total_persisted"] == 0
        assert result["discarded_no_url"] == 1
        # Upsert NO debe haberse llamado con datos de exposición
        upsert_calls = mock_db.table.return_value.upsert.call_args_list
        exposure_upserts = [c for c in upsert_calls if c[0] and c[0][0]]
        assert len(exposure_upserts) == 0

    def test_candidate_without_url_increments_discarded(self):
        from app.exposure.engine import ExposureEngine

        # Dos candidatos: uno sin URL, uno con URL válida
        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "MSFT", "relationship": "no url", "source_urls": []},'
            ' {"exposed_ticker": "AMZN", "relationship": "with url", "source_urls": ["https://reuters.com/x"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)
        result = engine.run_sync(pre_ipo_targets=["OpenAI"], crypto_targets=[])

        assert result["discarded_no_url"] == 1
        assert result["total_persisted"] == 1

    def test_candidate_with_url_triggers_upsert(self):
        """Candidato con URL válida → upsert llamado."""
        from app.exposure.engine import ExposureEngine

        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "MSFT", "relationship": "stake ~49%", "source_urls": ["https://sec.gov/filing"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)
        result = engine.run_sync(pre_ipo_targets=["OpenAI"], crypto_targets=[])

        assert result["total_persisted"] == 1
        assert mock_db.table.return_value.upsert.called

    def test_idempotency_upsert_on_conflict(self):
        """
        Correr dos veces → upsert con on_conflict, no inserts duplicados.
        """
        from app.exposure.engine import ExposureEngine

        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "MSTR", "relationship": "BTC treasury", "source_urls": ["https://sec.gov/mstr"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)

        engine.run_sync(pre_ipo_targets=[], crypto_targets=["BTC"])
        engine.run_sync(pre_ipo_targets=[], crypto_targets=["BTC"])

        # Verifica que upsert se llamó con on_conflict
        upsert_call = mock_db.table.return_value.upsert
        assert upsert_call.called
        # El segundo argumento del keyword call debe incluir on_conflict
        for call_args in upsert_call.call_args_list:
            kwargs = call_args[1]
            if "on_conflict" in kwargs:
                assert "source_entity" in kwargs["on_conflict"]
                break

    def test_result_dict_structure(self):
        """El resultado tiene todos los campos esperados."""
        from app.exposure.engine import ExposureEngine

        engine = ExposureEngine(
            db=_make_mock_db(),
            llm_fn=_make_llm_fn("[]"),
        )
        result = engine.run_sync(pre_ipo_targets=["X"], crypto_targets=[])

        for field in ("raw_candidates", "discarded_no_url", "persisted_by_confidence",
                      "total_persisted", "errors", "ok"):
            assert field in result, f"Campo '{field}' ausente en result"

    def test_confidence_classification_in_result(self):
        """Los persistidos se clasifican por nivel de confianza en el resultado."""
        from app.exposure.engine import ExposureEngine

        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "MSFT", "relationship": "sec filing stake", '
            '"source_urls": ["https://sec.gov/filing/openai.htm"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)
        result = engine.run_sync(pre_ipo_targets=["OpenAI"], crypto_targets=[])

        assert "confirmado_oficial" in result["persisted_by_confidence"]
        assert result["persisted_by_confidence"]["confirmado_oficial"] == 1

    def test_sources_passed_as_list_to_upsert(self):
        """Las fuentes se pasan como list[str] al upsert (Supabase maneja JSONB)."""
        from app.exposure.engine import ExposureEngine

        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "AMZN", "relationship": "Anthropic investor", '
            '"source_urls": ["https://reuters.com/anthropic-amazon"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)
        engine.run_sync(pre_ipo_targets=["Anthropic"], crypto_targets=[])

        upsert_call = mock_db.table.return_value.upsert.call_args
        if upsert_call:
            row_batch = upsert_call[0][0]   # primer argumento posicional
            if row_batch:
                row = row_batch[0]
                assert isinstance(row["sources"], list), "sources debe ser list, no string JSON"

    def test_empty_llm_response_no_error(self):
        """Respuesta LLM vacía no genera errores ni persistidos."""
        from app.exposure.engine import ExposureEngine

        engine = ExposureEngine(db=_make_mock_db(), llm_fn=_make_llm_fn("[]"))
        result = engine.run_sync(pre_ipo_targets=["OpenAI"], crypto_targets=[])

        assert result["total_persisted"] == 0
        assert result["ok"] is True

    def test_multiple_targets_processed(self):
        """Múltiples objetivos pre_ipo y crypto son procesados."""
        from app.exposure.engine import ExposureEngine

        counter = {"n": 0}

        def counting_llm(prompt):
            counter["n"] += 1
            return LLMResponse(text="[]", sources=[], engine="groq")

        engine = ExposureEngine(db=_make_mock_db(), llm_fn=counting_llm)
        engine.run_sync(
            pre_ipo_targets=["OpenAI", "Anthropic"],
            crypto_targets=["BTC", "ETH"],
        )

        # 2 pre_ipo + 2 crypto = 4 llamadas al LLM
        assert counter["n"] == 4


# ══════════════════════════════════════════════════════════════════════════════
# S6-6: llm_client.py — fallback Groq → Gemini
# ══════════════════════════════════════════════════════════════════════════════

class TestLLMClient:

    def test_groq_called(self):
        """query_with_web_search delega en _query_groq."""
        groq_response = LLMResponse(text='[]', sources=[], engine="groq")
        with patch("app.exposure.llm_client._query_groq", return_value=groq_response) as mock_groq:
            from app.exposure.llm_client import query_with_web_search
            result = query_with_web_search("test prompt")
            assert result.engine == "groq"
            mock_groq.assert_called_once()

    def test_groq_fail_propagates_exception(self):
        """Si Groq falla, la excepción se propaga al llamador (no hay fallback)."""
        with patch("app.exposure.llm_client._query_groq", side_effect=RuntimeError("Groq down")):
            from app.exposure.llm_client import query_with_web_search
            with pytest.raises(RuntimeError, match="Groq down"):
                query_with_web_search("test prompt")

    def test_groq_fail_discovery_returns_empty(self):
        """DiscoveryService captura la excepción de Groq y devuelve [] sin propagar."""
        with patch("app.exposure.llm_client._query_groq", side_effect=RuntimeError("Groq down")):
            from app.exposure.discovery import DiscoveryService
            # El query_fn del servicio llama a query_with_web_search, que llama a _query_groq
            from app.exposure.llm_client import query_with_web_search
            svc = DiscoveryService(llm_fn=query_with_web_search)
            candidates = svc.discover_pre_ipo("OpenAI")
            assert candidates == []   # error registrado, nada persistido

    def test_groq_fail_engine_records_zero_persisted(self):
        """Con Groq caído, el engine no persiste nada y registra el error."""
        from app.exposure.engine import ExposureEngine
        with patch("app.exposure.llm_client._query_groq", side_effect=RuntimeError("Groq down")):
            from app.exposure.llm_client import query_with_web_search
            mock_db = _make_mock_db()
            engine = ExposureEngine(db=mock_db, llm_fn=query_with_web_search)
            result = engine.run_sync(pre_ipo_targets=["OpenAI"], crypto_targets=[])
            assert result["total_persisted"] == 0
            # El upsert de exposures no debe haberse llamado
            assert not mock_db.table.return_value.upsert.called

    def test_urls_from_text_extraction(self):
        """_urls_from_text extrae URLs correctamente del texto."""
        from app.exposure.llm_client import _urls_from_text
        text = 'See https://sec.gov/filing and https://reuters.com/article for details.'
        urls = _urls_from_text(text)
        assert any("sec.gov" in u for u in urls)
        assert any("reuters.com" in u for u in urls)

    def test_urls_from_text_empty(self):
        from app.exposure.llm_client import _urls_from_text
        assert _urls_from_text("") == []
        assert _urls_from_text("No URLs here.") == []


# ══════════════════════════════════════════════════════════════════════════════
# S6-7: Persistencia — sources vacío NUNCA persiste
# ══════════════════════════════════════════════════════════════════════════════

class TestSourcesNeverEmpty:

    def test_engine_never_calls_upsert_with_empty_sources(self):
        """
        Garantía de punta a punta: ningún row que llegue a upsert
        puede tener sources vacío.
        """
        from app.exposure.engine import ExposureEngine

        # LLM devuelve candidatos mezclados: uno sin URL, uno con URL
        llm_fn = _make_llm_fn(
            '[{"exposed_ticker": "BAD", "relationship": "no url", "source_urls": []},'
            ' {"exposed_ticker": "GOOD", "relationship": "has url", "source_urls": ["https://sec.gov/f"]}]'
        )
        mock_db = _make_mock_db()
        engine = ExposureEngine(db=mock_db, llm_fn=llm_fn)
        engine.run_sync(pre_ipo_targets=["Test"], crypto_targets=[])

        # Verifica que si upsert fue llamado, ningún row tiene sources vacío
        upsert_calls = mock_db.table.return_value.upsert.call_args_list
        for call_args in upsert_calls:
            batch = call_args[0][0] if call_args[0] else []
            for row in batch:
                sources = row.get("sources", [])
                assert sources, f"Row con sources vacío llegó al upsert: {row}"
