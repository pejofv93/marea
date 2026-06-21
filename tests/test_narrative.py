"""
Tests MAREA Sesión 7 — Capa narrativa.

Garantías verificadas:
  (a) Narrativa cerrada: generate_narrative usa query_direct, NO query_with_web_search.
  (b) query_direct usa modelo != compound-beta y no pasa tools.
  (c) Sello de interpretación siempre presente en el resultado.
  (d) El prompt prohíbe predicciones y obliga a marcar incertidumbre.
  (e) snapshot_json guardado junto con cada narrativa (auditoría).
  (f) Confianza baja / cold_start → instrucción explícita en el prompt.
  (g) Idempotencia: segundo run usa upsert on_conflict="ts".
  (h) Si Groq falla, no se persiste narrativa y el error se registra.
  (i) Los 204 tests previos siguen verdes.
"""

from unittest.mock import MagicMock, patch, call

import pytest

from app.exposure.llm_client import LLMResponse


# ══════════════════════════════════════════════════════════════════════════════
# Helpers compartidos
# ══════════════════════════════════════════════════════════════════════════════

def _fluent(data):
    """
    Mock de tabla Supabase con encadenamiento fluente.
    Cualquier cadena de .select/.eq/.order/.limit/.upsert termina en
    .execute() que devuelve data.
    """
    m = MagicMock()
    for method in ("select", "eq", "order", "limit", "upsert"):
        getattr(m, method).return_value = m
    res = MagicMock()
    res.data = data if data is not None else []
    m.execute.return_value = res
    return m


def _make_db(regimes=None, flow_scores=None, correlations=None,
             rotations=None, exposures=None):
    """Mock de db con datos configurables por tabla."""
    table_map = {
        "regimes":      _fluent(regimes),
        "flow_scores":  _fluent(flow_scores),
        "correlations": _fluent(correlations),
        "rotations":    _fluent(rotations),
        "exposures":    _fluent(exposures),
        "narratives":   _fluent([]),
    }
    db = MagicMock()
    db.table.side_effect = lambda name: table_map.get(name, _fluent([]))
    return db


# Datos de prueba reutilizables

_REGIME = {
    "ts": "2026-06-17T00:00:00+00:00",
    "win": "7d",
    "regime": "risk_on",
    "confidence": 0.85,
    "signals": ["crypto_inflow", "equity_inflow", "dxy_falling"],
}

def _score(asset_id, ticker, asset_class, score, confidence="normal"):
    return {
        "asset_id": asset_id,
        "ts": "2026-06-17T00:00:00+00:00",
        "win": "7d",
        "score": score,
        "confidence": confidence,
        "assets": {"ticker": ticker, "asset_class": asset_class, "sector": None},
    }


_SCORES = [
    _score(1, "BTC",  "crypto",  0.8),
    _score(2, "ETH",  "crypto",  0.6),
    _score(3, "SPY",  "equity",  0.4),
    _score(4, "GLD",  "gold",   -0.3),
    _score(5, "^TNX", "bonds",  -0.7),
]

_DECOUPLING = {
    "pair_a": "BTC", "pair_b": "SPY",
    "corr": -0.55, "matrix_type": "intermarket",
    "ts": "2026-06-17T00:00:00+00:00",
}

_ROTATION = {
    "from_sector": "energy", "to_sector": "tech",
    "strength": 0.7, "ts": "2026-06-17T00:00:00+00:00",
}

_EXPOSURE = {
    "source_entity": "OpenAI", "exposed_ticker": "MSFT",
    "exposure_type": "pre_ipo", "confidence": "confirmado_oficial",
}

_FULL_SNAPSHOT = {
    "regime": {"name": "risk_on", "confidence": 0.85, "signals": ["crypto_inflow"], "ts": "x"},
    "top_inflow": [{"ticker": "BTC", "asset_class": "crypto", "sector": None, "score": 0.8, "confidence": "normal"}],
    "top_outflow": [{"ticker": "^TNX", "asset_class": "bonds", "sector": None, "score": -0.7, "confidence": "normal"}],
    "class_scores": {"crypto": 0.7, "equity": 0.4, "bonds": -0.7},
    "cold_start": False,
    "decouplings": [{"pair": "BTC/SPY", "corr": -0.55, "type": "intermarket"}],
    "rotations": [{"from": "energy", "to": "tech", "strength": 0.7}],
    "exposures": [{"entity": "OpenAI", "ticker": "MSFT", "type": "pre_ipo", "confidence": "confirmado_oficial"}],
    "generated_at": "2026-06-17T00:00:00+00:00",
}


