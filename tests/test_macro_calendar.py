"""
Tests MAREA Bloque 5 — Calendario macro (el "por qué" del día).

Garantías verificadas (entregables):
  (a) Eventos del DÍA correctos (FOMC, BCE, IPC, empleo, PCE, PIB) en hora de Madrid.
  (b) Conversión horaria con DST, incluidas las semanas de DESFASE EE.UU./UE
      (IPC 11-mar → 13:30 Madrid, no 14:30; FOMC 18-mar/28-oct → 19:00).
  (c) Solo ALTO IMPACTO USA/eurozona; tipos no admitidos se ignoran.
  (d) Día SIN eventos → el bloque no aparece.
  (e) Fallo de la fuente → degradación elegante (lista vacía, nunca lanza).
  (f) Integración en el digest: parte diario (siempre) y apertura intradía.
  (g) NINGÚN test hace llamadas reales (ni red, ni Telegram, ni BD).
"""

import datetime as dt
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.alerts import digest
from app.alerts.digest import (
    build_daily_digest,
    build_intraday_digest,
    render_macro_block,
    send_intraday_digest,
)
from app.analysis import macro_calendar as mc
from app.analysis.macro_calendar import (
    HIGH_IMPACT_KINDS,
    KIND_LABEL,
    MACRO_EVENTS,
    events_on,
    todays_macro_events,
)

MAD = ZoneInfo("Europe/Madrid")


# ══════════════════════════════════════════════════════════════════════════════
# (a) Eventos del día correctos
# ══════════════════════════════════════════════════════════════════════════════

class TestEventsOn:

    def test_fomc_day(self):
        evs = events_on(dt.date(2026, 7, 29))
        assert [e.kind for e in evs] == ["fomc"]
        assert evs[0].region == "US"
        assert evs[0].label == "decisión de tipos de la Fed"

    def test_multi_event_day_sorted_by_time(self):
        # 30-abr-2026: BCE (14:15) + PCE (14:30) + PIB avance (14:30).
        evs = events_on(dt.date(2026, 4, 30))
        kinds = [e.kind for e in evs]
        assert set(kinds) == {"ecb", "pce", "gdp"}
        # Ordenados por hora: BCE (14:15) antes que los de las 14:30.
        assert kinds[0] == "ecb"
        assert evs[0].time_madrid == "14:15"

    def test_day_without_events_is_empty(self):
        assert events_on(dt.date(2026, 7, 4)) == []   # sábado sin datos de primer orden

    def test_ecb_region_is_eurozone(self):
        evs = events_on(dt.date(2026, 6, 11))   # BCE
        assert evs and evs[0].region == "EZ"


# ══════════════════════════════════════════════════════════════════════════════
# (b) Conversión horaria con DST (lo delicado)
# ══════════════════════════════════════════════════════════════════════════════

class TestDSTConversion:

    def test_us_830_normal_is_1430_madrid(self):
        # IPC 10-jun: EE.UU. y UE ambos en horario de verano → 8:30 ET = 14:30 Madrid.
        evs = events_on(dt.date(2026, 6, 10))
        assert evs[0].kind == "cpi" and evs[0].time_madrid == "14:30"

    def test_us_830_dst_mismatch_march_is_1330(self):
        # IPC 11-mar: EE.UU. ya en verano (desde 8-mar), UE aún no (hasta 29-mar)
        # → desfase de 1h → 8:30 ET = 13:30 Madrid (no 14:30).
        evs = events_on(dt.date(2026, 3, 11))
        assert evs[0].kind == "cpi" and evs[0].time_madrid == "13:30"

    def test_fomc_normal_is_2000_madrid(self):
        evs = events_on(dt.date(2026, 7, 29))
        assert evs[0].time_madrid == "20:00"   # 14:00 ET = 20:00 Madrid (verano alineado)

    def test_fomc_dst_mismatch_is_1900(self):
        # FOMC 18-mar y 28-oct caen en las semanas de desfase → 14:00 ET = 19:00 Madrid.
        assert events_on(dt.date(2026, 3, 18))[0].time_madrid == "19:00"
        assert events_on(dt.date(2026, 10, 28))[0].time_madrid == "19:00"

    def test_fomc_winter_is_2000(self):
        # Diciembre: ambos en horario estándar → alineados → 20:00.
        assert events_on(dt.date(2026, 12, 9))[0].time_madrid == "20:00"

    def test_ecb_always_1415_madrid(self):
        # BCE (Fráncfort) comparte huso con Madrid → 14:15 todo el año.
        for d in (dt.date(2026, 3, 19), dt.date(2026, 7, 23), dt.date(2026, 12, 17)):
            assert events_on(d)[0].time_madrid == "14:15"


