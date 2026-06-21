"""
MAREA — Dashboard de flujos de liquidez intermercado.

Solo-lectura: lee de Supabase, no recalcula ni dispara ingestas.
Ejecutar desde la raíz del proyecto:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
import os

# Garantiza que la raíz del proyecto esté en sys.path cuando Streamlit
# ejecuta este script desde dashboard/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import pandas as pd

from dashboard.data import (
    load_regime_current,
    load_regime_history,
    load_latest_narrative,
    load_flow_scores,
    load_correlations,
    load_rotations,
    load_exposures,
    load_alerts,
)
from dashboard.components import (
    DISCLAIMER,
    REGIME_LABEL,
    REGIME_COLOR,
    confidence_badge_html,
    regime_badge_html,
    low_conf_note_html,
    empty_state,
    disclaimer_box,
    flow_score_chart,
    corr_heatmap,
    regime_timeline_chart,
)

# ── Configuración de página ───────────────────────────────────────────────────

st.set_page_config(
    page_title="MAREA — Monitor de Flujos",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; }
      .marea-regime-box { padding: 1rem 1.5rem; border-radius: 8px;
                          background: #1a1a2e; margin-bottom: 1rem; }
      .marea-disclaimer { background: #1f2937; border-left: 4px solid #f59e0b;
                          padding: 0.5rem 1rem; border-radius: 4px;
                          font-size: 0.85em; color: #d1d5db; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Cabecera ─────────────────────────────────────────────────────────────────

st.title("🌊 MAREA — Monitor de Flujos de Liquidez")

# ── Carga de datos (todas cacheadas 5 min) ────────────────────────────────────

regime = load_regime_current()
narrative = load_latest_narrative()
flow_df = load_flow_scores("7d")
history = load_regime_history(60)
rotations = load_rotations(20)
exposures = load_exposures()
alerts = load_alerts(30)

# ── 1. RÉGIMEN ACTUAL (cabecera destacada) ────────────────────────────────────

st.subheader("Régimen actual")

if regime is None:
    empty_state("Sin régimen calculado aún — ejecuta primero `/analysis/run`.")
else:
    conf = float(regime.get("confidence", 0.0))
    reg_name = regime.get("regime", "neutral")
    signals: list[str] = regime.get("signals") or []

    st.markdown(regime_badge_html(reg_name, conf), unsafe_allow_html=True)
    st.caption(f"Última actualización: {regime.get('ts', '—')}")

    if conf < 0.4:
        data_factor = regime.get("data_confidence_factor")
        if data_factor is not None and data_factor < 0.7:
            st.warning(
                "⚠ Datos preliminares — los scores subyacentes están en cold start "
                "(histórico insuficiente). Las señales detectadas son reales, "
                "pero calculadas sobre pocos datos.",
                icon=None,
            )
        else:
            st.warning(
                "⚠ Datos preliminares — confianza baja. "
                "El régimen puede no ser representativo todavía.",
                icon=None,
            )

    if signals:
        cols = st.columns(min(len(signals), 4))
        for col, sig in zip(cols, signals):
            col.metric(label="Señal", value=sig.replace("_", " ").title())

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tabs = st.tabs(
    [
        "📰 Narrativa",
        "🔥 Heatmap de Flujos",
        "📊 Correlaciones",
        "🔄 Rotación Sectorial",
        "🔗 Exposición Indirecta",
        "📅 Timeline de Régimen",
        "🔔 Alertas Recientes",
    ]
)

# ── Tab 1: Narrativa ──────────────────────────────────────────────────────────

with tabs[0]:
    st.subheader("Narrativa más reciente")
    if narrative is None:
        empty_state("Sin narrativa generada aún — ejecuta `/narrative/generate`.")
    else:
        conf_narr = float(narrative.get("confidence", 0.0))

        col_meta1, col_meta2 = st.columns(2)
        col_meta1.caption(f"Generada: {narrative.get('ts', '—')}")
        col_meta2.caption(f"Motor: {narrative.get('llm_engine', '—')}")

        st.markdown(
            confidence_badge_html(conf_narr),
            unsafe_allow_html=True,
        )
        if conf_narr < 0.4:
            st.warning(
                "⚠ Confianza baja — la narrativa puede contener incertidumbre explícita.",
                icon=None,
            )

        st.markdown("---")
        st.markdown(narrative.get("text", ""))
        st.markdown("---")

        # Sello obligatorio — siempre visible
        st.markdown(
            f'<div class="marea-disclaimer">⚠️ <strong>{DISCLAIMER}</strong></div>',
            unsafe_allow_html=True,
        )

# ── Tab 2: Heatmap de flujos ──────────────────────────────────────────────────

with tabs[1]:
    st.subheader("Flow Scores por Asset")

    if flow_df.empty:
        empty_state("Sin flow scores — ejecuta primero la ingesta y el scoring.")
    else:
        has_low = (flow_df["confidence"] == "low").any()

        st.plotly_chart(flow_score_chart(flow_df), use_container_width=True)

        if has_low:
            st.markdown(low_conf_note_html(), unsafe_allow_html=True)
            st.caption(
                "Los assets marcados con * tienen pocos datos históricos (cold start). "
                "Sus scores son orientativos."
            )

        with st.expander("Ver tabla de datos"):
            display_df = flow_df[
                ["ticker", "name", "asset_class", "sector", "score", "confidence", "n_obs"]
            ].copy()
            display_df["score"] = display_df["score"].round(3)
            st.dataframe(display_df, use_container_width=True, hide_index=True)

# ── Tab 3: Correlaciones ──────────────────────────────────────────────────────

with tabs[2]:
    st.subheader("Matrices de Correlación")

    matrix_choice = st.radio(
        "Tipo de matriz",
        options=["intermarket", "sector"],
        format_func=lambda x: "Intermercado (clases de activo)" if x == "intermarket" else "Sectorial (ETFs)",
        horizontal=True,
    )

    matrix_df = load_correlations(matrix_choice, "7d")

    if matrix_df.empty:
        empty_state("Sin correlaciones calculadas — ejecuta `/analysis/run`.")
    else:
        decoupling: set = matrix_df.attrs.get("decoupling_pairs", set())

        title_map = {
            "intermarket": "Correlación Intermercado (7d) — ⚠ = desacople detectado",
            "sector": "Correlación Sectorial — ETFs (7d) — ⚠ = desacople detectado",
        }
        fig = corr_heatmap(matrix_df, title_map[matrix_choice], decoupling)
        st.plotly_chart(fig, use_container_width=True)

        if decoupling:
            pairs_str = ", ".join(
                f"{a}/{b}" for a, b in sorted(decoupling) if a < b
            )
            st.warning(
                f"Pares en desacople (correlación 7d diverge >0.5 respecto a 30d): **{pairs_str}**"
            )

        st.caption("Escala: −1 = correlación inversa perfecta · 0 = sin correlación · +1 = correlación directa perfecta")

# ── Tab 4: Rotación sectorial ─────────────────────────────────────────────────

with tabs[3]:
    st.subheader("Rotación Sectorial Detectada")

    if not rotations:
        empty_state("Sin rotaciones detectadas aún.")
    else:
        rot_df = pd.DataFrame(rotations)
        rot_df["ts"] = pd.to_datetime(rot_df["ts"]).dt.strftime("%Y-%m-%d %H:%M")
        rot_df["strength"] = rot_df["strength"].round(3)
        rot_df = rot_df.rename(
            columns={
                "ts": "Fecha",
                "from_sector": "Desde",
                "to_sector": "Hacia",
                "strength": "Intensidad",
            }
        )
        st.dataframe(rot_df, use_container_width=True, hide_index=True)
        st.caption(
            "Intensidad = min(|score_outflow|, |score_inflow|). "
            "Umbral de detección: ±0.25 en score de clase."
        )

# ── Tab 5: Exposición indirecta ───────────────────────────────────────────────

with tabs[4]:
    st.subheader("Mapa de Exposición Indirecta")

    # Aviso prominente de no-consejo
    st.markdown(
        '<div class="marea-disclaimer">⚠️ <strong>NO es consejo de inversión.</strong> '
        "Las exposiciones marcadas como hipótesis son generadas por IA y NO han sido "
        "verificadas manualmente. Comprueba siempre las fuentes originales.</div>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    if not exposures:
        empty_state("Sin exposiciones registradas — ejecuta `/exposure/discover`.")
    else:
        _CONF_LABELS = {
            "confirmado_oficial": "✅ Confirmado oficial",
            "rumor_prensa": "📰 Rumor prensa",
            "especulacion": "🔮 Especulación",
        }
        _CONF_ORDER = {
            "confirmado_oficial": 0,
            "rumor_prensa": 1,
            "especulacion": 2,
        }

        sorted_exp = sorted(
            exposures, key=lambda e: _CONF_ORDER.get(e.get("confidence", "especulacion"), 2)
        )

        for exp in sorted_exp:
            conf_level = exp.get("confidence", "especulacion")
            is_hypothesis = conf_level in ("rumor_prensa", "especulacion")

            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 2, 4])
                c1.markdown(f"**{exp.get('source_entity', '?')}** → **{exp.get('exposed_ticker', '?')}**")
                c2.markdown(f"`{exp.get('exposure_type', '?')}`")
                c3.markdown(_CONF_LABELS.get(conf_level, conf_level))

                st.caption(exp.get("relationship", ""))

                if is_hypothesis:
                    st.error(
                        "⚠ SIN VERIFICAR — hipótesis generada por IA · NO es consejo de inversión",
                        icon=None,
                    )

                sources: list = exp.get("sources") or []
                if sources:
                    links = " · ".join(f"[fuente {i+1}]({url})" for i, url in enumerate(sources[:3]))
                    st.markdown(f"Fuentes: {links}")

                st.caption(f"Última verificación: {exp.get('last_verified_at', '—')}")

# ── Tab 6: Timeline de régimen ────────────────────────────────────────────────

with tabs[5]:
    st.subheader("Histórico de Régimen")

    if not history:
        empty_state("Sin histórico de régimen — ejecuta `/analysis/run` varios días.")
    else:
        st.plotly_chart(regime_timeline_chart(history), use_container_width=True)
        st.caption(
            "Línea punteada amarilla = umbral de confianza mínimo para envío de alertas (0.4). "
            "Puntos por debajo = datos preliminares."
        )

        with st.expander("Ver tabla completa"):
            hist_df = pd.DataFrame(history)
            hist_df["ts"] = pd.to_datetime(hist_df["ts"]).dt.strftime("%Y-%m-%d")
            hist_df["confidence"] = hist_df["confidence"].round(3)
            hist_df["regime"] = hist_df["regime"].map(REGIME_LABEL).fillna(hist_df["regime"])
            hist_df = hist_df.rename(
                columns={"ts": "Fecha", "regime": "Régimen", "confidence": "Confianza"}
            )
            st.dataframe(hist_df, use_container_width=True, hide_index=True)

# ── Tab 7: Alertas recientes ──────────────────────────────────────────────────

with tabs[6]:
    st.subheader("Alertas Recientes")

    if not alerts:
        empty_state("Sin alertas registradas aún — ejecuta `/alerts/run`.")
    else:
        _TYPE_LABEL = {
            "flow_extreme": "Flujo extremo",
            "regime_change": "Cambio de régimen",
            "decoupling": "Desacople",
            "exposure": "Exposición indirecta",
        }

        sent_alerts = [a for a in alerts if a.get("sent")]
        pending_alerts = [a for a in alerts if not a.get("sent")]

        col_sent, col_pending = st.columns(2)

        with col_sent:
            st.markdown(f"**Enviadas** ({len(sent_alerts)})")
            for a in sent_alerts:
                with st.container(border=True):
                    type_str = _TYPE_LABEL.get(a.get("alert_type", ""), a.get("alert_type", "?"))
                    st.markdown(f"**{type_str}** — `{a.get('entity', '?')}`")
                    st.caption(
                        f"Estado: {a.get('state', '?')} · "
                        f"Confianza: {float(a.get('confidence', 0)):.0%} · "
                        f"Enviada: {a.get('sent_at', '—')}"
                    )

        with col_pending:
            st.markdown(f"**No enviadas** ({len(pending_alerts)})")
            for a in pending_alerts:
                with st.container(border=True):
                    type_str = _TYPE_LABEL.get(a.get("alert_type", ""), a.get("alert_type", "?"))
                    reason = a.get("not_sent_reason") or "sin razón"
                    st.markdown(f"**{type_str}** — `{a.get('entity', '?')}`")
                    st.caption(
                        f"Estado: {a.get('state', '?')} · "
                        f"Confianza: {float(a.get('confidence', 0)):.0%} · "
                        f"Razón: `{reason}`"
                    )

# ── Footer ────────────────────────────────────────────────────────────────────

st.divider()
st.markdown(
    f'<div class="marea-disclaimer">⚠️ <strong>{DISCLAIMER}</strong> — '
    "MAREA es un sistema de monitoreo de flujos. Los datos mostrados son "
    "automáticos y pueden contener errores o sesgos. "
    "No operes basándote exclusivamente en esta información.</div>",
    unsafe_allow_html=True,
)