# ══════════════════════════════════════════════════════════════════════════════
# S7-1: snapshot.py — construcción desde datos sintéticos
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildSnapshot:

    def _build(self, **kwargs):
        from app.narrative.snapshot import build_snapshot
        db = _make_db(**kwargs)
        return build_snapshot(db)

    def test_required_keys_present(self):
        snap = self._build()
        for key in ("regime", "top_inflow", "top_outflow", "class_scores",
                    "cold_start", "decouplings", "rotations", "exposures", "generated_at"):
            assert key in snap, f"Clave '{key}' ausente en snapshot"

    def test_regime_extracted(self):
        snap = self._build(regimes=[_REGIME])
        assert snap["regime"]["name"] == "risk_on"
        assert snap["regime"]["confidence"] == 0.85
        assert "crypto_inflow" in snap["regime"]["signals"]

    def test_regime_none_when_empty(self):
        snap = self._build(regimes=[])
        assert snap["regime"] is None

    def test_top_inflow_sorted_descending(self):
        snap = self._build(flow_scores=_SCORES)
        scores = [item["score"] for item in snap["top_inflow"]]
        assert scores == sorted(scores, reverse=True), "top_inflow debe ordenarse de mayor a menor score"

    def test_top_outflow_highest_negative_first(self):
        snap = self._build(flow_scores=_SCORES)
        if snap["top_outflow"]:
            scores = [item["score"] for item in snap["top_outflow"]]
            assert scores == sorted(scores), "top_outflow debe ir del score más negativo al menos negativo"

    def test_top_inflow_max_three(self):
        snap = self._build(flow_scores=_SCORES)
        assert len(snap["top_inflow"]) <= 3

    def test_class_scores_computed(self):
        snap = self._build(flow_scores=_SCORES)
        assert "crypto" in snap["class_scores"]
        # BTC(0.8) + ETH(0.6) / 2 = 0.7
        assert abs(snap["class_scores"]["crypto"] - 0.7) < 0.01

    def test_cold_start_majority_low_confidence(self):
        low_scores = [
            _score(i, f"T{i}", "equity", 0.1, "low") for i in range(4)
        ] + [_score(99, "BTC", "crypto", 0.5, "normal")]
        snap = self._build(flow_scores=low_scores)
        assert snap["cold_start"] is True

    def test_cold_start_false_when_mostly_normal(self):
        normal_scores = [_score(i, f"T{i}", "equity", 0.1, "normal") for i in range(5)]
        snap = self._build(flow_scores=normal_scores)
        assert snap["cold_start"] is False

    def test_cold_start_true_when_no_scores(self):
        snap = self._build(flow_scores=[])
        assert snap["cold_start"] is True

    def test_decouplings_mapped(self):
        snap = self._build(correlations=[_DECOUPLING])
        assert len(snap["decouplings"]) == 1
        assert snap["decouplings"][0]["pair"] == "BTC/SPY"
        assert snap["decouplings"][0]["corr"] == -0.55

    def test_rotations_mapped(self):
        snap = self._build(rotations=[_ROTATION])
        assert len(snap["rotations"]) == 1
        assert snap["rotations"][0]["from"] == "energy"
        assert snap["rotations"][0]["to"] == "tech"

    def test_exposures_mapped(self):
        snap = self._build(exposures=[_EXPOSURE])
        assert len(snap["exposures"]) == 1
        assert snap["exposures"][0]["entity"] == "OpenAI"
        assert snap["exposures"][0]["confidence"] == "confirmado_oficial"

    def test_asset_dedup_keeps_most_recent(self):
        """Dos filas del mismo asset_id → score mostrado es el de la primera fila (más reciente)."""
        dup_scores = [
            _score(1, "BTC", "crypto", 0.9),  # más reciente (ya ordenado desc por la BD)
            _score(1, "BTC", "crypto", 0.3),  # duplicado antiguo — debe descartarse
            _score(2, "ETH", "crypto", 0.5),  # asset diferente
        ]
        snap = self._build(flow_scores=dup_scores)
        btc_entries = [i for i in snap["top_inflow"] if i["ticker"] == "BTC"]
        assert btc_entries, "BTC debe aparecer en top_inflow"
        assert btc_entries[0]["score"] == pytest.approx(0.9), (
            "El score de BTC debe ser el de la fila más reciente (0.9), no el duplicado (0.3)"
        )

    def test_snapshot_is_compact(self):
        """top_inflow y top_outflow juntos no superan 2 × TOP_N elementos."""
        snap = self._build(flow_scores=_SCORES * 5)  # 25 filas de 5 assets únicos
        assert len(snap["top_inflow"]) <= 3
        assert len(snap["top_outflow"]) <= 3

    def test_partial_failure_does_not_abort(self):
        """Si una tabla falla, el resto del snapshot se construye igualmente."""
        db = _make_db(regimes=[_REGIME])
        # Forzar fallo en flow_scores
        def bad_table(name):
            if name == "flow_scores":
                m = MagicMock()
                m.select.side_effect = RuntimeError("DB down")
                return m
            return _make_db(regimes=[_REGIME]).table(name)
        db.table.side_effect = bad_table

        from app.narrative.snapshot import build_snapshot
        snap = build_snapshot(db)

        assert snap["regime"]["name"] == "risk_on"   # régimen ok
        assert snap["cold_start"] is True             # fallback por error
        assert snap["top_inflow"] == []