# ══════════════════════════════════════════════════════════════════════════════
# (c) Solo alto impacto USA/eurozona
# ══════════════════════════════════════════════════════════════════════════════

class TestHighImpactOnly:

    def test_table_only_high_impact_kinds_and_regions(self):
        for d, hhmm, tz, kind in MACRO_EVENTS:
            assert kind in HIGH_IMPACT_KINDS
        regions = {("EZ" if k == "ecb" else "US") for *_, k in MACRO_EVENTS}
        assert regions <= {"US", "EZ"}

    def test_unknown_kind_is_ignored(self):
        # Una fuente con un evento menor (no admitido) → se filtra.
        table = [
            ("2026-07-29", "14:00", "America/New_York", "fomc"),
            ("2026-07-29", "10:00", "America/New_York", "retail_sales"),  # menor → ignorar
        ]
        evs = events_on(dt.date(2026, 7, 29), table=table)
        assert [e.kind for e in evs] == ["fomc"]

    def test_expected_counts_in_table(self):
        kinds = [k for *_, k in MACRO_EVENTS]
        assert kinds.count("fomc") == 8
        assert kinds.count("ecb") == 7
        assert kinds.count("cpi") == 12
        assert kinds.count("nfp") == 12
        assert kinds.count("pce") == 12
        assert kinds.count("gdp") == 4   # solo el primer avance de cada trimestre


# ══════════════════════════════════════════════════════════════════════════════
# todays_macro_events — "hoy" en hora de Madrid
# ══════════════════════════════════════════════════════════════════════════════

class TestTodaysEvents:

    def test_today_with_event(self):
        now = datetime(2026, 7, 29, 10, 0, tzinfo=MAD)
        evs = todays_macro_events(now=now)
        assert [e.kind for e in evs] == ["fomc"]

    def test_today_from_utc_now(self):
        # now en UTC: se normaliza a fecha de Madrid igual.
        now = datetime(2026, 7, 29, 8, 0, tzinfo=ZoneInfo("UTC"))
        assert [e.kind for e in todays_macro_events(now=now)] == ["fomc"]

    def test_today_without_events(self):
        now = datetime(2026, 7, 4, 12, 0, tzinfo=MAD)
        assert todays_macro_events(now=now) == []

    def test_stale_table_future_year_no_events_no_crash(self):
        # Año posterior al último curado: sin eventos, no rompe (solo log de aviso).
        now = datetime(2027, 6, 1, 12, 0, tzinfo=MAD)
        assert todays_macro_events(now=now) == []


# ══════════════════════════════════════════════════════════════════════════════
# (e) Degradación elegante
# ══════════════════════════════════════════════════════════════════════════════

