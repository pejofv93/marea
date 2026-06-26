"""
Tests MAREA Bloque 3 — Inteligencia INTRADÍA de sesión.

Garantías verificadas (entregables):
  (a) CIERRE-JUEZ: veredicto confirmado / revertido / agotado según el caso.
  (b) GIROS: solo en activos con movimiento FUERTE en al menos un momento;
      ignora giros de activos planos.
  (c) VELOCIDAD: acelera / frena / estable correctamente entre momentos.
  (d) AUTO-ACTIVACIÓN: con < MIN_MOMENTS momentos del día, las tres degradan con
      elegancia sin romper el parte.
  (e) Usa el score PENALIZADO por credibilidad: un flujo base FOGONAZO se OMITE.
  (f) NO aplica a termómetros (^VIX, CRYPTO_FNG).
  (g) Integración en el digest: bloques ⚖️ Veredicto / 🔄 Giros / ⚡ Ritmo.
  (h) NINGÚN test hace llamadas reales (ni Telegram, ni BD).
"""

from unittest.mock import MagicMock, patch

from app.alerts import digest
from app.alerts.digest import (
    build_intraday_digest,
    render_giros_block,
    render_ritmo_block,
    render_verdict_block,
    send_intraday_digest,
)
from app.analysis import intraday_session as iss
from app.analysis.intraday_session import (
    MIN_MOMENTS,
    SessionAnalysis,
    analyze_session,
    classify_velocity,
    classify_verdict,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _asset(ticker, score, asset_class="etf", credibility_label=None):
    return {"ticker": ticker, "score": score, "asset_class": asset_class,
            "credibility_label": credibility_label}


def _moment(name, assets):
    return {"moment": name, "assets": assets}


# ══════════════════════════════════════════════════════════════════════════════
# Clasificadores PUROS
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyVerdict:

    def test_confirmado_misma_direccion_mantiene_fuerza(self):
        assert classify_verdict(0.80, 0.75) == "confirmado"
        assert classify_verdict(0.80, 0.95) == "confirmado"   # intensifica → confirmado
        assert classify_verdict(-0.80, -0.70) == "confirmado"

    def test_revertido_cambio_de_signo_vivo(self):
        assert classify_verdict(0.80, -0.60) == "revertido"
        assert classify_verdict(-0.85, 0.50) == "revertido"

    def test_agotado_misma_direccion_pierde_fuelle(self):
        # 0.30 < 0.80 × 0.5 (=0.40) → agotado
        assert classify_verdict(0.80, 0.30) == "agotado"
        assert classify_verdict(-0.90, -0.30) == "agotado"

    def test_agotado_flujo_apagado_aunque_cambie_de_signo(self):
        # Cierre casi plano: NO es una reversión real, el flujo se apagó.
        assert classify_verdict(0.80, -0.05) == "agotado"
        assert classify_verdict(0.80, 0.10) == "agotado"


class TestClassifyVelocity:

    def test_acelera(self):
        assert classify_velocity(0.40, 0.70) == "acelera"
        assert classify_velocity(-0.40, -0.80) == "acelera"   # salida intensificándose

    def test_frena(self):
        assert classify_velocity(0.80, 0.50) == "frena"
        assert classify_velocity(-0.80, -0.55) == "frena"

    def test_estable(self):
        assert classify_velocity(0.60, 0.65) == "estable"     # |Δ| < 0.10
        assert classify_velocity(-0.50, -0.50) == "estable"


# ══════════════════════════════════════════════════════════════════════════════
# (a) Cierre como juez del día — veredicto
# ══════════════════════════════════════════════════════════════════════════════

class TestVeredicto:

    def test_confirmado_end_to_end(self):
        prior = [_moment("apertura", [_asset("XLK", 0.85)])]
        current = [_asset("XLK", 0.80)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdict_ready is True
        assert len(sa.verdicts) == 1
        v = sa.verdicts[0]
        assert v.ticker == "XLK" and v.verdict == "confirmado"
        assert v.early_moment == "apertura"

    def test_revertido_end_to_end(self):
        prior = [_moment("apertura", [_asset("TSLA", 0.80)])]
        current = [_asset("TSLA", -0.65)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts[0].verdict == "revertido"

    def test_agotado_end_to_end(self):
        prior = [_moment("apertura", [_asset("GLD", 0.90)])]
        current = [_asset("GLD", 0.25)]   # 0.25 < 0.90×0.5 → agotado
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts[0].verdict == "agotado"

    def test_agotado_cuando_el_activo_desaparece_al_cierre(self):
        # Estaba fuerte en apertura y ya no aparece al cierre → flujo apagado.
        prior = [_moment("apertura", [_asset("SOXX", 0.85)])]
        current = [_asset("BTC", 0.30, "crypto")]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts[0].ticker == "SOXX"
        assert sa.verdicts[0].verdict == "agotado"
        assert sa.verdicts[0].close_score == 0.0

    def test_solo_dictamina_sobre_flujos_fuertes(self):
        # Apertura floja (<STRONG_MOVE) → no se dictamina veredicto sobre ella.
        prior = [_moment("apertura", [_asset("XLK", 0.30)])]
        current = [_asset("XLK", 0.10)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdict_ready is True
        assert sa.verdicts == []

    def test_usa_media_si_no_hay_apertura(self):
        prior = [_moment("media", [_asset("XLK", 0.80)])]
        current = [_asset("XLK", 0.75)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts[0].early_moment == "media"
        assert sa.verdicts[0].verdict == "confirmado"

    def test_prefiere_apertura_sobre_media(self):
        prior = [
            _moment("apertura", [_asset("XLK", 0.85)]),
            _moment("media", [_asset("XLK", 0.40)]),
        ]
        current = [_asset("XLK", 0.80)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts[0].early_moment == "apertura"


# ══════════════════════════════════════════════════════════════════════════════
# (b) Giros intradía
# ══════════════════════════════════════════════════════════════════════════════

class TestGiros:

    def test_giro_detectado_en_activo_fuerte(self):
        prior = [_moment("apertura", [_asset("TSLA", 0.80)])]
        current = [_asset("TSLA", -0.55)]
        sa = analyze_session(prior, "media", current)
        assert len(sa.giros) == 1
        g = sa.giros[0]
        assert g.ticker == "TSLA"
        assert g.prev_score == 0.80 and g.now_score == -0.55

    def test_ignora_giro_de_activo_plano(self):
        # Ambos lados débiles (<STRONG_MOVE) aunque cambie el signo → no es giro.
        prior = [_moment("apertura", [_asset("XLV", 0.25)])]
        current = [_asset("XLV", -0.30)]
        sa = analyze_session(prior, "media", current)
        assert sa.giros == []

    def test_ignora_giro_si_la_nueva_direccion_es_casi_plana(self):
        # Entraba fuerte, ahora ~0: el "ahora sale" no sería real → no es giro.
        prior = [_moment("apertura", [_asset("SOXX", 0.80)])]
        current = [_asset("SOXX", -0.05)]
        sa = analyze_session(prior, "media", current)
        assert sa.giros == []

    def test_sin_cambio_de_signo_no_hay_giro(self):
        prior = [_moment("apertura", [_asset("XLK", 0.80)])]
        current = [_asset("XLK", 0.40)]   # baja pero sigue entrando
        sa = analyze_session(prior, "media", current)
        assert sa.giros == []

    def test_giro_compara_los_dos_ultimos_momentos(self):
        # apertura→media sin giro, media→cierre con giro: el de cierre se reporta.
        prior = [
            _moment("apertura", [_asset("TSLA", 0.70)]),
            _moment("media", [_asset("TSLA", 0.75)]),
        ]
        current = [_asset("TSLA", -0.60)]
        sa = analyze_session(prior, "cierre", current)
        assert len(sa.giros) == 1
        assert sa.giros[0].prev_moment == "media"
        assert sa.giros[0].now_moment == "cierre"


# ══════════════════════════════════════════════════════════════════════════════
# (c) Velocidad del flujo — ritmo
# ══════════════════════════════════════════════════════════════════════════════

class TestRitmo:

    def test_entrada_acelerandose(self):
        prior = [_moment("apertura", [_asset("GLD", 0.40)])]
        current = [_asset("GLD", 0.75)]
        sa = analyze_session(prior, "media", current)
        r = [x for x in sa.ritmo if x.ticker == "GLD"][0]
        assert r.direction == "entrada" and r.trend == "acelera"

    def test_entrada_pierde_fuelle(self):
        prior = [_moment("apertura", [_asset("XLK", 0.80)])]
        current = [_asset("XLK", 0.45)]
        sa = analyze_session(prior, "media", current)
        r = [x for x in sa.ritmo if x.ticker == "XLK"][0]
        assert r.direction == "entrada" and r.trend == "frena"

    def test_estable_no_se_muestra(self):
        prior = [_moment("apertura", [_asset("XLK", 0.60)])]
        current = [_asset("XLK", 0.65)]   # |Δ| < 0.10 → estable → no en ritmo
        sa = analyze_session(prior, "media", current)
        assert [x for x in sa.ritmo if x.ticker == "XLK"] == []

    def test_giro_no_aparece_en_ritmo(self):
        # Un cambio de signo es un GIRO, no un dato de ritmo.
        prior = [_moment("apertura", [_asset("TSLA", 0.80)])]
        current = [_asset("TSLA", -0.60)]
        sa = analyze_session(prior, "media", current)
        assert [x for x in sa.ritmo if x.ticker == "TSLA"] == []
        assert len(sa.giros) == 1

    def test_flujo_actual_plano_no_tiene_ritmo(self):
        prior = [_moment("apertura", [_asset("XLV", 0.50)])]
        current = [_asset("XLV", 0.10)]   # actual < RELEVANT_EPS → sin ritmo
        sa = analyze_session(prior, "media", current)
        assert [x for x in sa.ritmo if x.ticker == "XLV"] == []


# ══════════════════════════════════════════════════════════════════════════════
# (d) Auto-activación — degradación elegante
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoActivacion:

    def test_un_solo_momento_no_dictamina_ni_gira_ni_ritmo(self):
        sa = analyze_session([], "apertura", [_asset("XLK", 0.85)])
        assert sa.n_moments == 1
        assert sa.n_moments < MIN_MOMENTS
        assert sa.verdicts == [] and sa.giros == [] and sa.ritmo == []
        assert sa.verdict_ready is False

    def test_cierre_sin_momentos_previos_no_esta_listo(self):
        # Parte de cierre pero sin apertura/media del día → no hay con qué juzgar.
        sa = analyze_session([], "cierre", [_asset("XLK", 0.85)])
        assert sa.verdict_ready is False
        assert sa.verdicts == []

    def test_momento_actual_sustituye_al_previo_mismo_momento(self):
        # Si por reintento llega de nuevo 'apertura', el actual manda (no duplica).
        prior = [_moment("apertura", [_asset("XLK", 0.10)])]
        sa = analyze_session(prior, "apertura", [_asset("XLK", 0.90)])
        assert sa.n_moments == 1   # sigue siendo un único momento


# ══════════════════════════════════════════════════════════════════════════════
# (e) Score penalizado por credibilidad — los fogonazos se omiten
# ══════════════════════════════════════════════════════════════════════════════

class TestCredibilidad:

    def test_veredicto_omite_flujo_base_fogonazo(self):
        prior = [_moment("apertura", [_asset("XLK", 0.85, credibility_label="fogonazo")])]
        current = [_asset("XLK", 0.80)]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts == []   # no se dictamina sobre un fogonazo

    def test_giro_omite_si_el_lado_fuerte_es_fogonazo(self):
        prior = [_moment("apertura", [_asset("TSLA", 0.80, credibility_label="fogonazo")])]
        current = [_asset("TSLA", -0.55)]
        sa = analyze_session(prior, "media", current)
        assert sa.giros == []

    def test_ritmo_omite_flujo_actual_fogonazo(self):
        prior = [_moment("apertura", [_asset("GLD", 0.40)])]
        current = [_asset("GLD", 0.80, credibility_label="fogonazo")]
        sa = analyze_session(prior, "media", current)
        assert [x for x in sa.ritmo if x.ticker == "GLD"] == []

    def test_dudoso_no_se_omite(self):
        # 'dudoso' sigue siendo un flujo creíble a medias; solo el fogonazo se descarta.
        prior = [_moment("apertura", [_asset("XLK", 0.85, credibility_label="dudoso")])]
        current = [_asset("XLK", 0.80, credibility_label="dudoso")]
        sa = analyze_session(prior, "cierre", current)
        assert len(sa.verdicts) == 1


# ══════════════════════════════════════════════════════════════════════════════
# (f) No aplica a termómetros
# ══════════════════════════════════════════════════════════════════════════════

class TestExcluyeTermometros:

    def test_vix_y_fng_no_generan_veredicto_ni_giro_ni_ritmo(self):
        prior = [_moment("apertura", [
            _asset("^VIX", 0.90, "macro"),
            _asset("CRYPTO_FNG", -0.85, "crypto"),
        ])]
        current = [
            _asset("^VIX", -0.80, "macro"),       # giro aparente en el VIX → debe ignorarse
            _asset("CRYPTO_FNG", -0.30, "crypto"),
        ]
        sa = analyze_session(prior, "cierre", current)
        assert sa.verdicts == []
        assert sa.giros == []
        assert sa.ritmo == []


# ══════════════════════════════════════════════════════════════════════════════
# (g) Render — bloques del digest
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderVeredicto:

    def _sa_with(self, **kw):
        base = dict(n_moments=2, moments_present=["apertura", "cierre"], verdict_ready=True)
        base.update(kw)
        return SessionAnalysis(**base)

    def test_no_close_no_block(self):
        sa = self._sa_with()
        assert render_verdict_block(sa, is_close=False) == []

    def test_degrada_sin_momentos(self):
        sa = SessionAnalysis(n_moments=1, moments_present=["cierre"], verdict_ready=False)
        lines = render_verdict_block(sa, is_close=True)
        assert "Veredicto del día" in lines[0]
        assert any("Sin suficientes momentos" in l for l in lines)

    def test_listo_sin_veredictos(self):
        sa = self._sa_with(verdicts=[])
        lines = render_verdict_block(sa, is_close=True)
        assert any("Sin flujos fuertes" in l for l in lines)

    def test_confirmado_revertido_agotado_en_texto(self):
        sa = self._sa_with(verdicts=[
            iss.Verdict("XLK", "etf", "confirmado", "apertura", 0.85, 0.80),
            iss.Verdict("TSLA", "etf", "revertido", "apertura", 0.80, -0.60),
            iss.Verdict("GLD", "etf", "agotado", "apertura", 0.90, 0.25),
        ])
        text = "\n".join(render_verdict_block(sa, is_close=True))
        assert "Tecnología" in text and "CONFIRMADO" in text
        assert "REVERTIDO" in text and "ahora sale" in text
        assert "AGOTADO" in text and "perdió fuelle" in text


class TestRenderGirosRitmo:

    def test_giros_block_nombra_activo_y_dos_direcciones(self):
        sa = SessionAnalysis(n_moments=2, moments_present=["apertura", "media"],
                             giros=[iss.Giro("TSLA", "etf", "apertura", "media", 0.80, -0.55)])
        text = "\n".join(render_giros_block(sa))
        assert "🔄 <b>Giros:</b>" in text
        assert "entraba en la apertura" in text and "ahora sale" in text
        assert "dado la vuelta" in text

    def test_giros_vacio_no_muestra_nada(self):
        sa = SessionAnalysis(n_moments=2)
        assert render_giros_block(sa) == []

    def test_ritmo_block_acelera_y_frena(self):
        sa = SessionAnalysis(n_moments=2,
                             ritmo=[
                                 iss.Ritmo("GLD", "etf", "entrada", "acelera", 0.40, 0.75),
                                 iss.Ritmo("XLK", "etf", "salida", "frena", -0.80, -0.45),
                             ])
        text = "\n".join(render_ritmo_block(sa))
        assert "⚡ <b>Ritmo:</b>" in text
        assert "la entrada en Oro (GLD) se acelera" in text
        assert "la salida en Tecnología pierde fuelle" in text

    def test_ritmo_vacio_no_muestra_nada(self):
        assert render_ritmo_block(SessionAnalysis(n_moments=2)) == []


# ══════════════════════════════════════════════════════════════════════════════
# build_intraday_digest — splice de los bloques de sesión
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildIntradayIntegration:

    def test_incluye_giros_ritmo_y_veredicto(self):
        assets = [{"ticker": "XLK", "name": None, "asset_class": "etf",
                   "sector": None, "score": 0.7, "confidence": "ok"}]
        text = build_intraday_digest(
            {"assets": assets}, moment="Tarde USA (intradía)",
            giros_lines=["🔄 <b>Giros:</b>", "  • Tesla entraba…"],
            ritmo_lines=["⚡ <b>Ritmo:</b>", "  • la entrada en Oro se acelera…"],
            verdict_lines=["⚖️ <b>Veredicto del día:</b>", "  • …CONFIRMADO."],
        )
        assert "🔄 <b>Giros:</b>" in text
        assert "⚡ <b>Ritmo:</b>" in text
        assert "⚖️ <b>Veredicto del día:</b>" in text

    def test_sin_bloques_de_sesion_no_aparecen(self):
        assets = [{"ticker": "XLK", "name": None, "asset_class": "etf",
                   "sector": None, "score": 0.7, "confidence": "ok"}]
        text = build_intraday_digest({"assets": assets}, moment="Apertura USA (intradía)")
        assert "Veredicto del día" not in text
        assert "🔄 <b>Giros:</b>" not in text
        assert "⚡ <b>Ritmo:</b>" not in text


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de BD (todo mockeado)
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadTodayMoments:

    def test_excluye_momento_actual_y_parsea_scores(self):
        db = MagicMock()
        db.table().select().eq().eq().execute.return_value.data = [
            {"moment": "apertura", "scores": [
                {"ticker": "XLK", "score": 0.8, "asset_class": "etf", "credibility_label": "confirmado"}]},
            {"moment": "cierre", "scores": []},   # se excluye (momento actual)
        ]
        out = digest._load_today_moments(db, "intraday", exclude_moment="cierre")
        assert len(out) == 1
        assert out[0]["moment"] == "apertura"
        assert out[0]["assets"][0]["ticker"] == "XLK"
        assert out[0]["assets"][0]["credibility_label"] == "confirmado"

    def test_error_de_bd_degrada_a_lista_vacia(self):
        db = MagicMock()
        db.table.side_effect = RuntimeError("db caída")
        assert digest._load_today_moments(db, "intraday") == []


# ══════════════════════════════════════════════════════════════════════════════
# send_intraday_digest — extremo a extremo con momentos del día
# ══════════════════════════════════════════════════════════════════════════════

def _intraday_analysis(*movs):
    return {"movements": [
        {"ticker": t, "asset_class": c, "score": s, "confidence": "ok",
         "credibility_label": cl}
        for (t, s, c, cl) in movs
    ], "strong_inflow": [], "strong_outflow": [], "errors": [], "ok": True}


class TestSendIntradayWithSession:

    def test_cierre_emite_veredicto_confirmado(self):
        send_fn = MagicMock(return_value=True)
        prior = [_moment("apertura", [_asset("XLK", 0.85)])]
        analysis = _intraday_analysis(("XLK", 0.80, "etf", None))
        with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
             patch("app.alerts.digest._save_cycle"), \
             patch("app.alerts.digest._load_today_moments", return_value=prior):
            res = send_intraday_digest(db=MagicMock(), analysis=analysis, hour_utc=20, send_fn=send_fn)
        assert res["ok"] is True and res["sent"] is True
        text = send_fn.call_args[0][0]
        assert "⚖️ <b>Veredicto del día:</b>" in text
        assert "CONFIRMADO" in text

    def test_media_emite_giro_y_no_veredicto(self):
        send_fn = MagicMock(return_value=True)
        prior = [_moment("apertura", [_asset("TSLA", 0.80)])]
        analysis = _intraday_analysis(("TSLA", -0.60, "etf", None))
        with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
             patch("app.alerts.digest._save_cycle"), \
             patch("app.alerts.digest._load_today_moments", return_value=prior):
            res = send_intraday_digest(db=MagicMock(), analysis=analysis, hour_utc=16, send_fn=send_fn)
        text = send_fn.call_args[0][0]
        assert "🔄 <b>Giros:</b>" in text
        assert "Veredicto del día" not in text   # el veredicto es solo del cierre

    def test_apertura_degrada_sin_romper(self):
        # Primer parte del día: sin momentos previos → sin giros/ritmo/veredicto.
        send_fn = MagicMock(return_value=True)
        analysis = _intraday_analysis(("XLK", 0.85, "etf", None))
        with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
             patch("app.alerts.digest._save_cycle"), \
             patch("app.alerts.digest._load_today_moments", return_value=[]):
            res = send_intraday_digest(db=MagicMock(), analysis=analysis, hour_utc=13, send_fn=send_fn)
        assert res["ok"] is True and res["sent"] is True
        text = send_fn.call_args[0][0]
        assert "🔄 <b>Giros:</b>" not in text
        assert "⚡ <b>Ritmo:</b>" not in text