# ══════════════════════════════════════════════════════════════════════════════
# S7-2: generator.py — prompt con prohibiciones y marcado de incertidumbre
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPrompt:

    def _prompt(self, regime_name="risk_on", confidence=0.85,
                signals=None, cold_start=False, **snap_extra):
        from app.narrative.generator import build_prompt
        snap = {
            "regime": {"name": regime_name, "confidence": confidence, "signals": signals or []},
            "top_inflow": [],
            "top_outflow": [],
            "class_scores": {},
            "cold_start": cold_start,
            "decouplings": [],
            "rotations": [],
            **snap_extra,
        }
        return build_prompt(snap)

    def test_prompt_contains_regime(self):
        p = self._prompt(regime_name="flight_to_safety", confidence=0.7)
        assert "flight_to_safety" in p

    def test_prompt_contains_confidence_percentage(self):
        p = self._prompt(confidence=0.85)
        assert "85%" in p

    def test_low_confidence_triggers_uncertainty_instruction(self):
        """Confianza < 40 % → el prompt debe incluir instrucción de marcar incertidumbre."""
        p = self._prompt(confidence=0.3)
        assert "confianza" in p.lower()
        assert "insuficiente" in p.lower() or "baja" in p.lower() or "débil" in p.lower()

    def test_cold_start_triggers_preliminary_instruction(self):
        """cold_start=True → el prompt debe indicar datos preliminares."""
        p = self._prompt(cold_start=True)
        assert "cold_start" in p or "preliminar" in p.lower() or "insuficiente" in p.lower()

    def test_normal_confidence_no_uncertainty_instruction(self):
        """Confianza ≥ 40 % y sin cold_start → NO debe aparecer instrucción de incertidumbre."""
        from app.narrative.generator import _UNCERTAINTY_INSTRUCTION, _COLD_START_INSTRUCTION
        p = self._prompt(confidence=0.7, cold_start=False)
        assert _UNCERTAINTY_INSTRUCTION not in p
        assert _COLD_START_INSTRUCTION not in p

    def test_prompt_lists_inflow_tickers(self):
        snap_extra = {
            "top_inflow": [{"ticker": "BTC", "asset_class": "crypto", "score": 0.8, "confidence": "normal"}],
        }
        p = self._prompt(**snap_extra)
        assert "BTC" in p

    def test_prompt_lists_class_scores(self):
        p = self._prompt(class_scores={"crypto": 0.7, "bonds": -0.5})
        assert "crypto" in p
        assert "bonds" in p

    def test_prompt_includes_decoupling(self):
        snap_extra = {"decouplings": [{"pair": "BTC/SPY", "corr": -0.55, "type": "intermarket"}]}
        p = self._prompt(**snap_extra)
        assert "BTC/SPY" in p

    def test_prompt_includes_rotation(self):
        snap_extra = {"rotations": [{"from": "energy", "to": "tech", "strength": 0.7}]}
        p = self._prompt(**snap_extra)
        assert "energy" in p and "tech" in p

    def test_prompt_boundary_40_percent(self):
        """Confidence exactamente en 0.4 NO debe disparar instrucción (solo < 0.4)."""
        from app.narrative.generator import _UNCERTAINTY_INSTRUCTION
        p = self._prompt(confidence=0.4)
        assert _UNCERTAINTY_INSTRUCTION not in p

    def test_prompt_cold_start_overrides_confidence_check(self):
        """cold_start=True tiene preferencia sobre la instrucción de confidence."""
        from app.narrative.generator import _COLD_START_INSTRUCTION, _UNCERTAINTY_INSTRUCTION
        # Alta confianza pero cold_start=True → debe salir cold_start, no uncertainty
        p = self._prompt(confidence=0.9, cold_start=True)
        assert _COLD_START_INSTRUCTION in p
        assert _UNCERTAINTY_INSTRUCTION not in p


