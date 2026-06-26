"""
Partes de Telegram por ciclo — rediseño "SIGUE LA LIQUIDEZ" (Sesión 12).

A diferencia de las ALERTAS POR EVENTO (que solo llegan con confianza alta,
anti-duplicado e histéresis), el parte se envía SIEMPRE en cada ciclo: es a la
vez foto del mercado y señal de vida.

PRINCIPIO RECTOR — "no dejar nada a medias":
  Todo flujo que sale va a algún sitio, o se DECLARA en espera. Toda pólvora de
  stablecoins liberada dispara hacia un destino o se declara sin destino. Toda
  tensión entre fuerzas se resuelve diciendo cuál manda (o se declara empate).
  Nunca se suelta una frase insinuante sin cerrarla. Cerrar es a veces "fue a X"
  (cuando los datos lo respaldan) y a veces "en espera, sin destino visible"
  (cuando no). Las dos cierran; lo prohibido es inventar un destino o dejar la
  frase colgando.

AFIRMATIVO vs CONDICIONAL:
  * Lo que la liquidez HIZO → se afirma (entra/sale, se fortalece). Son hechos.
  * El DESTINO inferido por simultaneidad → "parece dirigirse a / apunta a"
    (inferencia, no rastreo literal del dinero).
  * El salto al PRECIO futuro → no se hace. MAREA describe flujo, no predice.

LOS DATOS MANDAN: cifras, rankings, destino inferido, pólvora y "quién manda"
salen de REGLAS sobre los flow scores reales. La narrativa de Groq, si está,
solo añade una frase de color en cursiva; nunca aporta números.

Composición (build_*_digest): funciones PURAS sobre estado sintético → testeables.
Envío (send_*_digest): leen estado real de la BD, componen, envían y persisten
el ciclo (digest_cycles) para que el siguiente parte pueda comparar. Nunca lanzan.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("marea.alerts.digest")

_DISCLAIMER = "⚠️ Interpretación automática · no es consejo de inversión."
_COLD_NOTICE = "⚠️ <i>Datos preliminares (histórico insuficiente, baja confianza)</i>"
_COLD_CONF_THRESHOLD = 0.4   # confianza de régimen por debajo de esto → preliminar

# ── Graduación de intensidad por |score| (documentado) ────────────────────────
#   |score| ≥ 0.85 → "fuerte" · 0.5–0.85 → "moderada" · < 0.5 → "leve"
_STRONG = 0.85
_MODERATE = 0.5
# Dos fuerzas opuestas se consideran "parejas" (empate) si sus |score| distan
# menos de esto → "señales cruzadas sin dirección clara".
_TIE_EPS = 0.12
# Cambio mínimo de score entre dos partes para considerarlo "movimiento" real.
_DELTA_MIN = 0.10

# ── Semáforo (umbrales documentados) ──────────────────────────────────────────
#   🟢 tranquilo  : ninguna fuerza supera lo "moderado" (max|score| < 0.5)
#   🟡 normal     : hay movimiento moderado (0.5 ≤ max|score| < 0.85)
#   🔴 fuerte     : hay rotación fuerte (max|score| ≥ 0.85), rotación sectorial
#                   fuerte, o régimen risk-off/refugio con confianza alta.
#   En COLD START nunca se pinta 🔴 (los datos no son fiables): por defecto 🟡,
#   y 🟢 solo si de verdad todo está plano.

# Activos que son TERMÓMETROS de sentimiento, no vasijas de liquidez: el VIX y el
# Fear&Greed no "reciben dinero". Se excluyen de rankings/destino/quién-manda para
# no escribir frases como "el capital se fue al VIX".
_SENTIMENT = {"^VIX", "CRYPTO_FNG"}

_STABLE_TICKERS = {"STABLES_USDT", "STABLES_USDC"}
_CRYPTO_TICKERS = {"BTC", "ETH", "BTC-USD", "ETH-USD", "BTC_PERP", "ETH_PERP", "IBIT"}
_SAFE_TICKERS = {"GC=F", "SI=F", "GLD", "SLV", "GDX", "SIL", "DX-Y.NYB", "^VIX", "^TNX"}

# Diccionario ticker → nombre legible (es). Cae a assets.name y luego al ticker.
_READABLE = {
    "^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^IBEX": "IBEX 35", "^N225": "Nikkei 225",
    "GC=F": "Oro", "SI=F": "Plata",
    "DX-Y.NYB": "Dólar (DXY)", "^VIX": "Volatilidad (VIX)", "^TNX": "Bono 10A EE.UU.",
    "SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)", "GLD": "Oro (GLD)", "SLV": "Plata (SLV)",
    "IBIT": "Bitcoin ETF (IBIT)", "SOXX": "Semiconductores (SOXX)", "SMH": "Semiconductores (SMH)",
    "XME": "Metales y minería", "GDX": "Mineras de oro", "SIL": "Mineras de plata",
    "ITA": "Defensa (ITA)", "XAR": "Defensa (XAR)", "XLE": "Energía", "XLK": "Tecnología",
    "XLF": "Financieras (bancos)", "XLV": "Salud",
    "BTC": "Bitcoin", "ETH": "Ethereum", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
    "BTC_PERP": "Bitcoin (perp)", "ETH_PERP": "Ethereum (perp)",
    "STABLES_USDT": "USDT (stablecoin)", "STABLES_USDC": "USDC (stablecoin)",
    "CRYPTO_FNG": "Miedo/Codicia cripto",
}

# Traducción de nombres de régimen a lenguaje claro (es)
_REGIME_ES = {
    "risk_on":          "Risk-ON (apetito por riesgo)",
    "risk_off":         "Risk-OFF (aversión al riesgo)",
    "flight_to_safety": "Huida a refugio (oro/bonos)",
    "sector_rotation":  "Rotación sectorial",
    "neutral":          "Neutral (señales débiles o mixtas)",
}

# Qué momento previo prefiere comparar cada momento (la "película"). Configurable.
#   apertura → vs. cierre anterior · media → vs. apertura de hoy · cierre → vs. media
_COMPARE_MAP = {"apertura": "cierre", "media": "apertura", "cierre": "media"}
_MOMENT_LABEL = {"apertura": "la apertura", "media": "la media sesión", "cierre": "el cierre anterior"}
_MOMENT_FROM_INTRADAY = {"Apertura USA": "apertura", "Media sesión USA": "media", "Tarde USA": "cierre"}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers PUROS — los datos mandan (nombres, intensidad, clasificación)
# ══════════════════════════════════════════════════════════════════════════════

def _name(asset: dict) -> str:
    """Nombre legible: diccionario curado → assets.name → ticker."""
    t = asset.get("ticker", "?")
    return _READABLE.get(t) or asset.get("name") or t


def _score(asset: dict) -> float:
    try:
        return float(asset.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _intensity(score: float) -> str:
    a = abs(score)
    if a >= _STRONG:
        return "fuerte"
    if a >= _MODERATE:
        return "moderada"
    return "leve"


def _dir_word(score: float) -> str:
    """Lo que la liquidez HIZO (afirmativo): entra / sale."""
    return "entra" if score >= 0 else "sale"


def _classify(asset: dict) -> str:
    """stable | crypto | safe | risk — base para pólvora, crypto y presión."""
    t = asset.get("ticker")
    cls = asset.get("asset_class")
    sec = asset.get("sector")
    if t in _STABLE_TICKERS or sec == "stablecoin" or cls == "onchain":
        return "stable"
    if t in _CRYPTO_TICKERS or cls == "crypto":
        return "crypto"
    if t in _SAFE_TICKERS:
        return "safe"
    return "risk"


def _flow_assets(assets: list[dict]) -> list[dict]:
    """Activos que SÍ son vasijas de liquidez (excluye termómetros de sentimiento)."""
    return [a for a in (assets or []) if a.get("ticker") not in _SENTIMENT]


def _fmt(asset: dict) -> str:
    """'Oro (moderada, +0.62)'."""
    s = _score(asset)
    return f"{_name(asset)} ({_intensity(s)}, {s:+.2f})"


# ── Semáforo + titular ────────────────────────────────────────────────────────

def _semaphore(assets: list[dict], regime: dict | None, cold_start: bool, rotation_strength: float) -> str:
    fa = _flow_assets(assets)
    maxabs = max((abs(_score(a)) for a in fa), default=0.0)
    if cold_start:
        # Datos no fiables: nunca 🔴; 🟢 solo si todo está realmente plano.
        return "🟢" if maxabs < _MODERATE else "🟡"
    regime_stress = (
        bool(regime)
        and regime.get("name") in {"risk_off", "flight_to_safety"}
        and float(regime.get("confidence") or 0.0) >= 0.6
    )
    if maxabs >= _STRONG or (rotation_strength or 0.0) >= _MODERATE or regime_stress:
        return "🔴"
    if maxabs >= _MODERATE:
        return "🟡"
    return "🟢"


def _headline(assets: list[dict]) -> str:
    """Titular estilo cabecera de periódico, derivado de REGLAS sobre los flujos."""
    fa = _flow_assets(assets)
    if not fa:
        return "Sin datos de flujo suficientes en este ciclo"
    ins = sorted([a for a in fa if _score(a) >= _MODERATE], key=_score, reverse=True)
    outs = sorted([a for a in fa if _score(a) <= -_MODERATE], key=_score)
    if not ins and not outs:
        return "Mercado tranquilo: sin rotaciones de liquidez marcadas"
    parts: list[str] = []
    if outs:
        parts.append(f"sale capital de {_name(outs[0])}")
    if ins:
        parts.append(f"entra en {_name(ins[0])}")
    cryptos_in = [a for a in fa if _classify(a) == "crypto" and _score(a) >= _MODERATE]
    if cryptos_in and not (ins and _classify(ins[0]) == "crypto"):
        parts.append("algo entra en cripto")
    s = "; ".join(parts)
    return s[:1].upper() + s[1:] if s else "Movimiento de liquidez en curso"


# ── Rankings de entrada / salida ──────────────────────────────────────────────

def _top_inflow(assets: list[dict], n: int) -> list[dict]:
    return sorted([a for a in _flow_assets(assets) if _score(a) > 0], key=_score, reverse=True)[:n]


def _top_outflow(assets: list[dict], n: int) -> list[dict]:
    return sorted([a for a in _flow_assets(assets) if _score(a) < 0], key=_score)[:n]


def _outflow_lines(outflow: list[dict], assets: list[dict]) -> list[str]:
    """Líneas del ranking de salidas; cada salida ≥ moderada se cierra con destino."""
    lines = []
    for a in outflow:
        base = f"  ▼ {_name(a)} — {_intensity(_score(a))}, {_score(a):+.2f}"
        if abs(_score(a)) >= _MODERATE:
            base += f" → {_infer_destination(a.get('ticker'), assets)}"
        lines.append(base)
    return lines


def _strongest_line(assets: list[dict]) -> str | None:
    fa = sorted(_flow_assets(assets), key=lambda a: abs(_score(a)), reverse=True)
    top = [a for a in fa if abs(_score(a)) >= _MODERATE][:2]
    if not top:
        return None
    frags = [f"{_name(a)} {_dir_word(_score(a))} ({_intensity(_score(a))}, {_score(a):+.2f})" for a in top]
    return "🔥 <b>Lo más fuerte:</b> " + "; ".join(frags) + "."


# ── Inferencia de destino (cerrar el círculo sin inventar) ────────────────────

def _infer_destination(source_ticker: str, assets: list[dict]) -> str:
    """
    Inferencia POR SIMULTANEIDAD (no rastreo): los receptores son los activos
    que reciben inflow ≥ moderado a la vez que `source_ticker` tiene salida.
    Si hay receptores claros → "parece dirigirse a …"; si no → "en espera".
    """
    receptors = sorted(
        [a for a in _flow_assets(assets) if a.get("ticker") != source_ticker and _score(a) >= _MODERATE],
        key=_score, reverse=True,
    )
    if receptors:
        names = " y ".join(_name(a) for a in receptors[:2])
        return f"parece dirigirse a {names}"
    return "sin destino visible — capital en espera"


# ── Crypto + cierre de la pólvora de stablecoins ──────────────────────────────

def _powder_line(assets: list[dict]) -> str | None:
    """
    Cierra SIEMPRE la pólvora de stablecoins (uno de los tres finales):
      · liberada y entra en crypto (crypto recibe a la vez),
      · liberada pero va a otro lado (bolsa/dólar reciben, crypto no),
      · liberada pero en espera (nadie la recibe con fuerza),
      · o acumulándose (las stablecoins crecen → capital aparcado).
    """
    stables = [a for a in (assets or []) if _classify(a) == "stable"]
    if not stables:
        return None
    avg = sum(_score(a) for a in stables) / len(stables)
    cryptos = [a for a in (assets or []) if _classify(a) == "crypto"]
    crypto_recv = [a for a in cryptos if _score(a) >= _MODERATE]
    non_crypto_recv = sorted(
        [a for a in _flow_assets(assets)
         if _classify(a) not in ("stable", "crypto") and _score(a) >= _MODERATE],
        key=_score, reverse=True,
    )

    if avg <= -_MODERATE:   # supply de stablecoins cae con fuerza → pólvora liberada
        if crypto_recv:
            picked = crypto_recv[:2]
            names = " y ".join(_name(a) for a in picked)
            verb = "recibe" if len(picked) == 1 else "reciben"
            return f"🧨 La pólvora de las stablecoins se libera y dispara hacia crypto ({names} {verb} a la vez)."
        if non_crypto_recv:
            names = " y ".join(_name(a) for a in non_crypto_recv[:2])
            return f"🧨 La pólvora de las stablecoins se libera, pero NO va a crypto: apunta a {names}."
        return "🧨 La pólvora de las stablecoins se libera, pero queda en espera — sin destino claro aún."
    if avg >= _MODERATE:
        return "🧨 Se acumula pólvora en stablecoins: capital aparcado, todavía sin desplegar."
    return "🧨 Las stablecoins apenas se mueven: sin pólvora liberada en este ciclo."


def _crypto_block(assets: list[dict]) -> list[str]:
    """SIEMPRE con nombres y dirección concretos. Cierra la pólvora siempre."""
    cryptos = sorted(
        [a for a in (assets or []) if _classify(a) == "crypto"],
        key=lambda a: abs(_score(a)), reverse=True,
    )
    if cryptos:
        frag = "; ".join(
            f"{_name(a)} {_dir_word(_score(a))} ({_intensity(_score(a))}, {_score(a):+.2f})"
            for a in cryptos[:4]
        )
        lines = [f"💰 <b>En crypto:</b> {frag}."]
    else:
        lines = ["💰 <b>En crypto:</b> sin datos de crypto en este ciclo."]
    powder = _powder_line(assets)
    if powder:
        lines.append(powder)
    else:
        lines.append("🧨 Sin stablecoins en los datos: no se puede rastrear la pólvora este ciclo.")
    return lines


# ── Quién manda (dictamina o declara empate) ──────────────────────────────────

def _pressure_for(asset: dict, inflow: bool) -> str:
    kind = _classify(asset)
    risky = kind in ("risk", "crypto")
    if inflow:
        return "la presión apunta a apetito por riesgo (risk-on)" if risky \
            else "la presión apunta a búsqueda de refugio (risk-off)"
    return "la presión apunta a aversión al riesgo (risk-off)" if risky \
        else "la presión apunta a salida del refugio (risk-on)"


def _who_dominates(assets: list[dict]) -> str:
    fa = _flow_assets(assets)
    inflows = [a for a in fa if _score(a) > 0]
    outflows = [a for a in fa if _score(a) < 0]
    top_in = max(inflows, key=_score, default=None)
    top_out = min(outflows, key=_score, default=None)

    if top_in and top_out:
        mi, mo = abs(_score(top_in)), abs(_score(top_out))
        if abs(mi - mo) < _TIE_EPS:
            return (
                "⚡ <b>Quién manda:</b> señales cruzadas sin dirección clara "
                f"({_name(top_in)} entra, {_name(top_out)} sale, fuerzas parejas) "
                "— sin lectura concluyente hoy."
            )
        if mi >= mo:
            return (
                f"⚡ <b>Quién manda:</b> domina la entrada en {_name(top_in)} "
                f"({_intensity(_score(top_in))}, {_score(top_in):+.2f}); {_pressure_for(top_in, True)}."
            )
        return (
            f"⚡ <b>Quién manda:</b> domina la salida de {_name(top_out)} "
            f"({_intensity(_score(top_out))}, {_score(top_out):+.2f}); {_pressure_for(top_out, False)}."
        )
    if top_in:
        return (
            f"⚡ <b>Quién manda:</b> domina la entrada en {_name(top_in)} "
            f"({_intensity(_score(top_in))}, {_score(top_in):+.2f}); {_pressure_for(top_in, True)}."
        )
    if top_out:
        return (
            f"⚡ <b>Quién manda:</b> domina la salida de {_name(top_out)} "
            f"({_intensity(_score(top_out))}, {_score(top_out):+.2f}); {_pressure_for(top_out, False)}."
        )
    return "⚡ <b>Quién manda:</b> sin fuerzas destacadas este ciclo."


# ── Comparación temporal (la película, con origen→destino) ────────────────────

def _compare_block(assets: list[dict], compare: dict | None) -> list[str]:
    """
    '🔄 Cambio desde …': delta por activo entre este parte y el anterior.
    Cada salida mencionada se cierra con destino. Degrada con elegancia en cold
    start: sin parte anterior → lo dice explícitamente (no inventa comparación).
    """
    if not compare or not compare.get("scores"):
        return ["🔄 <b>Cambio desde el parte anterior:</b> sin parte anterior suficiente para comparar todavía."]

    prev = compare["scores"]
    label = compare.get("label", "el parte anterior")
    movers = []
    for a in _flow_assets(assets):
        t = a.get("ticker")
        if t in prev:
            movers.append((a, _score(a) - float(prev[t] or 0.0), float(prev[t] or 0.0)))

    if not movers:
        return [f"🔄 <b>Cambio desde {label}:</b> sin activos comunes para comparar todavía."]

    movers.sort(key=lambda x: abs(x[1]), reverse=True)
    shown = [m for m in movers if abs(m[1]) >= _DELTA_MIN][:4]
    lines = [f"🔄 <b>Cambio desde {label}:</b>"]
    if not shown:
        lines.append("  Sin cambios relevantes respecto al parte anterior.")
        return lines

    for a, d, p in shown:
        now = _score(a)
        flipped = (p < 0 < now) or (now < 0 < p)
        if flipped and now > 0:
            lines.append(f"  ↗ {_name(a)} gira a ENTRADA (antes salía, Δ{d:+.2f}).")
        elif flipped and now < 0:
            lines.append(f"  ↘ {_name(a)} gira a SALIDA (antes entraba, Δ{d:+.2f}); {_infer_destination(a.get('ticker'), assets)}.")
        elif now < 0 and d < 0:
            lines.append(f"  ▼ {_name(a)} intensifica salida (Δ{d:+.2f}); {_infer_destination(a.get('ticker'), assets)}.")
        elif now > 0 and d > 0:
            lines.append(f"  ▲ {_name(a)} intensifica entrada (Δ{d:+.2f}).")
        else:
            lines.append(f"  ≈ {_name(a)} afloja su movimiento (Δ{d:+.2f}).")
    return lines


# ── Fondo (régimen como conclusión, confianza real) ───────────────────────────

def _fondo_line(regime: dict | None, cold_start: bool) -> str:
    if not regime:
        return "📈 <b>Fondo:</b> régimen sin determinar — histórico insuficiente para una lectura de fondo."
    name = _REGIME_ES.get(regime.get("name", ""), regime.get("name", "?"))
    conf = float(regime.get("confidence") or 0.0)
    qual = "alta" if conf >= 0.6 else "moderada" if conf >= _COLD_CONF_THRESHOLD else "baja"
    tail = " — tómalo como preliminar" if (cold_start or conf < _COLD_CONF_THRESHOLD) else ""
    line = f"📈 <b>Fondo:</b> el régimen de fondo es {name}, con confianza {conf:.0%} ({qual}){tail}."
    signals = regime.get("signals") or []
    if signals:
        sig = ", ".join(_SIGNAL_ES.get(s, s) for s in signals)
        line += f"\nSeñales activas: {sig}."
    return line


_SIGNAL_ES = {
    "crypto_inflow":            "entrada a crypto",
    "equity_inflow":            "entrada a acciones",
    "gold_inflow":              "entrada a oro",
    "bonds_inflow":             "entrada a bonos",
    "crypto_outflow":           "salida de crypto",
    "equity_outflow":           "salida de acciones",
    "dxy_falling":              "dólar debilitándose",
    "dxy_rising":               "dólar fortaleciéndose",
    "vix_calm":                 "volatilidad baja",
    "vix_fearful":              "volatilidad alta (miedo)",
    "sector_rotation_detected": "rotación sectorial",
}


def _narrative_snippet(narrative: str | None) -> str | None:
    if not narrative or not narrative.strip():
        return None
    snippet = narrative.strip().splitlines()[0].strip()
    if not snippet:
        return None
    if len(snippet) > 220:
        snippet = snippet[:217] + "…"
    return f"🖊 <i>{snippet}</i>"


# ══════════════════════════════════════════════════════════════════════════════
# Composición — funciones PURAS (reciben estado, devuelven texto)
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_digest(
    state: dict,
    narrative: str | None = None,
    now_label: str = "Cierre de mercado",
    compare: dict | None = None,
) -> str:
    """
    Parte DIARIO completo. ``state`` = {assets, regime, cold_start, rotations}.
    ``compare`` = {label, scores:{ticker:score}} del parte anterior (o None).
    """
    state = state or {}
    assets = state.get("assets") or []
    regime = state.get("regime")
    cold_start = bool(state.get("cold_start"))
    rotations = state.get("rotations") or []
    rot_strength = float(rotations[0].get("strength") or 0.0) if rotations else 0.0
    conf = float(regime["confidence"]) if regime else 0.0
    preliminary = cold_start or regime is None or conf < _COLD_CONF_THRESHOLD

    sem = _semaphore(assets, regime, cold_start, rot_strength)

    blocks: list[list[str]] = []

    # 1+2. Titular con semáforo + cabecera + coletilla + subtítulo de comparación
    head = [f"{sem} <b>{_headline(assets)}</b>", f"📊 <b>MAREA — {now_label}</b>"]
    if preliminary:
        head.append(_COLD_NOTICE)
    if compare and compare.get("label"):
        head.append(f"🔄 vs. {compare['label']}")
    blocks.append(head)

    # 3. Lo más fuerte
    strong = _strongest_line(assets)
    if strong:
        blocks.append([strong])

    # 4. Más entrada de liquidez (TOP 5)
    inflow = _top_inflow(assets, 5)
    if inflow:
        blocks.append(["🟢 <b>Más entrada de liquidez:</b>"]
                      + [f"  ▲ {_name(a)} — {_intensity(_score(a))}, {_score(a):+.2f}" for a in inflow])

    # 5. Más salida de liquidez (TOP 5) — cada salida FUERTE/MODERADA se cierra
    #    con su destino inferido o un "en espera" explícito (no dejar a medias).
    outflow = _top_outflow(assets, 5)
    if outflow:
        blocks.append(["🔴 <b>Más salida de liquidez:</b>"] + _outflow_lines(outflow, assets))

    # 6. Crypto + cierre de pólvora
    blocks.append(_crypto_block(assets))

    # 7. Cambio temporal (origen→destino)
    blocks.append(_compare_block(assets, compare))

    # 8. Quién manda
    blocks.append([_who_dominates(assets)])

    # 9. Fondo
    blocks.append([_fondo_line(regime, cold_start)])

    # (color) narrativa Groq, opcional
    snippet = _narrative_snippet(narrative)
    if snippet:
        blocks.append([snippet])

    # 10. Sello
    blocks.append([_DISCLAIMER])

    return "\n\n".join("\n".join(b) for b in blocks)


def build_intraday_digest(
    state: dict,
    moment: str = "Sesión USA",
    compare: dict | None = None,
) -> str:
    """
    Parte INTRADÍA (versión corta). ``state`` = {assets, cold_start?}.
    Top 3 (no 5), sin bloque largo de fondo; misma exigencia en crypto/pólvora.
    """
    state = state or {}
    assets = state.get("assets") or []
    # Preliminar si ningún activo tiene confianza 'ok'.
    if "cold_start" in state:
        cold_start = bool(state["cold_start"])
    else:
        cold_start = not any(a.get("confidence") == "ok" for a in assets)

    sem = _semaphore(assets, None, cold_start, 0.0)

    blocks: list[list[str]] = []

    head = [f"{sem} <b>{_headline(assets)}</b>", f"📡 <b>MAREA — {moment}</b>"]
    if cold_start:
        head.append(_COLD_NOTICE)
    if compare and compare.get("label"):
        head.append(f"🔄 vs. {compare['label']}")
    blocks.append(head)

    inflow = _top_inflow(assets, 3)
    if inflow:
        blocks.append(["🟢 <b>Top entradas:</b>"]
                      + [f"  ▲ {_name(a)} — {_intensity(_score(a))}, {_score(a):+.2f}" for a in inflow])

    outflow = _top_outflow(assets, 3)
    if outflow:
        blocks.append(["🔴 <b>Top salidas:</b>"] + _outflow_lines(outflow, assets))

    blocks.append(_crypto_block(assets))
    blocks.append(_compare_block(assets, compare))
    blocks.append([_who_dominates(assets)])
    blocks.append([_DISCLAIMER])

    return "\n\n".join("\n".join(b) for b in blocks)


# ══════════════════════════════════════════════════════════════════════════════
# Envío — leen estado, componen, envían y persisten el ciclo (nunca lanzan)
# ══════════════════════════════════════════════════════════════════════════════

def _digest_enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "digest_enabled", True))


def _resolve_db(db):
    if db is not None:
        return db
    from app.db import get_db
    return get_db()


def _send(text: str, send_fn=None) -> bool:
    """Envía vía Telegram reutilizando el cliente existente. send_fn inyectable en tests."""
    if send_fn is not None:
        return send_fn(text)
    from app.alerts.telegram import send_message
    from app.config import settings
    return send_message(
        text,
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )


def send_daily_digest(db=None, now_label: str = "Cierre de mercado", send_fn=None) -> dict:
    """
    Compone y envía el parte diario. ``ok=True`` SIEMPRE (un fallo de envío no
    tumba el ciclo): los problemas se registran en ``errors`` (errores blandos).
    """
    result = {"kind": "daily", "enabled": _digest_enabled(), "sent": False, "errors": []}
    if not result["enabled"]:
        logger.info("DIGEST_ENABLED=false — parte diario omitido")
        result["ok"] = True
        return result

    try:
        from app.narrative.snapshot import build_snapshot

        rdb = _resolve_db(db)
        snapshot = build_snapshot(rdb)         # reutiliza: régimen, cold_start, rotaciones
        assets = _load_daily_assets(rdb)       # TOP 5 + crypto + stablecoins, con nombres reales
        state = {
            "assets": assets,
            "regime": snapshot.get("regime"),
            "cold_start": bool(snapshot.get("cold_start")),
            "rotations": snapshot.get("rotations") or [],
        }
        moment = "cierre"
        compare = _load_prev_cycle(rdb, "daily", moment)
        narrative = _latest_narrative(rdb)
        text = build_daily_digest(state, narrative=narrative, now_label=now_label, compare=compare)
        ok = _send(text, send_fn)
        result["sent"] = bool(ok)
        if not ok:
            result["errors"].append("telegram_send_failed")
            logger.warning("Parte diario: Telegram no aceptó el mensaje")
        else:
            logger.info("Parte diario enviado a Telegram")
        _save_cycle(rdb, "daily", moment, assets, regime=snapshot.get("regime"))
    except Exception as e:  # noqa: BLE001 — nunca tumbar el ciclo por el parte
        logger.error("Parte diario falló al componer/enviar: %s", e)
        result["errors"].append(str(e))

    result["ok"] = True
    return result


def send_intraday_digest(db=None, analysis: dict | None = None, hour_utc: int | None = None, send_fn=None) -> dict:
    """
    Compone y envía el parte intradía. ``analysis`` es el resultado en memoria
    del análisis intradía recién ejecutado. Igual que el diario: ``ok=True``
    siempre; los fallos se registran como errores blandos.
    """
    result = {"kind": "intraday", "enabled": _digest_enabled(), "sent": False, "errors": []}
    if not result["enabled"]:
        logger.info("DIGEST_ENABLED=false — parte intradía omitido")
        result["ok"] = True
        return result

    try:
        rdb = _resolve_db(db)
        moment_label = _intraday_moment(hour_utc)
        moment = _MOMENT_FROM_INTRADAY.get(moment_label, "media")
        assets = _assets_from_movements(analysis)
        state = {"assets": assets}
        compare = _load_prev_cycle(rdb, "intraday", moment)
        text = build_intraday_digest(state, moment=f"{moment_label} (intradía)", compare=compare)
        ok = _send(text, send_fn)
        result["sent"] = bool(ok)
        if not ok:
            result["errors"].append("telegram_send_failed")
            logger.warning("Parte intradía: Telegram no aceptó el mensaje")
        else:
            logger.info("Parte intradía enviado a Telegram")
        _save_cycle(rdb, "intraday", moment, assets)
    except Exception as e:  # noqa: BLE001
        logger.error("Parte intradía falló al componer/enviar: %s", e)
        result["errors"].append(str(e))

    result["ok"] = True
    return result


# ── Helpers de lectura / persistencia ─────────────────────────────────────────

def _load_daily_assets(db) -> list[dict]:
    """Último flow score 7d por asset (dedup), con nombre/clase/sector reales."""
    try:
        resp = (
            db.table("flow_scores")
            .select("asset_id,ts,win,score,confidence,assets(ticker,name,asset_class,sector)")
            .eq("win", "7d")
            .order("ts", desc=True)
            .limit(500)
            .execute()
        )
        raw = resp.data or []
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudieron leer flow_scores para el parte diario: %s", e)
        return []

    seen: set = set()
    out: list[dict] = []
    for row in raw:
        aid = row.get("asset_id")
        if aid is None or aid in seen:
            continue
        seen.add(aid)
        ai = row.get("assets") or {}
        out.append({
            "ticker": ai.get("ticker", "?"),
            "name": ai.get("name"),
            "asset_class": ai.get("asset_class"),
            "sector": ai.get("sector"),
            "score": round(float(row.get("score") or 0.0), 3),
            "confidence": row.get("confidence", "low"),
        })
    return out


def _assets_from_movements(analysis: dict | None) -> list[dict]:
    """Convierte los movimientos intradía (en memoria) al estado del parte."""
    out: list[dict] = []
    for m in (analysis or {}).get("movements") or []:
        out.append({
            "ticker": m.get("ticker", "?"),
            "name": None,
            "asset_class": m.get("asset_class"),
            "sector": None,
            "score": round(float(m.get("score") or 0.0), 3),
            "confidence": m.get("confidence", "low"),
        })
    return out


def _load_prev_cycle(db, rail: str, moment: str) -> dict | None:
    """
    Lee el parte ANTERIOR relevante de digest_cycles para componer la película.
    Prefiere el momento mapeado (apertura↔cierre, etc.); si no, el más reciente
    de OTRO momento; si no hay nada → None (cold start lo declara explícito).
    """
    try:
        resp = (
            db.table("digest_cycles")
            .select("ts,rail,moment,scores,created_at")
            .eq("rail", rail)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer el ciclo anterior (%s/%s): %s", rail, moment, e)
        return None
    if not rows:
        return None

    pref = _COMPARE_MAP.get(moment)
    pref_rows = [r for r in rows if r.get("moment") == pref]
    other_rows = [r for r in rows if r.get("moment") != moment]
    chosen = pref_rows[0] if pref_rows else (other_rows[0] if other_rows else rows[0])

    scores: dict = {}
    for s in chosen.get("scores") or []:
        if isinstance(s, dict) and s.get("ticker") is not None:
            try:
                scores[s["ticker"]] = float(s.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
    label = _MOMENT_LABEL.get(chosen.get("moment"), "el parte anterior")
    return {"label": label, "scores": scores}


def _save_cycle(db, rail: str, moment: str, assets: list[dict], regime: dict | None = None) -> None:
    """Persiste el parte actual para que el siguiente pueda compararse. Nunca lanza."""
    try:
        from app.ingest._base import day_ts

        row = {
            "ts": day_ts(),
            "rail": rail,
            "moment": moment,
            "scores": [
                {
                    "ticker": a.get("ticker"),
                    "score": a.get("score"),
                    "asset_class": a.get("asset_class"),
                    "confidence": a.get("confidence"),
                }
                for a in (assets or [])
            ],
            "regime": (regime or {}).get("name") if regime else None,
            "confidence": float(regime["confidence"]) if regime else None,
        }
        db.table("digest_cycles").upsert(row, on_conflict="ts,rail,moment").execute()
    except Exception as e:  # noqa: BLE001 — persistir es best-effort, nunca tumba el parte
        logger.warning("No se pudo persistir el ciclo (%s/%s): %s", rail, moment, e)


def _latest_narrative(db) -> str | None:
    try:
        resp = (
            db.table("narratives")
            .select("text")
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("text") if rows else None
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer narrativa para el parte: %s", e)
        return None


def _intraday_moment(hour_utc: int | None = None) -> str:
    """
    Deriva el momento del día desde la hora UTC del ciclo.
    Cron (verano): 13:30→apertura, 16:00→media sesión, 18:00→tarde.
    (En invierno serían 12/15/17 UTC, que caen en los mismos tramos.)
    """
    if hour_utc is None:
        from datetime import datetime, timezone
        hour_utc = datetime.now(timezone.utc).hour
    if hour_utc <= 14:
        return "Apertura USA"
    if hour_utc <= 16:
        return "Media sesión USA"
    return "Tarde USA"
