"""
Helpers de renderizado para el dashboard MAREA.

Cada función devuelve un objeto Plotly Figure o produce output de Streamlit
directamente. Ninguna función lee de la BD — eso es responsabilidad de data.py.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DISCLAIMER = "Interpretación automática de datos · no es consejo de inversión."

REGIME_LABEL: dict[str, str] = {
    "risk_on": "Risk-ON",
    "risk_off": "Risk-OFF",
    "flight_to_safety": "Huida a la seguridad",
    "sector_rotation": "Rotación sectorial",
    "neutral": "Neutral",
}

REGIME_COLOR: dict[str, str] = {
    "risk_on": "#22c55e",
    "risk_off": "#ef4444",
    "flight_to_safety": "#f59e0b",
    "sector_rotation": "#3b82f6",
    "neutral": "#6b7280",
}

_PLOTLY_LAYOUT = dict(
    plot_bgcolor="#111111",
    paper_bgcolor="#111111",
    font=dict(color="#FFFFFF"),
    margin=dict(l=20, r=20, t=40, b=20),
)


# ── Badges ────────────────────────────────────────────────────────────────────

def confidence_badge_html(conf: float) -> str:
    """Devuelve HTML de un badge coloreado según el nivel de confianza."""
    if conf >= 0.7:
        bg, label = "#22c55e", f"Alta {conf:.0%}"
    elif conf >= 0.4:
        bg, label = "#f59e0b", f"Media {conf:.0%}"
    else:
        bg, label = "#ef4444", f"Datos preliminares {conf:.0%}"
    return (
        f'<span style="background:{bg};color:#000;padding:2px 10px;'
        f'border-radius:4px;font-size:0.85em;font-weight:600;">'
        f"Confianza {label}</span>"
    )


def regime_badge_html(regime: str, conf: float) -> str:
    color = REGIME_COLOR.get(regime, "#6b7280")
    label = REGIME_LABEL.get(regime, regime)
    return (
        f'<span style="background:{color};color:#000;padding:4px 16px;'
        f'border-radius:6px;font-size:1.1em;font-weight:700;">'
        f"{label}</span>&nbsp;&nbsp;"
        + confidence_badge_html(conf)
    )


def low_conf_note_html() -> str:
    return (
        '<span style="background:#374151;color:#f59e0b;padding:2px 8px;'
        'border-radius:4px;font-size:0.8em;">* bajo nº de observaciones</span>'
    )


def empty_state(message: str = "Sin datos aún — ejecuta la ingesta primero.") -> None:
    st.info(f"ℹ  {message}")


def disclaimer_box() -> None:
    st.caption(f"⚠️ {DISCLAIMER}")


# ── Heatmap de flujos ─────────────────────────────────────────────────────────

def flow_score_chart(df: pd.DataFrame) -> go.Figure:
    """
    Gráfico de barras coloreadas por score (verde = inflow, rojo = outflow).
    Assets con confidence='low' se marcan con asterisco.
    Agrupados por asset_class (orden visual).
    """
    if df.empty:
        return go.Figure()

    df = df.copy()
    df["score"] = df["score"].fillna(0.0)
    df = df.sort_values(["asset_class", "score"], ascending=[True, False])
    df["label"] = df.apply(
        lambda r: f"{r['ticker']}{'*' if r['confidence'] == 'low' else ''}", axis=1
    )

    colors = df["score"].apply(_bar_color).tolist()
    hover = df.apply(
        lambda r: (
            f"<b>{r['ticker']}</b> — {r['name']}<br>"
            f"Score: {r['score']:.3f}<br>"
            f"Clase: {r['asset_class']} / {r.get('sector') or '—'}<br>"
            f"Confianza: {r['confidence']} (n={r['n_obs']})"
        ),
        axis=1,
    ).tolist()

    fig = go.Figure(
        go.Bar(
            x=df["label"].tolist(),
            y=df["score"].tolist(),
            marker_color=colors,
            text=[f"{s:.2f}" for s in df["score"]],
            textposition="outside",
            hovertext=hover,
            hoverinfo="text",
        )
    )
    fig.update_layout(
        title="Flow Scores por Asset (ventana 7d) — * = baja confianza",
        yaxis_title="Score  −1 outflow ←→ +1 inflow",
        yaxis_range=[-1.3, 1.3],
        height=420,
        showlegend=False,
        **_PLOTLY_LAYOUT,
    )
    fig.add_hline(y=0, line_color="#4b5563", line_width=1)
    fig.add_hline(y=0.7, line_color="#22c55e", line_dash="dot", line_width=1)
    fig.add_hline(y=-0.7, line_color="#ef4444", line_dash="dot", line_width=1)
    return fig


def _bar_color(score: float) -> str:
    if score > 0.3:
        return "#22c55e"
    if score < -0.3:
        return "#ef4444"
    return "#6b7280"


# ── Matriz de correlación ─────────────────────────────────────────────────────

def corr_heatmap(
    matrix: pd.DataFrame,
    title: str,
    decoupling_pairs: set[tuple[str, str]] | None = None,
) -> go.Figure:
    """
    Heatmap de correlación. Pares en desacople llevan una anotación ⚠.
    """
    if matrix.empty:
        return go.Figure()

    labels = list(matrix.index)
    z = matrix.values.tolist()

    annotations: list[dict] = []
    if decoupling_pairs:
        for i, row_l in enumerate(labels):
            for j, col_l in enumerate(labels):
                if (row_l, col_l) in decoupling_pairs:
                    annotations.append(
                        dict(
                            x=j,
                            y=i,
                            text="⚠",
                            showarrow=False,
                            font=dict(color="#f59e0b", size=14),
                        )
                    )

    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=labels,
            y=labels,
            colorscale="RdBu",
            zmid=0,
            zmin=-1,
            zmax=1,
            colorbar=dict(title="Corr", tickformat=".1f"),
            hovertemplate="%{y} / %{x}: %{z:.3f}<extra></extra>",
        )
    )
    if annotations:
        fig.update_layout(annotations=annotations)

    fig.update_layout(
        title=title,
        height=520,
        xaxis=dict(tickangle=-45),
        **_PLOTLY_LAYOUT,
    )
    return fig


# ── Timeline de régimen ───────────────────────────────────────────────────────

def regime_timeline_chart(history: list[dict]) -> go.Figure:
    """
    Scatter coloreado por régimen a lo largo del tiempo.
    Eje Y = confianza; color = régimen.
    """
    if not history:
        return go.Figure()

    df = pd.DataFrame(history)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts")

    fig = go.Figure()
    for regime, grp in df.groupby("regime", sort=False):
        fig.add_trace(
            go.Scatter(
                x=grp["ts"].tolist(),
                y=grp["confidence"].tolist(),
                mode="markers+lines",
                name=REGIME_LABEL.get(regime, regime),
                marker=dict(
                    color=REGIME_COLOR.get(regime, "#6b7280"),
                    size=9,
                    symbol="circle",
                ),
                line=dict(color=REGIME_COLOR.get(regime, "#6b7280"), width=1.5),
                hovertemplate=(
                    f"<b>{REGIME_LABEL.get(regime, regime)}</b><br>"
                    "Confianza: %{y:.0%}<br>%{x|%Y-%m-%d}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title="Histórico de Régimen (ventana 7d)",
        yaxis_title="Confianza",
        yaxis_range=[0, 1.1],
        yaxis_tickformat=".0%",
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        **_PLOTLY_LAYOUT,
    )
    fig.add_hline(y=0.4, line_color="#f59e0b", line_dash="dot", line_width=1)
    return fig