# ══════════════════════════════════════════════════════════════════════════════
# S7-3: Sin búsqueda web — garantía de narrativa cerrada
# ══════════════════════════════════════════════════════════════════════════════

class TestNarrativaNoWebSearch:

    def test_generate_narrative_uses_query_direct_not_web_search(self):
        """
        (a) generate_narrative debe llamar query_direct, nunca query_with_web_search.
        """
        mock_resp = LLMResponse(text="narrativa de prueba", sources=[], engine="groq")

        with patch("app.narrative.generator.query_direct", return_value=mock_resp) as mock_direct, \
             patch("app.exposure.llm_client.query_with_web_search") as mock_web:
            from app.narrative.generator import generate_narrative
            result = generate_narrative(_FULL_SNAPSHOT)

            mock_direct.assert_called_once()
            mock_web.assert_not_called()
            assert result == "narrativa de prueba"

    def _call_groq_direct(self, prompt, system=""):
        """
        Llama a _query_groq_direct con groq mockeado via sys.modules.
        Devuelve (captured_kwargs, LLMResponse).
        """
        import sys
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "texto de prueba"
            return resp

        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = fake_create

        mock_groq_module = MagicMock()
        mock_groq_module.Groq.return_value = fake_client

        with patch.dict(sys.modules, {"groq": mock_groq_module}):
            from app.exposure.llm_client import _query_groq_direct
            result = _query_groq_direct(prompt, system)

        return captured, result

    def test_query_direct_no_compound_beta_model(self):
        """
        (b) _query_groq_direct usa un modelo != compound-beta y no pasa tools.
        """
        captured, _ = self._call_groq_direct("prompt de prueba", "system de prueba")

        model = captured.get("model", "")
        assert model != "compound-beta", (
            f"query_direct NO debe usar compound-beta (tiene web search), usó: '{model}'"
        )
        assert "tools" not in captured, "query_direct NO debe pasar herramientas de búsqueda"

    def test_query_direct_passes_system_prompt(self):
        """query_direct incluye el system prompt como primer mensaje."""
        captured, _ = self._call_groq_direct("user prompt", "mi system prompt")

        messages = captured.get("messages", [])
        assert messages[0]["role"] == "system"
        assert "mi system prompt" in messages[0]["content"]
        assert messages[1]["role"] == "user"

    def test_query_direct_no_system_skips_system_message(self):
        """Sin system, solo debe ir el mensaje de usuario."""
        captured, _ = self._call_groq_direct("sólo usuario", "")

        messages = captured.get("messages", [])
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_generate_narrative_returns_stripped_text(self):
        """La narrativa devuelta no tiene espacios ni saltos de línea al inicio/fin."""
        mock_resp = LLMResponse(text="  texto con espacios  \n", sources=[], engine="groq")
        with patch("app.narrative.generator.query_direct", return_value=mock_resp):
            from app.narrative.generator import generate_narrative
            result = generate_narrative(_FULL_SNAPSHOT)
        assert result == "texto con espacios"


# ══════════════════════════════════════════════════════════════════════════════
# S7-4: engine.py — orquestación, persistencia y errores
# ══════════════════════════════════════════════════════════════════════════════

def _make_engine(generate_text="narrativa ok", regime=_REGIME, scores=None,
                 fail_generate=False):
    """Crea un NarrativeEngine con mocks de BD y función generadora."""
    db = _make_db(
        regimes=[regime] if regime else [],
        flow_scores=scores or _SCORES,
    )

    def _gen(snap):
        if fail_generate:
            raise RuntimeError("Groq caído")
        return generate_text

    from app.narrative.engine import NarrativeEngine
    return NarrativeEngine(db=db, generate_fn=_gen)