class TestDegradation:

    def test_never_raises_on_internal_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("tz roto")
        monkeypatch.setattr(mc, "events_on", boom)
        # No debe propagar: degrada a [].
        assert todays_macro_events(now=datetime(2026, 7, 29, 10, 0, tzinfo=MAD)) == []

    def test_macro_lines_helper_swallows_errors(self, monkeypatch):
        monkeypatch.setattr(
            "app.analysis.macro_calendar.todays_macro_events",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert digest._macro_lines() == []


# ══════════════════════════════════════════════════════════════════════════════
# (d)(f) Render + integración en el digest
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderAndDigest:

    def test_render_block_format(self):
        evs = events_on(dt.date(2026, 7, 29))   # FOMC
        lines = render_macro_block(evs)
        text = "\n".join(lines)
        assert "📅 <b>Agenda macro de hoy:</b>" in text
        assert "20:00" in text
        assert "decisión de tipos de la Fed" in text
        assert "suele traer volatilidad" in text   # condicional, no predice precio

    def test_render_empty_no_block(self):
        assert render_macro_block([]) == []

    def test_daily_digest_includes_macro_when_present(self):
        state = {"assets": [{"ticker": "^GSPC", "name": None, "asset_class": "index",
                             "sector": None, "score": 0.6, "confidence": "ok"}],
                 "regime": {"name": "risk_on", "confidence": 0.8, "signals": []},
                 "cold_start": False, "rotations": []}
        text = build_daily_digest(state, macro_lines=["📅 <b>Agenda macro de hoy:</b>", "  • 14:30 IPC"])
        assert "📅 <b>Agenda macro de hoy:</b>" in text

    def test_daily_digest_absent_when_no_macro(self):
        state = {"assets": [{"ticker": "^GSPC", "name": None, "asset_class": "index",
                             "sector": None, "score": 0.6, "confidence": "ok"}],
                 "regime": {"name": "risk_on", "confidence": 0.8, "signals": []},
                 "cold_start": False, "rotations": []}
        assert "Agenda macro" not in build_daily_digest(state)

    def test_intraday_digest_includes_macro_when_present(self):
        text = build_intraday_digest({"assets": [{"ticker": "BTC", "asset_class": "crypto",
                                                  "score": 0.5, "confidence": "ok"}]},
                                     moment="Apertura USA (intradía)",
                                     macro_lines=["📅 <b>Agenda macro de hoy:</b>", "  • 20:00 Fed"])
        assert "📅 <b>Agenda macro de hoy:</b>" in text


# ══════════════════════════════════════════════════════════════════════════════
# (f) Cableado de los envíos — agenda solo en apertura intradía
# ══════════════════════════════════════════════════════════════════════════════

class TestSenderWiring:

    def _analysis(self):
        return {"movements": [], "strong_inflow": [], "strong_outflow": [], "errors": [], "ok": True}

    def test_intraday_apertura_includes_macro(self):
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
             patch("app.alerts.digest._load_today_moments", return_value=[]), \
             patch("app.alerts.digest._save_cycle"), \
             patch("app.alerts.digest._load_context_lines", return_value=[]), \
             patch("app.alerts.digest._macro_lines", return_value=["📅 <b>Agenda macro de hoy:</b>", "  • 20:00 Fed"]):
            send_intraday_digest(db=MagicMock(), analysis=self._analysis(), hour_utc=13, send_fn=send_fn)
        assert "📅 <b>Agenda macro de hoy:</b>" in send_fn.call_args[0][0]

    def test_intraday_media_excludes_macro(self):
        send_fn = MagicMock(return_value=True)
        with patch("app.alerts.digest._load_prev_cycle", return_value=None), \
             patch("app.alerts.digest._load_today_moments", return_value=[]), \
             patch("app.alerts.digest._save_cycle"), \
             patch("app.alerts.digest._load_context_lines", return_value=[]), \
             patch("app.alerts.digest._macro_lines", return_value=["📅 <b>Agenda macro de hoy:</b>"]):
            send_intraday_digest(db=MagicMock(), analysis=self._analysis(), hour_utc=16, send_fn=send_fn)
        # En media sesión NO se incluye la agenda (aunque _macro_lines tenga datos).
        assert "Agenda macro" not in send_fn.call_args[0][0]