class TestNarrativeEngine:

    def test_result_has_all_required_fields(self):
        """El dict resultado tiene todos los campos esperados."""
        engine = _make_engine()
        result = engine.run_sync()
        for field in ("text", "regime", "confidence", "disclaimer", "ts", "errors", "ok"):
            assert field in result, f"Campo '{field}' ausente en resultado"

    def test_disclaimer_always_present(self):
        """(c) El sello de interpretación siempre está en el resultado."""
        from app.narrative.generator import DISCLAIMER
        result = _make_engine().run_sync()
        assert result["disclaimer"] == DISCLAIMER
        assert "no es consejo de inversión" in result["disclaimer"].lower()

    def test_text_populated_on_success(self):
        result = _make_engine(generate_text="Los datos muestran un patrón risk-on.").run_sync()
        assert result["text"] == "Los datos muestran un patrón risk-on."
        assert result["ok"] is True

    def test_regime_extracted_from_snapshot(self):
        result = _make_engine(regime=_REGIME).run_sync()
        assert result["regime"] == "risk_on"
        assert result["confidence"] == pytest.approx(0.85)

    def test_snapshot_json_persisted_in_upsert(self):
        """(e) El upsert debe incluir snapshot_json (auditoría post-hoc)."""
        db = _make_db(regimes=[_REGIME], flow_scores=_SCORES)
        upserted_rows = []

        def capture_upsert(row, on_conflict=None):
            upserted_rows.append(row)
            m = MagicMock()
            m.execute.return_value = MagicMock()
            return m

        db.table("narratives").upsert = capture_upsert

        from app.narrative.engine import NarrativeEngine
        engine = NarrativeEngine(db=db, generate_fn=lambda s: "narrativa")
        engine.run_sync()

        assert upserted_rows, "upsert no fue llamado"
        row = upserted_rows[0]
        assert "snapshot_json" in row, "snapshot_json debe persistirse"
        assert isinstance(row["snapshot_json"], dict), "snapshot_json debe ser dict"
        assert "regime" in row["snapshot_json"], "snapshot_json debe tener clave 'regime'"

    def test_groq_fail_no_persist(self):
        """(h) Si Groq falla, el engine NO llama a upsert y registra el error."""
        db = _make_db(regimes=[_REGIME], flow_scores=_SCORES)

        from app.narrative.engine import NarrativeEngine
        engine = NarrativeEngine(db=db, generate_fn=lambda s: (_ for _ in ()).throw(RuntimeError("Groq down")))
        result = engine.run_sync()

        assert result["ok"] is False
        assert any("Groq down" in e for e in result["errors"])
        # upsert en narratives NO debe haberse llamado con datos
        narratives_mock = db.table("narratives")
        assert not narratives_mock.upsert.called, "No debe llamarse upsert si Groq falla"

    def test_idempotency_uses_on_conflict_ts(self):
        """(g) Idempotencia: el upsert se llama con on_conflict='ts'."""
        db = _make_db(regimes=[_REGIME], flow_scores=_SCORES)
        on_conflict_values = []

        orig_upsert = db.table("narratives").upsert

        def capture_upsert(row, on_conflict=None):
            on_conflict_values.append(on_conflict)
            return orig_upsert(row, on_conflict=on_conflict)

        db.table("narratives").upsert = capture_upsert

        from app.narrative.engine import NarrativeEngine
        engine = NarrativeEngine(db=db, generate_fn=lambda s: "texto")
        engine.run_sync()
        engine.run_sync()

        assert on_conflict_values, "upsert debería haberse llamado"
        assert all(v == "ts" for v in on_conflict_values), (
            f"on_conflict debe ser 'ts', obtenido: {on_conflict_values}"
        )

    def test_upsert_row_includes_llm_engine(self):
        """El row persistido incluye llm_engine='groq'."""
        db = _make_db(regimes=[_REGIME], flow_scores=_SCORES)
        upserted = []

        def capture(row, on_conflict=None):
            upserted.append(row)
            m = MagicMock()
            m.execute.return_value = MagicMock()
            return m

        db.table("narratives").upsert = capture
        from app.narrative.engine import NarrativeEngine
        NarrativeEngine(db=db, generate_fn=lambda s: "x").run_sync()

        assert upserted[0].get("llm_engine") == "groq"

    def test_no_regime_data_runs_ok(self):
        """Sin datos de régimen en BD, el engine no lanza excepción."""
        engine = _make_engine(regime=None)
        result = engine.run_sync()
        # El régimen fallback es neutral
        assert result["regime"] in ("neutral", "desconocido", "")
        assert result["ok"] is True

    def test_result_ok_false_when_generate_fails(self):
        engine = _make_engine(fail_generate=True)
        result = engine.run_sync()
        assert result["ok"] is False
        assert len(result["errors"]) > 0
        assert result["text"] == ""   # no debe haber texto basura


# ══════════════════════════════════════════════════════════════════════════════
# S7-5: Integración snapshot → prompt → engine
# ══════════════════════════════════════════════════════════════════════════════

class TestNarrativeIntegration:

    def test_low_regime_confidence_reflected_in_prompt_passed_to_llm(self):
        """
        (f) Confianza baja en snapshot → el prompt que se pasa al generador
        debe incluir instrucción explícita de incertidumbre.
        """
        low_regime = {**_REGIME, "confidence": 0.2, "regime": "neutral"}
        db = _make_db(regimes=[low_regime], flow_scores=_SCORES)

        prompts_seen = []

        def capturing_gen(snap):
            from app.narrative.generator import build_prompt
            p = build_prompt(snap)
            prompts_seen.append(p)
            return "narrativa"

        from app.narrative.engine import NarrativeEngine
        NarrativeEngine(db=db, generate_fn=capturing_gen).run_sync()

        assert prompts_seen, "El generador debe haber sido llamado"
        prompt = prompts_seen[0]
        assert "20%" in prompt or "insuficiente" in prompt.lower() or "baja" in prompt.lower()

    def test_cold_start_reflected_in_prompt_passed_to_llm(self):
        """
        (f) cold_start=True → el prompt incluye instrucción de datos preliminares.
        """
        low_scores = [_score(i, f"T{i}", "equity", 0.1, "low") for i in range(6)]
        db = _make_db(regimes=[_REGIME], flow_scores=low_scores)

        prompts_seen = []

        def capturing_gen(snap):
            from app.narrative.generator import build_prompt
            p = build_prompt(snap)
            prompts_seen.append(p)
            return "narrativa"

        from app.narrative.engine import NarrativeEngine
        NarrativeEngine(db=db, generate_fn=capturing_gen).run_sync()

        assert prompts_seen
        assert "cold_start" in prompts_seen[0] or "preliminar" in prompts_seen[0].lower()

    def test_full_pipeline_structure(self):
        """Ciclo completo snapshot→generar→persistir devuelve estructura válida."""
        db = _make_db(
            regimes=[_REGIME],
            flow_scores=_SCORES,
            correlations=[_DECOUPLING],
            rotations=[_ROTATION],
            exposures=[_EXPOSURE],
        )
        from app.narrative.engine import NarrativeEngine
        result = NarrativeEngine(
            db=db,
            generate_fn=lambda s: "Los datos sugieren un entorno risk-on.",
        ).run_sync()

        assert result["ok"] is True
        assert result["text"] == "Los datos sugieren un entorno risk-on."
        assert result["regime"] == "risk_on"
        assert result["disclaimer"] != ""
        assert result["ts"] != ""


# ══════════════════════════════════════════════════════════════════════════════
# S7-6: sistema_prompt tiene las prohibiciones correctas
# ══════════════════════════════════════════════════════════════════════════════

class TestSystemPromptContent:

    def _sys(self):
        from app.narrative.generator import _SYSTEM_PROMPT
        return _SYSTEM_PROMPT

    def test_prohibits_price_predictions(self):
        s = self._sys().lower()
        assert "va a subir" in s or "prediccion" in s or "predicciones" in s

    def test_prohibits_investment_advice(self):
        s = self._sys().lower()
        assert "comprar" in s or "vender" in s or "acumular" in s

    def test_prohibits_external_causes(self):
        s = self._sys()
        assert "fed" in s.lower() or "causas externas" in s.lower() or "no estén en los datos" in s

    def test_prohibits_certainty_language(self):
        s = self._sys().lower()
        assert "definitivamente" in s or "certeza" in s or "seguro que" in s

    def test_requires_observational_language(self):
        s = self._sys().lower()
        assert "los datos muestran" in s or "sugiere" in s or "observacional" in s

    def test_requires_uncertainty_marking(self):
        s = self._sys().lower()
        assert "confianza" in s and ("baja" in s or "insuficiente" in s or "limitada" in s)

    def test_requires_spanish(self):
        s = self._sys().lower()
        assert "español" in s
