# MAREA — Monitor de flujos de liquidez intermercado

Sesiones completadas: **1** (scaffold + yfinance) · **2** (crypto + on-chain) · **3** (universo dinámico) · **4** (motor flow scores) · **5** (análisis intermercado) · **6** (mapa de exposición indirecta) · **7** (capa narrativa LLM) · **8** (motor de alertas + bot Telegram) · **9** (dashboard Streamlit) · **9b** (carril intradía) · **10** (despliegue GitHub Actions) · **11** (parte por ciclo en Telegram) · **12** (rediseño de los partes: "sigue la liquidez")

---

## Dos carriles de datos: DIARIO vs INTRADÍA

MAREA tiene **dos carriles paralelos e independientes**. El diario da el fondo
de mercado; el intradía capta el movimiento mientras ocurre.

| Aspecto | Carril DIARIO | Carril INTRADÍA |
|---------|--------------|-----------------|
| Tabla snapshots | `raw_snapshots` | `raw_snapshots_intraday` |
| Tabla scores | `flow_scores` | `flow_scores_intraday` |
| Timestamp | Medianoche UTC (`day_ts()`) | **Hora real de la barra** |
| Ventanas | 7d / 30d | 4h / 1d_intraday |
| Fuentes | yfinance 1d + crypto APIs | yfinance 60m/15m + crypto actual |
| Régimen | Sí (risk_on/risk_off…) | No — señal de corto plazo |
| Alertas | `flow_extreme`, `regime_change`, `decoupling`, `exposure` | `intraday_flow` |

**El carril diario no se modifica.** Ambos carriles conviven sin tocarse.

### Cómo ejecutar el carril intradía

```bash
# 1. Ingesta: descarga barras 60m (yfinance) + precio actual (crypto + FNG)
curl http://localhost:8000/ingest/intraday/run

# 2. Calcula flow scores intradía (ventanas 4h y 1d_intraday)
curl http://localhost:8000/scores/intraday/compute

# 3. Detecta movimientos en curso (inflow/outflow por activo)
curl http://localhost:8000/analysis/intraday/run

# 4. Consulta los últimos scores intradía
curl http://localhost:8000/scores/intraday/latest
```

### Intervalos soportados

yfinance ofrece barras intradía en `15m` y `60m` (entre otras).
MAREA usa **60m por defecto** (configurable con `INTRADAY_INTERVAL=15m` en `.env`).
El `period` de descarga es `5d` por defecto (`INTRADAY_PERIOD=5d`).

> **Nota importante**: los datos de yfinance intradía **no son tiempo real
> estricto** — son barras de 15 o 60 minutos con un retraso de ~15 minutos
> para el mercado US. Crypto (CoinGecko) se actualiza cada pocos minutos.
> Use estos datos como orientación de corto plazo, no como feed de trading.

### Variables de entorno intradía

```
INTRADAY_INTERVAL=60m        # '60m' | '15m'
INTRADAY_PERIOD=5d           # period de yfinance (cuánto histórico cargar)
INTRADAY_FLOW_THRESHOLD=0.6  # |score intradía| que dispara alerta Telegram
```

### Cold start intradía

Al igual que el carril diario, el intradía arranca en frío: con pocas barras
el score tiene `confidence='low'` y **no se envían alertas**. Se necesitan al
menos `score_min_obs` (defecto: 10) observaciones para `confidence='ok'`.

## Universo de assets (32 en total)

| Grupo | Fuente | Tickers |
|-------|--------|---------|
| Índices macro | yfinance | ^GSPC ^IXIC ^IBEX ^N225 |
| Commodities | yfinance | GC=F SI=F |
| Macro / tipos | yfinance | DX-Y.NYB ^VIX ^TNX |
| ETFs principales | yfinance | SPY QQQ GLD SLV IBIT |
| ETFs sectoriales | yfinance | SOXX SMH XME GDX SIL ITA XAR XLE XLK XLF XLV |
| Crypto spot | CoinGecko | BTC ETH |
| Crypto perpetuos | Binance Futures | BTC_PERP ETH_PERP |
| Stablecoin supply | DefiLlama | STABLES_USDT STABLES_USDC |
| Sentimiento | Alternative.me | CRYPTO_FNG |

---

## Requisitos

- Python 3.11+
- Cuenta Supabase (free tier suficiente)

---

## 1. Configurar entorno

```bash
cd marea
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

---

## 2. Configurar variables de entorno

```bash
cp .env.example .env
```

Edita `.env` con tus credenciales de Supabase (Project Settings > API):

```
SUPABASE_URL=https://xxxxxxxxxxxxxxxxxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## 3. Correr migraciones en Supabase

Abre el **SQL Editor** de tu proyecto Supabase y ejecuta en orden:

```
migrations/001_init.sql   ← tablas + 25 assets yfinance
migrations/002_crypto_assets.sql  ← amplía CHECK constraint + 7 assets crypto
```

> Alternativa con psql:
> ```bash
> psql "postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres" \
>   -f migrations/001_init.sql \
>   -f migrations/002_crypto_assets.sql
> ```

---

## 4. Levantar la API en local

```bash
uvicorn app.main:app --reload --port 8000
```

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/health` | Estado de la API |
| GET | `/ingest/run` | Dispara ingesta completa (yfinance + crypto + on-chain) |
| GET | `/docs` | Swagger UI automático |

---

## 5. Lanzar ingesta manual

```bash
curl http://localhost:8000/ingest/run
```

Respuesta por fuente:

```json
{
  "total_snapshots": 32,
  "ok": true,
  "by_source": {
    "yfinance_fixed": {"snapshots_inserted": 25, "errors": [], "ok": true},
    "coingecko":      {"snapshots_inserted": 2,  "errors": [], "ok": true},
    "defillama":      {"snapshots_inserted": 2,  "errors": [], "ok": true},
    "binance":        {"snapshots_inserted": 2,  "errors": [], "ok": true},
    "fng":            {"snapshots_inserted": 1,  "errors": [], "ok": true}
  },
  "errors": []
}
```

---

## 6. Correr tests

```bash
pytest tests/ -v
```

28 tests, todos mockeados (sin red, sin Supabase real).

---

## Arquitectura Sesiones 1-2

```
app/
  main.py              FastAPI + lifespan del scheduler
  config.py            pydantic-settings (.env)
  db.py                Cliente Supabase (singleton)
  universe/
    fixed.py           25 assets yfinance + 7 assets crypto
  ingest/
    _base.py           fetch_json (retry+backoff), upsert_records, day_ts
    yfinance_fixed.py  Lote único yf.download() — 25 tickers
    crypto_coingecko.py  BTC+ETH en 1 llamada /coins/markets
    crypto_defillama.py  USDT+USDC supply en 1 llamada /stablecoins
    crypto_binance.py    funding+OI: 1 premiumIndex + 2 openInterest
    crypto_fng.py        Fear&Greed en 1 llamada /fng/
    run_all.py           Orquestador: 5 fuentes, errores independientes
  scheduler.py         APScheduler → IngestAll diario (UTC)
migrations/
  001_init.sql         Tablas + constraints + 25 seeds yfinance
  002_crypto_assets.sql  Amplía CHECK + 7 seeds crypto/on-chain
tests/
  conftest.py          Env vars de test
  test_ingest.py       28 tests (S1 + S2)
```

### Decisiones de diseño clave

**BTC_PERP ≠ BTC**: Binance escribe mark price + funding/OI en tickers propios
(`BTC_PERP`, `ETH_PERP`) para evitar conflicto de upsert con CoinGecko
que escribe el precio spot en `BTC`/`ETH`. Sin esto, el segundo upsert
sobreescribiría silenciosamente los campos del primero.

**Stablecoins separadas**: `STABLES_USDT` y `STABLES_USDC` como assets
independientes (no un único "STABLES" agregado) para poder rastrear
la divergencia USDT/USDC, que es una señal de riesgo regulatorio/liquidez.

**`extra` jsonb**: todos los campos no-OHLCV (market_cap, funding_rate,
open_interest, supply_prev_day, value_classification) van en `extra`.
Esto evita migraciones de columna cada vez que añadimos un campo nuevo.

**Filtro `source` en yfinance**: `_load_asset_map` ahora filtra
`.eq("source", "yfinance")` para que los tickers crypto no lleguen
a `yf.download()` y den error.

**`day_ts()`**: las fuentes HTTP no tienen barra diaria, usan UTC midnight
del día actual. Esto alinea todos los snapshots al mismo timestamp
que los bars diarios de yfinance para joins limpios.

**Anti-baneo CoinGecko**: 1 sola llamada con `ids=bitcoin,ethereum`.
`fetch_json` en `_base.py` reintenta con backoff 2s→5s→15s.
El orquestador duerme 1s entre fuentes HTTP.

---

## Sesión 5 — Análisis intermercado

### Dos matrices de correlación

**Matriz A (intermarket)** correlaciones entre clases de activo:

| Clase | Activos incluidos |
|-------|------------------|
| `crypto` | BTC, ETH, BTC_PERP, ETH_PERP, IBIT, top-N dinámico |
| `equities` | ^GSPC, ^IXIC, ^IBEX, ^N225, SPY, QQQ, top-50 stocks |
| `gold` | GC=F, GLD |
| `silver` | SI=F, SLV |
| `bonds` | ^TNX (score invertido: yield↑ = outflow bonos) |
| `dollar` | DX-Y.NYB |
| `vix` | ^VIX (score invertido: VIX↑ = miedo) |

**Matriz B (sector)** correlaciones entre ETFs sectoriales:
`SOXX · SMH · XME · GDX · SIL · ITA · XAR · XLE · XLK · XLF · XLV`

Ambas matrices se calculan con ventana **7d** y **30d** sobre los flow_scores diarios.

### Detección de desacople

Un par se declara en desacople (`is_decoupling=True` en el row 7d) cuando:
- Correlación base (30d): `|corr_30d| ≥ 0.7` (estaban correlacionados)
- Caída reciente (7d): `|corr_7d − corr_30d| ≥ 0.5` (ahora divergen)

### Régimen de mercado (clasificador solo-reglas)

| Régimen | Condiciones core (flujo) | Moduladores (contexto) |
|---------|--------------------------|------------------------|
| `risk_on` | crypto\_inflow OR equity\_inflow | dxy\_falling, vix\_calm |
| `risk_off` | crypto\_outflow OR equity\_outflow | dxy\_rising, vix\_fearful |
| `flight_to_safety` | (gold\_inflow OR bonds\_inflow) AND (crypto\_outflow OR equity\_outflow) | dxy\_rising |
| `sector_rotation` | delegado desde sector.py cuando macro es neutral | — |
| `neutral` | señales insuficientes o contradictorias | — |

**DXY y VIX son moduladores, no triggers**: solos no pueden activar ningún régimen.
Cada modulador alineado añade `+0.15` de confianza sobre el máximo core de `0.70`.

**Confianza**: `min(1.0, (cores_disparados/total_cores) × 0.70 + n_moduladores × 0.15)`

### Detección de rotación sectorial

Un evento `(from_sector → to_sector)` se detecta cuando:
- `score(from_sector) < −0.25` (outflow claro)
- `score(to_sector) > +0.25` (inflow claro) simultáneamente

`strength = min(|score_outflow|, |score_inflow|)`

### Nuevas tablas (migración 005)

| Tabla | UNIQUE constraint |
|-------|------------------|
| `regimes` | `(ts, window)` |
| `correlations` | `(ts, window, matrix_type, pair_a, pair_b)` |
| `rotations` | `(ts, from_sector, to_sector)` |

### Nuevos endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/analysis/run` | Corre todo el análisis, devuelve régimen + nº desacoples + rotaciones |
| GET | `/analysis/regime/latest` | Último régimen + señales disparadas |
| GET | `/analysis/correlations?type=intermarket\|sector` | Matriz actual de correlaciones |

---

## ⚠️ Sesión 6 — Mapa de exposición indirecta

> **ADVERTENCIA**: Las exposiciones marcadas como `rumor_prensa` o `especulacion` son
> **hipótesis generadas por IA** y NO han sido verificadas manualmente.
> **NO constituyen asesoramiento de inversión.**
> Verifique siempre las fuentes originales antes de tomar cualquier decisión.

### Cómo funciona el descubrimiento

1. **Objetivos configurados** (no hardcodeados como hechos): OpenAI, Anthropic, SpaceX,
   Stripe, Databricks para pre-IPO; BTC, ETH + universo dinámico S3 para crypto.
2. **LLM con búsqueda web obligatoria**: Groq `compound-beta` (búsqueda servidor) con
   fallback a Gemini 1.5 Flash (Google Search grounding). El prompt fuerza búsqueda
   real — nunca respuestas de memoria.
3. **Extracción estructurada**: el LLM devuelve JSON `{exposed_ticker, relationship, source_urls[]}`.
4. **Verificación dura** (`verify.py`): sin URL → candidato DESCARTADO. Sin excepciones.

### Política de verificación y confianza

| Nivel | Condición | ¿Hipótesis? |
|-------|-----------|-------------|
| `confirmado_oficial` | Fuente = SEC (sec.gov), IR corporativo (`ir.*`), newsroom oficial | No |
| `rumor_prensa` | Fuente = medio reconocido (Reuters, Bloomberg, FT, WSJ…) | **Sí** |
| `especulacion` | Cualquier otra URL válida | **Sí — marcado SIN VERIFICAR** |
| Descartado | Sin URL válida | No persiste |

**Invariante de persistencia**: la tabla `exposures` tiene constraint `jsonb_array_length(sources) > 0`.
El código también bloquea el upsert antes de llegar a la BD. Es imposible persistir sin fuentes.

**Trade-off explícito**: preferimos falsos negativos (perder una exposición real sin fuente)
a falsos positivos (inventar participaciones). Callar es más seguro que inventar.

### Nuevos endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/exposure/discover` | Lanza descubrimiento LLM para todos los objetivos |
| GET | `/exposure/map?type=pre_ipo\|crypto` | Mapa actual; baja confianza = hipótesis visible |

### Variables de entorno requeridas

```env
GROQ_API_KEY=gsk_...        # Groq console → compound-beta
GEMINI_API_KEY=AIza...      # Google AI Studio → gemini-1.5-flash
```

---

## Sesión 7 — Capa narrativa LLM

Convierte el snapshot numérico en lenguaje natural explicando el *por qué* de
los flujos de liquidez observados.

### Diseño: narrativa cerrada (sin búsqueda web)

El LLM recibe **solo datos internos de MAREA** y los explica. No puede
introducir causas externas ("por la Fed", "por la guerra en X") porque no las
conoce — las inventaría. Esto garantiza que la narrativa no puede alucinar.

**Motor**: Groq `llama-3.3-70b-versatile` (NO `compound-beta` — el modelo direct
no tiene herramientas web servidor-lado). La misma `GROQ_API_KEY` de S6.

### Sello de interpretación — OBLIGATORIO

Toda narrativa se muestra siempre con el sello visible:

> **"Interpretación automática de datos · no es consejo de inversión."**

### Qué contiene el snapshot

| Sección | Fuente | Descripción |
|---------|--------|-------------|
| `regime` | `regimes` | Régimen actual (window=7d) + confianza + señales |
| `top_inflow` / `top_outflow` | `flow_scores` | Top-3 scores más positivos / negativos (7d, dedup por asset) |
| `class_scores` | `flow_scores` | Score promedio por clase de activo |
| `cold_start` | `flow_scores` | True si >50 % de scores tienen confianza="low" |
| `decouplings` | `correlations` | Pares con `is_decoupling=True` en window=7d |
| `rotations` | `rotations` | Últimas 5 rotaciones sectoriales |
| `exposures` | `exposures` | Hasta 5 exposiciones (mayor confianza primero) |

El snapshot se mantiene **compacto** (no cientos de filas) para no disparar
tokens innecesarios. Lo que no está en el snapshot no puede llegar a la narrativa.

### Prohibiciones explícitas en el prompt

El `_SYSTEM_PROMPT` prohíbe expresamente:
- Predicciones de precio ("va a subir", "caerá", "alcanzará X").
- Consejos de inversión ("comprar", "vender", "acumular", "all-in").
- Causas externas no presentes en los datos.
- Lenguaje de certeza ("definitivamente", "seguro que").

Y obliga a:
- Lenguaje observacional: "los datos muestran…", "el patrón es consistente con…".
- Marcar incertidumbre cuando `confidence < 40 %` o `cold_start = True`.

### Auditoría post-hoc

La tabla `narratives` guarda `snapshot_json` (el snapshot exacto que recibió el
LLM) junto con cada narrativa. Permite responder a posteriori la pregunta
"¿por qué el LLM dijo X?".

### Nuevos endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/narrative/generate` | Construye snapshot, genera narrativa, persiste, devuelve texto + sello |
| GET | `/narrative/latest` | Última narrativa con sello de interpretación y confianza |

### Nueva migración

`migrations/007_narratives.sql` — tabla `narratives`:
- `UNIQUE (ts)`: idempotente, regenerar para el mismo día actualiza en vez de duplicar.
- `snapshot_json JSONB`: el snapshot completo para auditoría.

### Variables de entorno (sin cambios respecto a S6)

```env
GROQ_API_KEY=gsk_...   # misma clave de S6 — se usa con modelo distinto
```

---

---

## Sesión 8 — Motor de alertas + bot de Telegram

MAREA pasa de consultable a **proactivo**: detecta cambios de estado relevantes
y avisa en Telegram sin que nadie tenga que hacer polling.

> ⚠️ **Las alertas NO son consejo de inversión.** Son interpretaciones automáticas
> de datos de flujo. Verifica siempre las fuentes antes de operar.

### Los 4 tipos de alerta (configurables vía `.env`)

| Tipo | Disparador | Umbral configurable |
|------|-----------|---------------------|
| `flow_extreme` | `\|score 7d\|` supera el umbral en cualquier asset | `FLOW_EXTREME_THRESHOLD=0.7` |
| `regime_change` | Régimen actual ≠ último régimen avisado | `MIN_ALERT_CONFIDENCE=0.4` |
| `decoupling` | Par marcado `is_decoupling=True` en correlaciones (S5) | — |
| `exposure` | Nueva exposición indirecta descubierta (S6), con confianza + fuentes | — |

### Anti-duplicado por cambio de estado (no spam)

La alerta se dispara solo en la **TRANSICIÓN**, no mientras el estado persiste:

- **Cambio de régimen**: avisa en `risk_on → risk_off`; NO en cada ciclo que
  siga en `risk_off`.
- **Flow extreme**: avisa cuando el score **cruza** el umbral. Con **histéresis**:
  una vez avisado, se re-arma solo cuando el score baja del umbral (evita spam
  en el borde ±threshold).
- **Desacople / exposición**: no reenvía el mismo estado ya avisado.

La tabla `alerts` actúa como registro de lo enviado. `UNIQUE(alert_type, entity, state)`
garantiza una sola fila por transición; el re-armado actualiza `sent=false` en vez
de insertar duplicados.

### Umbral de confianza mínimo (`MIN_ALERT_CONFIDENCE=0.4`)

Coherente con las sesiones anteriores: **no despertar el móvil por datos de cold start**.

| Situación | Comportamiento |
|-----------|---------------|
| Confianza datos ≥ 0.4 y estado nuevo | Se envía a Telegram |
| Confianza datos < 0.4 (cold start, pocos días de datos) | Se **registra** en `alerts` con `sent=false, not_sent_reason='low_confidence'`; NO se envía |
| Estado ya avisado (dedup) | Se registra con `not_sent_reason='duplicate'`; NO se envía |

### Alertas de exposición indirecta

Siempre incluyen:
- Nivel de confianza: `✅ CONFIRMADO OFICIAL` / `📰 RUMOR PRENSA` / `🔮 ESPECULACIÓN`
- Las fuentes (URLs reales, hasta 3)
- Marca `⚠️ SIN VERIFICAR — hipótesis especulativa` si confianza < confirmado_oficial
- Recordatorio de que no es consejo de inversión

### Verificar el bot antes de usar

```bash
# Lanza un mensaje de prueba para confirmar que token + chat_id funcionan:
curl -X POST http://localhost:8000/alerts/test
```

### Nuevos endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/alerts/run` | Evalúa las 4 reglas y envía lo que proceda. Devuelve resumen. |
| GET | `/alerts/recent` | Últimas alertas registradas (enviadas y no, con razón) |
| POST | `/alerts/test` | Mensaje de prueba a Telegram para verificar la configuración |

### Nueva migración

`migrations/008_alerts.sql` — tabla `alerts`:
- `UNIQUE (alert_type, entity, state)`: un slot por estado; permite re-armado.
- `sent` / `not_sent_reason`: auditoría de qué se envió y por qué no.

### Variables de entorno requeridas

```env
# Obtén el token en @BotFather (Telegram); el chat_id con @userinfobot
TELEGRAM_BOT_TOKEN=tu_token_aqui
TELEGRAM_CHAT_ID=tu_chat_id_aqui

# Umbrales opcionales (estos son los valores por defecto):
# FLOW_EXTREME_THRESHOLD=0.7
# MIN_ALERT_CONFIDENCE=0.4
```

---

---

## Sesión 9 — Dashboard Streamlit

Pantalla única de solo-lectura donde se ve el estado del mercado de un vistazo.
**No recalcula ni dispara ingestas** — lee de Supabase directamente.

### Arrancar el dashboard en local

```bash
# Desde la raíz del proyecto (con el venv activo):
streamlit run dashboard/app.py
```

Abre automáticamente en `http://localhost:8501`.

### Secciones del dashboard

| Sección | Descripción |
|---------|-------------|
| **Régimen actual (cabecera)** | Régimen vigente + confianza + señales disparadas. Badge "Datos preliminares" si confianza < 40 %. |
| **Tab: Narrativa** | Texto LLM más reciente con sello obligatorio de interpretación + nivel de confianza. |
| **Tab: Heatmap de Flujos** | Barras coloreadas (verde = inflow, rojo = outflow) por asset. Assets con `*` = baja confianza. |
| **Tab: Correlaciones** | Heatmaps interactivos de las matrices intermarket y sector. ⚠ marca pares en desacople. |
| **Tab: Rotación Sectorial** | Tabla de rotaciones detectadas (from → to + intensidad). |
| **Tab: Exposición Indirecta** | Tabla de exposiciones con nivel de confianza y fuentes clicables. Las de baja confianza se marcan como "SIN VERIFICAR — hipótesis". |
| **Tab: Timeline de Régimen** | Histórico de regímenes como gráfico scatter coloreado. |
| **Tab: Alertas Recientes** | Alertas enviadas y no enviadas (con razón: `low_confidence` / `duplicate`). |

### Invariantes de seguridad

- **Solo-lectura**: no llama a `/ingest/run`, `/analysis/run`, ni ningún endpoint de escritura.
- **Sello obligatorio**: la narrativa siempre muestra "Interpretación automática de datos · no es consejo de inversión."
- **Baja confianza marcada**: todos los datos con confianza < 40 % o cold start aparecen atenuados / con badge de advertencia.
- **Hipótesis marcadas**: exposiciones `rumor_prensa` o `especulacion` llevan "SIN VERIFICAR — hipótesis generada por IA".
- **Estado vacío amable**: si la BD está vacía, muestra mensajes informativos en vez de errores.
- **Caché 5 min** (`st.cache_data`): no martillea Supabase en cada interacción.

### Estructura

```
dashboard/
  __init__.py
  app.py           # App Streamlit principal (8 secciones)
  data.py          # Lectura de Supabase + cache st.cache_data (TTL 5 min)
  components.py    # Helpers de render: heatmaps Plotly, badges, empty states
requirements-dashboard.txt  # streamlit + plotly (también en requirements.txt)
```

### Tests del dashboard

```bash
pytest tests/test_dashboard_data.py -v
```

26 tests que cubren las funciones `_fetch_*` de `data.py` con Supabase mockeado
(sin red, sin BD real). Los 319 tests previos siguen verdes (345 total).

---

## Sesión 11 — Mensaje-resumen por ciclo en Telegram (señal de vida)

Las alertas de la S8 solo llegan **por evento** (régimen, flujo extremo…) y con
filtro de confianza. En cold start o mercado tranquilo, Telegram queda en
silencio y no se distingue *"funciona y calla"* de *"está roto"*.

La S11 añade un **mensaje-resumen que se envía SIEMPRE en cada ciclo** (intradía
y diario), haya o no alertas. Cumple dos funciones:

1. **Foto del mercado** en ese momento (apertura / media sesión / cierre).
2. **Señal de vida**: si llega, MAREA corre; si un día no llega, algo falló.

Es **adicional** a las alertas, **no** las sustituye: las alertas (confianza
alta, anti-duplicado, histéresis) siguen igual, por encima del resumen. El
resumen **no** usa anti-duplicado (es intencional que llegue en cada ciclo).

### Qué contiene

> **Nota:** el **formato** descrito en esta tabla fue **rediseñado en la
> Sesión 12** ("sigue la liquidez"). Lo de aquí es el contenido original de la
> S11; el formato vigente (semáforo, top 5, cierre de pólvora, comparación
> temporal, "quién manda") está documentado en la **Sesión 12**. La S11 sigue
> siendo la responsable de que el parte **se envíe siempre** (señal de vida).

| Resumen DIARIO (22:30) | Resumen INTRADÍA (15:30 / 18:00 / 20:00) |
|------------------------|------------------------------------------|
| Régimen + confianza **real** + señales en lenguaje claro | Momento del día (apertura / media sesión / tarde) |
| Top 3 entradas y top 3 salidas por activo (con valor) | Movimientos intradía fuertes (o "sin movimientos") |
| Rotación sectorial destacada | Contexto DXY / VIX intradía |
| Una línea de la narrativa más reciente | Sello "no es consejo de inversión" |
| Sello "no es consejo de inversión" | |

### Honestidad sobre la confianza

Coherente con la narrativa y el dashboard: si los datos están en **cold start /
baja confianza**, el resumen lo dice explícitamente con
**⚠️ *Datos preliminares (histórico insuficiente, baja confianza)*** al
principio. Cuando los datos maduran (confianza ok), esa coletilla **desaparece
sola**. Nunca se presenta un dato preliminar como sólido.

### Dónde vive

- `app/alerts/digest.py` — composición pura (`build_daily_digest`,
  `build_intraday_digest`) + envío (`send_daily_digest`, `send_intraday_digest`).
  Reutiliza el cliente Telegram existente (`app/alerts/telegram.py`), no lo duplica.
- Se engancha como **paso final** de cada ciclo en `scripts/run_*_cycle.py`,
  después de ingesta → scores → análisis → [narrativa] → alertas.
- Si el envío de Telegram falla: **log y se continúa** (no tumba el ciclo).

### Desactivarlo (sin tocar código)

Variable de entorno **`DIGEST_ENABLED`** (por defecto `true`). Para apagar el
resumen, añádela como GitHub Secret (o en tu `.env` local) con valor `false`:

```bash
DIGEST_ENABLED=false
```

> En GitHub: **Settings → Secrets and variables → Actions → New repository
> secret**, nombre `DIGEST_ENABLED`, valor `false`. Si no la defines, el
> resumen se envía (comportamiento por defecto).

---

## Sesión 12 — Rediseño de los partes: "sigue la liquidez"

Los partes de la S11 eran demasiado *carcasa*: listaban números (`^GSPC +1.00`)
sin explicar qué significan ni hacia dónde va el dinero. La S12 los reescribe
para que **sigan la liquidez y la expliquen**, bajo una regla madre.

### Principio rector — no dejar nada a medias

> Todo flujo que sale va a algún sitio, **o se declara en espera**. Toda pólvora
> de stablecoins liberada dispara hacia un destino **o se declara sin destino**.
> Toda tensión entre fuerzas se resuelve diciendo **cuál manda** (o se declara
> empate). Nunca se suelta una frase insinuante sin cerrarla.

Cerrar el círculo es a veces *"esto fue a X"* (cuando los datos lo respaldan) y a
veces *"esto está en espera, sin destino visible"* (cuando no). **Las dos
cierran.** Lo prohibido es inventar un destino que los datos no respaldan, o
dejar la frase colgando.

### Afirmativo (flujo) vs condicional (destino / precio)

| Nivel | Lenguaje | Ejemplo |
|-------|----------|---------|
| Lo que la liquidez **hizo** | **afirmativo** (hecho observado) | "Sale capital de bancos; entra en bolsa USA." |
| El **destino inferido** por simultaneidad | **condicional** ("parece dirigirse a / apunta a") | "…parece dirigirse a S&P 500 y Dólar." |
| El salto al **precio futuro** | **no se hace** | MAREA describe flujo, no predice precio. |

> **Inferencia por simultaneidad, no rastreo.** Para decir "el dinero que sale de
> X parece ir a Y", MAREA mira qué activos **reciben inflow ≥ moderado en el
> mismo momento** en que X tiene salida. Si hay receptores claros, los nombra
> como destino probable; si no, declara "sin destino visible — capital en
> espera". MAREA **no puede rastrear** el dinero; el lenguaje condicional lo deja
> claro siempre.

### De números a significado

- **Nombres reales, no tickers:** `^GSPC → S&P 500`, `DX-Y.NYB → Dólar (DXY)`,
  `XLF → Financieras (bancos)`, `GC=F → Oro`, `BTC → Bitcoin`… (diccionario
  `_READABLE` en `digest.py`, con respaldo a `assets.name` y al ticker).
- **Intensidad graduada por el score:** `|score| ≥ 0.85 → fuerte` ·
  `0.5–0.85 → moderada` · `< 0.5 → leve`. El lenguaje refleja la magnitud real.
- **Termómetros excluidos de los flujos:** el VIX y el Fear&Greed son
  sentimiento, no vasijas de liquidez → se excluyen de rankings/destino para no
  escribir "el capital se fue al VIX".

### Semáforo (umbrales documentados)

Cada parte empieza con un titular tipo cabecera de periódico, precedido de un
semáforo según `max|score|` de los flujos:

| Color | Condición |
|-------|-----------|
| 🟢 tranquilo | `max|score| < 0.5` (ninguna fuerza supera lo moderado) |
| 🟡 normal | `0.5 ≤ max|score| < 0.85` |
| 🔴 fuerte | `max|score| ≥ 0.85`, **o** rotación sectorial fuerte, **o** régimen risk-off/refugio con confianza ≥ 0.6 |

> En **cold start** nunca se pinta 🔴 (los datos no son fiables): por defecto 🟡,
> y 🟢 solo si de verdad todo está plano.

### Estructura del parte DIARIO

1. **Semáforo + titular** (cabecera de periódico).
2. Cabecera `📊 MAREA — Cierre de mercado` + coletilla *datos preliminares* si
   cold start, + subtítulo `🔄 vs. [parte de comparación]`.
3. `🔥 Lo más fuerte` — los 1-2 movimientos de mayor intensidad.
4. `🟢 Más entrada de liquidez` — **TOP 5** con nombre + intensidad + score.
5. `🔴 Más salida de liquidez` — **TOP 5**; **cada salida ≥ moderada se cierra
   con su destino inferido o un "en espera"** (no dejar a medias).
6. `💰 En crypto` — **siempre** nombres y dirección concretos + **cierre de la
   pólvora** de stablecoins (uno de: a crypto / a otro lado / en espera / acumulándose).
7. `🔄 Cambio desde [parte anterior]` — la "película": delta por activo,
   giros de signo, intensificaciones, con **origen→destino**.
8. `⚡ Quién manda` — **dictamina** la fuerza de mayor `|score|` y deriva la
   presión, o declara **"señales cruzadas"** si están parejas (`Δ < 0.12`).
9. `📈 Fondo` — régimen + confianza **real** (penalizada por cold start).
10. Sello `⚠️ Interpretación automática · no es consejo de inversión.`

El parte **INTRADÍA** es la versión corta: semáforo + titular, **Top 3** in/out,
crypto + pólvora (igual de estricto), cambio temporal, quién manda, sello.
Omite el bloque largo de fondo (el régimen es del diario).

### Capa de comparación temporal (la "película" en vez de "foto")

Cada parte se compara con el **anterior relevante** y compone el bloque `🔄`:

| Parte | Se compara con |
|-------|----------------|
| Apertura | cierre anterior |
| Media sesión | apertura de hoy |
| Cierre | media sesión de hoy |

El mapeo vive en `_COMPARE_MAP` (configurable). Cada ciclo **persiste sus flow
scores** en la tabla `digest_cycles` (migración **011**), etiquetados con su
**momento** (apertura / media / cierre) y carril (daily / intraday); el siguiente
parte lee la fila previa por orden de emisión real (`created_at`) y calcula los
deltas. Guardar los scores como JSONB mantiene la comparación uniforme entre el
carril diario (ventana 7d) y el intradía (4h) — se comparan contra lo que cada
parte realmente reportó, no mezclando ventanas.

> **Degradación elegante (cold start).** Mientras no haya parte anterior, el
> bloque dice explícitamente *"sin parte anterior suficiente para comparar
> todavía"* — **no se inventa** una comparación. La capa cobra valor sola a
> medida que MAREA acumula histórico; `digest_cycles` se llena sin backfill.

### Groq redacta, los datos mandan

Se reutiliza la narrativa de Groq existente solo como **frase de color** en
cursiva. **Las cifras, rankings, destino inferido, cierre de pólvora y
"quién manda" salen de REGLAS sobre los flow scores reales**, nunca de que Groq
se los invente.

### Dónde vive

- `app/alerts/digest.py` — helpers puros (`_name`, `_intensity`, `_semaphore`,
  `_infer_destination`, `_powder_line`, `_who_dominates`, `_compare_block`…),
  los dos builders y los senders (cargan estado real, leen el ciclo anterior,
  componen, envían y persisten).
- `migrations/011_digest_cycles.sql` — tabla de comparación temporal.
- `tests/test_digest.py` — cubre los siete entregables (no dejar a medias,
  pólvora ×3, quién manda, comparación, nombres, intensidad, semáforo) sin tocar
  Telegram ni la BD reales.

---

## Sesión 10 — Despliegue y automatización (GitHub Actions, coste cero)

A partir de aquí MAREA corre **sola**, sin arrancarla a mano y sin pagar nada.
El "reloj" que la dispara es **GitHub Actions** (cron gratuito). La base de
datos ya vive en Supabase, así que aquí solo automatizamos la ejecución.

> **Nada de Google Cloud, Railway ni Render.** Solo GitHub Actions + Supabase.

### Cómo funciona (visión general)

Hay **dos ciclos** con ritmos distintos, cada uno con su propio workflow:

| Ciclo | Cuándo (hora española, verano) | Qué ejecuta | Workflow |
|-------|-------------------------------|-------------|----------|
| **Intradía** | 15:30, 18:00 y 20:00 | ingesta intradía → scores intradía → análisis intradía → alertas | `.github/workflows/intraday.yml` |
| **Diario** | 22:30 (tras cierre USA) | ingesta diaria → universo → scores → análisis → narrativa → alertas | `.github/workflows/daily.yml` |

Cada workflow **no levanta la API web**: ejecuta directamente un script de
Python que llama a los engines internos en orden. Son:

```
scripts/run_intraday_cycle.py   # cadena intradía
scripts/run_daily_cycle.py      # cadena diaria
scripts/_common.py              # motor de ciclo compartido (orden + errores + aviso Telegram)
```

Estos scripts **reutilizan la lógica existente** (los mismos engines que sirven
los endpoints HTTP); no duplican nada.

### Horarios: por qué el cron está en UTC

GitHub Actions programa **siempre en UTC**. España peninsular en **verano**
(CEST) es **UTC+2**. La conversión usada en los workflows:

| Hora España (verano) | UTC (cron) | Ciclo |
|----------------------|-----------|-------|
| 15:30 (apertura USA) | **13:30** → `30 13 * * *` | intradía |
| 18:00 (media sesión) | **16:00** → `0 16 * * *` | intradía |
| 20:00 (tarde USA) | **18:00** → `0 18 * * *` | intradía |
| 22:30 (tras cierre USA) | **20:30** → `30 20 * * *` | diario |

> ⚠️ **Horario de invierno (CET = UTC+1):** del último domingo de octubre al
> último de marzo, para mantener los **mismos horarios locales** habría que
> **restar una hora** a cada cron UTC (intradía: 12:30, 15:00, 17:00;
> diario: 21:30). Está documentado como comentario dentro de cada `.yml`.

### Alertas: ambos carriles, sin spam

Los dos ciclos terminan evaluando alertas y enviando a Telegram **lo que
proceda**, respetando **todo** lo ya construido en el motor de alertas:

- **Anti-duplicado por cambio de estado** (un régimen que persiste no redispara).
- **Histéresis** (no re-alerta mientras sigue por encima del umbral).
- **Umbral de confianza mínimo** (`MIN_ALERT_CONFIDENCE`): en **cold start** o
  baja confianza **no se envía nada**. Es **normal y correcto** que las
  primeras ejecuciones casi no manden mensajes. No se fuerza ningún envío.

### Robustez (está desatendido)

- Cada paso del ciclo se ejecuta aislado: si **una fuente** falla, se loguea y
  **se continúa** (igual que la ingesta ya hacía). Que falle una sola API no
  tumba el ciclo.
- Si un paso **casca del todo** (excepción), el ciclo termina con **exit code
  ≠ 0** → GitHub marca la ejecución como **fallida** (aspa roja), y además se
  intenta **avisar por Telegram** con un mensaje breve de error, para enterarse
  sin mirar GitHub.
- Todo el log va a **stdout**, que GitHub Actions captura para depurar.

---

## Despliegue paso a paso (para no técnicos)

> Sigue esto **una sola vez**. Después MAREA funciona sola para siempre.

### Paso 1 — Crear el repositorio en GitHub (privado)

1. Entra en <https://github.com> e inicia sesión (o crea una cuenta gratis).
2. Arriba a la derecha, pulsa el **+** → **New repository**.
3. **Repository name:** `marea` (o el que quieras).
4. Marca **Private** (privado: nadie más lo verá).
5. **NO** marques "Add a README" ni nada más (el proyecto ya tiene archivos).
6. Pulsa **Create repository**. Verás una página con instrucciones; ignórala,
   usaremos los comandos de abajo.

### Paso 2 — Subir el proyecto

Abre una terminal **en la carpeta del proyecto** (`marea`) y ejecuta, **una
línea cada vez**. Sustituye `TU_USUARIO` por tu usuario de GitHub:

```bash
git init
git add .
git commit -m "MAREA: despliegue inicial con automatización GitHub Actions"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/marea.git
git push -u origin main
```

> Si `git push` te pide usuario y contraseña, la "contraseña" debe ser un
> **Personal Access Token** (GitHub ya no acepta la contraseña normal). Lo creas
> en GitHub → tu foto → **Settings** → **Developer settings** → **Personal
> access tokens** → **Tokens (classic)** → **Generate new token**, marca el
> permiso **repo**, cópialo y pégalo cuando te pida la contraseña.

### Paso 3 — Confirmar que el `.env` NO se sube (importante)

El archivo `.env` contiene tus credenciales y **nunca** debe subirse. Ya está
protegido en `.gitignore`. Verifícalo:

```bash
git status --ignored        # .env debe aparecer bajo "Ignored files"
git ls-files | grep .env    # debe mostrar SOLO ".env.example", nunca ".env"
```

- ✅ Correcto: aparece `.env.example` (la plantilla, sin secretos).
- ❌ Si vieras `.env` en la lista de archivos rastreados, **detente** y avisa:
  no debe estar ahí.

El `.env.example` está limpio (solo placeholders), así que es seguro que esté
en el repo: sirve de plantilla.

### Paso 4 — Añadir los GitHub Secrets (las credenciales)

Las credenciales que tienes en tu `.env` local se configuran en GitHub como
**Secrets** (GitHub los guarda cifrados; ni tú vuelves a verlos). En tu
repositorio:

1. Pestaña **Settings** (arriba del repo).
2. Menú izquierdo: **Secrets and variables** → **Actions**.
3. Botón **New repository secret**.
4. Añade **uno por uno** estos secrets (nombre EXACTO en mayúsculas, y el valor
   copiado de tu `.env` local):

| Name (exacto) | Value (de tu `.env`) |
|---------------|----------------------|
| `SUPABASE_URL` | tu URL de Supabase (`https://....supabase.co`) |
| `SUPABASE_KEY` | tu anon key de Supabase |
| `GROQ_API_KEY` | tu key de Groq (`gsk_...`) |
| `TELEGRAM_BOT_TOKEN` | el token del bot (de @BotFather) |
| `TELEGRAM_CHAT_ID` | tu chat id (de @userinfobot) |

   Para cada uno: escribe el **Name**, pega el **Value**, pulsa **Add secret**.
   Repite hasta tener **los cinco**.

> Los workflows leen estos secrets y se los pasan a los scripts como variables
> de entorno. Nunca aparecen en el código ni en los logs.

> **Opcional — `DIGEST_ENABLED`:** no hace falta añadirlo. Solo si algún día
> quieres **apagar el mensaje-resumen** (ver Sesión 11) crea un secret más
> llamado `DIGEST_ENABLED` con valor `false`. Si no existe, el resumen se envía.

### Paso 5 — Verificar que los workflows aparecen

1. Pestaña **Actions** (arriba del repo).
2. Deberías ver en la izquierda **«MAREA — Ciclo intradía»** y **«MAREA —
   Ciclo diario»**. Si los ves, GitHub ya reconoció los workflows. ✅

### Paso 6 — Probar un workflow A MANO (sin esperar al cron)

Ambos workflows tienen `workflow_dispatch`, que permite lanzarlos manualmente:

1. Pestaña **Actions** → pulsa **«MAREA — Ciclo diario»** (en la izquierda).
2. A la derecha, botón **Run workflow** → elige rama `main` → **Run workflow**.
3. Espera unos segundos y refresca: aparecerá una ejecución en curso (círculo
   amarillo). Al terminar será ✅ verde o ❌ roja.

> En las **primeras** ejecuciones es **normal** que no llegue casi ninguna
> alerta a Telegram: hay poco histórico, la confianza es baja (cold start) y el
> motor **a propósito** no envía nada. No es un fallo.

### Paso 7 — Leer los logs de una ejecución

1. Pestaña **Actions** → pulsa la ejecución que quieras ver.
2. Pulsa el job (**intraday-cycle** o **daily-cycle**).
3. Despliega el paso **«Ejecutar ciclo …»**: ahí está el log completo (qué pasos
   corrieron, errores de fuentes, alertas evaluadas/enviadas).
4. Si la ejecución salió ❌ roja, el log te dirá qué paso cascó; además habrás
   recibido un aviso breve por Telegram.

### Cómo cambiar los horarios

Edita el `cron:` dentro del `.yml` correspondiente (recuerda: **en UTC**),
haz `git add`, `git commit` y `git push`. GitHub recoge el cambio solo.

---

## Bloque 1 — Indicadores de CONTEXTO de régimen (con auto-activación)

Cuatro indicadores macro nuevos que **afinan** la lectura de régimen pero que
**NO son flujos de liquidez**: son termómetros de estado, misma filosofía que
`^VIX`/`CRYPTO_FNG`. Por eso **no entran en los rankings de entrada/salida ni
disparan alertas de flujo** — viven en su propia tabla `context_indicators`, no
en `flow_scores` (exclusión por construcción, no por lista que mantener).

### Indicadores y fuentes (verificadas)

| Indicador | Qué mide | Fuente (gratis) | Estado |
|-----------|----------|-----------------|--------|
| `btc_dominance` | % de market cap de BTC sobre el total crypto. Sube = rotación a BTC (miedo en alts); baja = apetito por riesgo | CoinGecko `/global` → `market_cap_percentage.btc` | ✅ activo |
| `credit_spread` | Ratio **HYG/LQD** (high-yield vs investment-grade). Cae = el high-yield sufre más → spreads ensanchándose → risk-off | yfinance `HYG`, `LQD` | ✅ activo |
| `yield_curve` | Spread **10Y-2Y** en puntos porcentuales (`^TNX` − `2YY=F`). < 0 = curva invertida → señal de recesión / risk-off | yfinance `^TNX` (10Y) y `2YY=F` (2Y, CBOE yield future) | ✅ activo |
| `put/call ratio` | Sentimiento de opciones | — | ❌ **OMITIDO** |

**Por qué se omite put/call:** tras verificar fuentes (jun-2026), CBOE gatea sus
endpoints (`cdn.cboe.com/api/...` → 403; `market_statistics/daily` → redirección
a página gateada): no hay API gratuita y estable de put/call total. Derivarlo de
las cadenas de opciones de yfinance es frágil (solo da la foto actual, sin
histórico, lo que rompe el modelo de auto-activación por `min_obs`, además de
lento y propenso a rate-limit). **No se fuerza una fuente dudosa**; si en el
futuro aparece una fiable, se añade como cuarto indicador sin tocar el resto.

> **Sobre el 2Y:** yfinance no expone un índice 2Y «de caja», pero **sí** un
> futuro de rendimiento del 2Y de CBOE (`2YY=F`), que da el spread 10Y-2Y
> clásico (no un proxy 5Y). El ticker corto es configurable
> (`YIELD_CURVE_SHORT_TICKER`, por defecto `2YY=F`; alternativas `^FVX` 5Y o
> `^IRX` 3M).

### Auto-activación (clave — patrón para bloques futuros)

Cada indicador se **enciende solo** cuando acumula histórico suficiente y
**degrada con elegancia** mientras no:

- Umbral mínimo de observaciones por indicador: `CONTEXT_MIN_OBS` (def. **5**),
  análogo al `score_min_obs` del scoring.
- Por **debajo** del umbral: el indicador **no modula** el régimen y se muestra
  marcado `(preliminar)` en el parte (o se omite si aún no tiene ni un dato).
- Por **encima**: modula el régimen y se presenta como señal sólida.
- Si falta el dato, **se omite limpiamente** (igual que la comparación temporal
  del digest: «sin parte anterior»). El sistema nunca se rompe ni ensucia los
  partes por un indicador que aún no está listo.
- **Se puede mergear ya**: cada indicador se autoenciende al acumular datos, sin
  intervención manual futura.

### Cómo entran en el régimen

Como **moduladores de confianza**, igual que DXY/VIX: refuerzan un régimen que
**ya encajó por flujos** (añaden confianza y aparecen como señales), pero
**NUNCA disparan un régimen por sí solos**. Si no hay candidatos de flujo, el
contexto no hace nada. Señales que añaden:

- `credit_spread_widening` / `yield_curve_inverted` / `yield_curve_flattening` /
  `btc_dominance_rising` → refuerzan **risk_off** y **flight_to_safety**.
- `credit_spread_tightening` / `yield_curve_steepening` /
  `btc_dominance_falling` → refuerzan **risk_on**.

Además enriquecen la **línea de contexto del parte** (bloque «🌡 Contexto
macro»), junto al régimen y sus señales.

### Robustez

Cada fuente nueva va en su propio `try/except` dentro de `ContextIngestRunner`
(una fuente más de `IngestAll`): si falla (API caída, ticker cambiado), se
registra y el ciclo **continúa**. HYG/LQD/^TNX/2YY=F se piden en **un solo lote**
de yfinance (anti rate-limit). Si el contexto entero no está disponible, MAREA
sigue funcionando **exactamente igual** que antes.

### Nueva migración

```text
migrations/012_context_indicators.sql   ← tabla context_indicators (NO toca anteriores)
```

### Variables de entorno (opcionales)

| Variable | Default | Significado |
|----------|---------|-------------|
| `CONTEXT_MIN_OBS` | `5` | Observaciones mínimas para que un indicador se active |
| `YIELD_CURVE_SHORT_TICKER` | `2YY=F` | Ticker del tramo corto de la curva (2Y) |

### Tests

`tests/test_context.py` (30 tests): cálculo de cada indicador, ingesta aislada
(escribe solo en `context_indicators`), auto-activación (bajo/sobre `min_obs`),
degradación elegante ante fallo de fuente o BD, el contexto modula pero **no
crea** régimen, no contaminan los rankings de flujo, y put/call documentado como
omitido.

---

## Bloque 2 — Credibilidad del flujo (con auto-activación)

Los flow scores salían casi siempre de **un** proxy (volumen). El volumen solo
no distingue un flujo **sano** (volumen + precio acompañando + sostenido) de un
**fogonazo** (pico aislado, o volumen sin que el precio confirme). Esta capa
cruza señales para juzgar la **credibilidad** del flujo, **penaliza** el score
cuando no está confirmado y **explica** la etiqueta en los partes.

### Las tres señales

| Señal | Disponible | Qué aporta |
|-------|-----------|------------|
| **Volumen** | día 1 | Base del score actual (`score_raw`). |
| **Confirmación de precio** | **día 1** (sin histórico) | ¿El precio se mueve en la dirección del flujo? Acompaña = creíble; plano = sospechoso (absorción); en contra = dudoso (distribución). Usa el cambio de precio de la misma ventana ya ingerida. |
| **Persistencia** | **auto-activada** (≥ `CREDIBILITY_PERSIST_MIN_OBS` obs) | ¿El flujo se sostiene varias barras o es un pico aislado? Por debajo del umbral **no influye** y no se menciona (degradación elegante). |

### Cómo se combina

```text
credibility   = price_factor × persistence_factor      (∈ [0..1])
score (final) = score_raw × credibility
```

- Volumen+precio coherentes → `price_factor = 1.0`. Precio plano → `0.6`.
  Precio en contra → `0.4`.
- Persistencia: sostenido → `1.0`; pico aislado → hasta `0.6`. Inactiva → `1.0`
  (no penaliza).
- Etiqueta por umbral: `credibility ≥ 0.8` → **confirmado**; `≥ 0.5` →
  **dudoso**; `< 0.5` → **fogonazo**.

Se guardan **ambos**: `score` (ya penalizado, lo que consumen rankings, régimen
y alertas → quedan limpios de fogonazos sin más cambios) y `score_raw` (bruto),
más `credibility`, `credibility_label` y `credibility_reason` para auditoría y
para el digest.

### Credibilidad ≠ confianza (cold start) — son dos cosas distintas

| | Mide | Eje |
|--|------|-----|
| **`confidence`** (`ok`/`low`) | ¿Tengo **suficiente histórico**? | Calidad de los datos |
| **`credibility`** (`[0..1]` + etiqueta) | ¿Es **creíble este flujo concreto**? | Calidad de la señal |

Son independientes: un flujo puede tener histórico de sobra (`confidence=ok`) y
aun así ser un fogonazo (`credibility` baja), y al revés. Por eso viven en
columnas separadas y se calculan por separado.

### A qué aplica

Solo a estrategias de **flujo con volumen+precio**: acciones/ETFs/commodities
(`VolumeFlowStrategy`) y crypto (`CryptoVolumeStrategy`) — donde la confirmación
de precio es especialmente valiosa (el z-score de volumen de crypto no
incorpora la dirección del precio). **No** aplica a termómetros/contexto
(`^VIX`, `CRYPTO_FNG`, DXY, bono 10Y) ni a stablecoins (su cambio de supply es
una medición directa del flujo, no un proxy de volumen con precio que confirmar).

### Ambos carriles

- **Diario**: credibilidad por volumen+precio; la persistencia diaria se activa
  al acumular ~2 semanas de histórico.
- **Intradía**: igual, pero la persistencia es **especialmente valiosa** aquí
  (flujo sostenido durante la sesión vs fogonazo de una sola barra), aprovechando
  las barras intradía.

### Reflejo en los partes

Los rankings ya vienen con el score **penalizado** (limpios de fogonazos). La
etiqueta se muestra **solo cuando aporta avisar** — flujo dudoso o fogonazo:

```text
  ▲ Semiconductores (SOXX) — moderada, +0.55 — ⚠️ posible fogonazo (precio plano…)
  ▼ Bitcoin — moderada, -0.60 → … — ⚠️ sin confirmar (precio en contra del flujo)
```

Lo **confirmado** va discreto (sin marca) para no saturar el parte.

### Nueva migración

```text
migrations/013_credibility.sql   ← columnas score_raw/credibility/label/reason (NO toca anteriores)
```

### Variable de entorno (opcional)

| Variable | Default | Significado |
|----------|---------|-------------|
| `CREDIBILITY_PERSIST_MIN_OBS` | `10` | Observaciones mínimas para activar la señal de persistencia |

### Tests

`tests/test_credibility.py` (18 tests): volumen+precio coherente/plano/contra,
persistencia auto-activada (bajo umbral no influye; pico aislado penaliza,
sostenido no), etiqueta correcta, no aplica a termómetros, score bruto y
penalizado guardados ambos, credibilidad ⟂ confianza, y reflejo en el digest.

---

## Bloque 3 — Inteligencia INTRADÍA de sesión (con auto-activación)

Ya existía una capa de comparación temporal (la "película" de la Sesión 12) que
miraba **un** parte contra el anterior. El Bloque 3 explota los distintos
**momentos del día** (apertura / media sesión / cierre) para tres lecturas que
el dato de "dos fotos" no daba — todas con **auto-activación** (se encienden
solas cuando el día acumula momentos; degradan con elegancia si faltan).

### Las tres funciones

| Función | Dónde aparece | Qué dice |
|---------|---------------|----------|
| **⚖️ Veredicto del día** (cierre-juez) | parte de **cierre** (Tarde USA) | Para los flujos **fuertes** de la apertura/media, dictamina al cierre: **CONFIRMADO** (sigue igual), **REVERTIDO** (se dio la vuelta) o **AGOTADO** (misma dirección, perdió fuelle). |
| **🔄 Giros** | partes intradía (media, tarde) | Activos que **cambian de signo** entre dos momentos (entraba → sale), solo si tuvieron movimiento **fuerte** en al menos uno (no ruido de planos). |
| **⚡ Ritmo** (velocidad) | partes intradía | Si la entrada/salida **se acelera** o **pierde fuelle** entre momentos consecutivos (la "derivada" del flujo). |

```text
⚖️ Veredicto del día:
  • En la apertura entró capital con fuerza en Tecnología (fuerte, +0.85); al cierre se ha CONFIRMADO: sigue entrando (+0.88).
  • En la apertura entró capital con fuerza en Oro (GLD) (moderada, +0.80); al cierre se ha AGOTADO: la entrada perdió fuelle (+0.25).
🔄 Giros:
  • TSLA entraba en la media sesión, ahora sale — el dinero se ha dado la vuelta (+0.78 → -0.62).
⚡ Ritmo:
  • la entrada en Oro (GLD) pierde fuelle (+0.55 → +0.25).
```

### Sin migración nueva (reutiliza digest_cycles)

La capa de comparación temporal (**migración 011**, `digest_cycles`) ya persiste,
por ciclo, los flow scores que el parte **realmente usó**, marcados con su
`moment` y con clave única `(ts, rail, moment)`. Para el carril intradía, un
mismo día (`ts` = medianoche) acumula hasta **tres filas** — apertura / media /
cierre —, que **son** los momentos del día. El Bloque 3 los lee tal cual; la
única ampliación es guardar también `credibility_label` dentro del JSONB de
scores (esquema-libre, retrocompatible: las filas antiguas simplemente no lo
traen). **Por eso no hace falta una migración 014.**

### Auto-activación y degradación elegante

- Mínimo **2 momentos del día** (`MIN_MOMENTS`) para cualquier función. Por
  debajo (primer parte del día, o día con ciclos de cron saltados): el veredicto
  declara *"sin suficientes momentos del día para dictaminar"* y los giros/ritmo
  **no se muestran**. Nunca se inventa una comparación ni se rompe el parte.
- Se mergea ya y se enciende solo a medida que el día acumula momentos. Sin
  intervención manual futura (mismo patrón que los Bloques 1 y 2).

### Respeta todo lo existente

- **Score penalizado** por credibilidad (Bloque 2) como base, nunca el bruto: un
  veredicto/giro/ritmo sobre un **fogonazo** no es fiable → se **omite**.
- **Afirmativo** en lo observado (los giros y veredictos hablan de flujos que ya
  pasaron); nada de salto a precio futuro.
- **No aplica a termómetros** (`^VIX`, `CRYPTO_FNG`) — misma fuente única de verdad.
- Regla madre **"nada a medias"**: el giro nombra el activo y las dos direcciones;
  el veredicto cierra con confirmado/revertido/agotado; el ritmo dice acelera o frena.

### Umbrales (documentados, en `intraday_session.py`)

| Constante | Valor | Significado |
|-----------|-------|-------------|
| `STRONG_MOVE` | `0.6` | `|score|` que cuenta como movimiento **fuerte** (alineado con el umbral de flujo intradía) |
| `RELEVANT_EPS` | `0.2` | Por debajo, el flujo es prácticamente plano (no cuenta como "ahora sale" ni como flujo vivo) |
| `FADE_RATIO` | `0.5` | Cierre `< apertura×esto` (misma dirección) → **AGOTADO**; por encima → **CONFIRMADO** |
| `VELOCITY_EPS` | `0.10` | Cambio mínimo de `|score|` para hablar de acelera/frena; por debajo, **estable** (no se muestra) |

### Dónde vive

- `app/analysis/intraday_session.py` — lógica **pura** (`analyze_session`,
  `classify_verdict`, `classify_velocity` + detección de giros/ritmo/veredicto).
- `app/alerts/digest.py` — renderizado (`render_verdict_block`,
  `render_giros_block`, `render_ritmo_block`) reutilizando nombres legibles e
  intensidad; `_load_today_moments` lee los momentos del día; `send_intraday_digest`
  los engancha. Best-effort: si algo falla, los bloques quedan vacíos (el parte nunca se rompe).

### Tests

`tests/test_intraday_session.py` (47 tests): veredicto confirmado/revertido/
agotado (incl. flujo apagado y desaparecido), giros solo en movimiento fuerte
(ignora planos), velocidad acelera/frena/estable, auto-activación (< 2 momentos
degrada), score penalizado (fogonazo omitido), exclusión de termómetros, y la
integración de los tres bloques en el digest.

---

## Bloque 4 — Detección temprana: desacoples + volumen anómalo (con auto-activación)

Avisa de señales **nacientes** antes de que sean obvias. Es el bloque que **MÁS
depende de histórico**: necesita establecer una *línea base de normalidad* antes
de poder detectar lo anormal. Por eso **despierta más tarde que los demás — y eso
es correcto, no un fallo**. Hasta tener base suficiente declara internamente
*"estableciendo línea base"* y **no muestra nada** (jamás una señal falsa por
falta de datos).

### Las dos funciones

| Función | Qué detecta | Cómo cierra el círculo |
|---------|-------------|------------------------|
| **🔗 Desacoples** | Dos activos que iban **de la mano** y se **separan** (correlación alta y estable que cae) → señal temprana de rotación. | Nombra los **dos lados** y qué hace cada uno: *"entra en oro (+0.80), sale de plata (−0.50) — el dinero rota de uno a otro."* |
| **📊 Volumen anómalo** | Volumen muy por encima de lo **normal para el propio activo** (vs su media/σ histórica). Es una señal de **atención** ("mira aquí"), no un flujo. | Se combina con la **dirección del flujo** (score penalizado): *"4.3σ por encima de lo habitual; la atención es de ENTRADA."* |

```text
🔗 Desacoples:
  • Oro y Plata, que se movían juntos (corr +0.92), se han desacoplado (ahora +0.08): entra en Oro (+0.80), sale de Plata (-0.50) — el dinero rota de uno a otro.
📊 Volumen anómalo:
  • Financieras (bancos) — volumen 4.3σ por encima de lo habitual; la atención es de ENTRADA (+0.62).
```

### Desacoples — qué se vigila

- **Pares clásicos** con sentido económico (lista configurable `CLASSIC_PAIRS`):
  oro/plata (`GC=F`/`SI=F`, `GLD`/`SLV`), S&P/Nasdaq (`^GSPC`/`^IXIC`, `SPY`/`QQQ`),
  semis (`SOXX`/`SMH`), BTC/ETH, oro/mineras (`GLD`/`GDX`), plata/mineras
  (`SLV`/`SIL`), defensa (`ITA`/`XAR`), bancos entre sí (`JPM`/`BAC`/`WFC`/`C`). Los
  que el universo dinámico no haya incorporado aún degradan en silencio.
- **Pares descubiertos:** además, se vigilan automáticamente los pares que la
  propia matriz detecte como **fuerte y establemente correlacionados** (|corr| ≥
  `0.85` en el histórico previo).
- **Condición de desacople:** la correlación **BASE** (histórico estable, ventana
  larga **EXCLUYENDO** lo reciente) era alta (`|base| ≥ 0.7`) **y** la correlación
  **RECIENTE** (ventana corta) cae claramente (`|base − recent| ≥ 0.5`). La base
  excluye a propósito el periodo reciente: si lo incluyera, una ruptura brusca se
  auto-anularía y nunca saltaría.

### Volumen anómalo — vs la propia línea base

Para cada activo se calcula la media y σ de su volumen histórico (`raw_snapshots`)
y se mira cuántas σ por encima está el volumen actual. Anómalo = `≥ 2.5σ`. Es
**atención, no flujo**: la dirección (entrada/salida/solo atención) la pone el
último flow score **penalizado**.

### Auto-activación (CRÍTICA aquí)

| Detección | Umbral de despertar | Tiempo aproximado |
|-----------|---------------------|-------------------|
| Correlación | `EARLY_CORR_MIN_OBS` (15) barras en la ventana **base** (previa a lo reciente) | ~3 semanas de histórico diario |
| Volumen | `EARLY_VOLUME_MIN_OBS` (20) observaciones para media/σ fiables | ~4 semanas |

Si **ningún** par/activo alcanza su umbral → `baseline_ready=False`, el sistema
declara *"estableciendo línea base"* (log interno) y los bloques **no aparecen**.
Por encima, se encienden solas. **Sin intervención manual.**

### Por qué SIN migración nueva (se calcula al vuelo)

Ambas detecciones se computan desde datos **ya almacenados**, sin tabla nueva:

- **Desacoples:** se correlacionan las **series de flow score (penalizado)** por
  ticker, que ya viven en `flow_scores`. Reutiliza la maquinaria de la matriz de
  correlación (Sesión 5) sobre un pivot a nivel de **ticker** (la matriz existente
  agrega por *clase*, lo que colapsa pares como S&P/Nasdaq o BTC/ETH en una sola
  columna; aquí se necesita el detalle por ticker).
- **Volumen:** la distribución histórica de volumen de cada activo ya está en
  `raw_snapshots.volume`; media/σ se calculan al vuelo.

Persistir líneas base en una tabla solo duplicaría datos ya presentes. **La
auto-activación vive en los umbrales de observaciones, no en una tabla.**

### Respeta lo existente

- **Score PENALIZADO** por credibilidad (Bloque 2) como base de la dirección.
- **Excluye termómetros** de sentimiento (`^VIX`, `CRYPTO_FNG`) de ambas
  detecciones — un desacople del VIX o una anomalía de volumen del Fear&Greed no
  tienen sentido. Los indicadores macro del Bloque 1 viven en `context_indicators`
  (no en `flow_scores`/`raw_snapshots`), así que quedan fuera **por construcción**.
- **Regla madre "nada a medias":** cada desacople nombra los dos lados; cada
  anomalía dice de qué activo y en qué dirección apunta el flujo. Afirmativo en lo
  observado; nunca se predice precio.

### Dónde vive

- `app/analysis/early_detection.py` — lógica **pura** (`build_ticker_pivot`,
  `detect_decouples`, `detect_volume_anomalies`) + fachada
  `evaluate_early_detection(db)` (best-effort, nunca lanza).
- `app/alerts/digest.py` — `render_decouple_block` / `render_volume_block`
  (reutilizan nombres legibles); `_early_blocks(db)` los carga; `send_daily_digest`
  los engancha en el parte **diario** (la matriz y el volumen son señales diarias).

### Variables de entorno (opcionales)

| Variable | Default | Significado |
|----------|---------|-------------|
| `EARLY_CORR_MIN_OBS` | `15` | Barras en la ventana base para fiarse de una correlación |
| `EARLY_VOLUME_MIN_OBS` | `20` | Observaciones para una media/σ de volumen fiables |

### Tests

`tests/test_early_detection.py` (24 tests): desacople real detectado cerrando el
círculo (ambos lados), par no correlacionado sin falso positivo, par que sigue
correlado, descubrimiento de pares estables, volumen anómalo vs normal con
dirección por score penalizado, auto-activación (sin base → ninguna señal), score
penalizado (no bruto), exclusión de termómetros, y la integración de ambos
bloques en el digest.

---

## Bloque 5 — Calendario macro: el "por qué" del día

Avisa de los **datos económicos de alto impacto** del día para que se entienda
**POR QUÉ** puede haber movimiento en la liquidez (una decisión de tipos, la
inflación, el empleo…). Es **contexto, no predicción**: dice qué evento hay y a
qué hora, y que *"suele traer volatilidad"* (condicional y genérico); **nunca**
afirma dirección de precio.

```text
📅 Agenda macro de hoy:
  • 14:15 🇪🇺 decisión de tipos del BCE — suele traer volatilidad.
  • 14:30 🇺🇸 PCE de EE.UU. (inflación favorita de la Fed) — suele traer volatilidad.
```

### Paso 0 — fuente verificada en vivo (decisión documentada)

A diferencia de los demás bloques (que reusan datos ya ingeridos), este necesita
una fuente de eventos económicos. Se investigaron **en vivo** tres caminos
gratuitos:

| Fuente | Veredicto |
|--------|-----------|
| **FRED** (St. Louis Fed) | Oficial y muy estable, pero solo da `fecha + nombre`: **sin hora, sin importancia, sin país**, y **sin FOMC ni BCE**. Insuficiente. |
| **FMP** (Financial Modeling Prep) | Esquema completo (país, impacto, FOMC, BCE), pero requiere **API key** y su tier gratuito es de **estabilidad incierta** (ha movido el calendario a pago). |
| **Calendario estático curado** ✅ | Fechas **oficiales** 2026 (Reserva Federal, BCE, OMB/BLS/BEA) en una tabla, consultada al vuelo. **Cero coste, cero key, no se cae nunca** (no hay API en runtime). |

**Decisión (con el usuario): el calendario estático curado.** Es la opción más
**estable** de las tres para un proyecto desatendido de coste cero, cumple todos
los requisitos de eventos, y no añade un secreto que gestionar.

> **Este bloque despierta desde el día 1** (un calendario es información de futuro
> inmediato, no una línea base que haya que acumular).

### Qué eventos (solo alto impacto USA + eurozona)

- **EE.UU.:** FOMC (decisión de tipos de la Fed), IPC/CPI, empleo (NFP), PCE (la
  inflación favorita de la Fed) y PIB (solo el **primer avance** de cada trimestre).
- **Eurozona:** decisión de tipos del **BCE** (Consejo de Gobierno).
- Las revisiones (2.ª/3.ª estimación del PIB) y los datos menores se **omiten** a
  propósito (filtrar el ruido).

### Hora correcta con horario de verano

Cada evento se guarda en su zona **origen** (ET para EE.UU., CET para el BCE) y se
convierte a **hora de Madrid** con `zoneinfo`, que resuelve el horario de verano —
incluidas las **~2 semanas al año** en que los cambios de hora de EE.UU. y la UE
no coinciden (p. ej. el IPC del 11-mar sale a las **13:30** Madrid, no 14:30; el
FOMC del 18-mar y 28-oct a las **19:00**, no 20:00). Por eso **no** se hardcodea el
desfase.

### Dónde se muestra

- **Parte diario** (cierre): la agenda de hoy enmarca el "por qué" del movimiento.
- **Apertura intradía**: "lo que viene hoy" (donde más útil es). En media sesión y
  tarde **no** se repite (el cierre ya la trae como contexto).
- Si hoy no hay eventos de primer orden, el bloque **no aparece**.

### Sin migración, sin API key

La tabla se consulta **al vuelo** en cada ciclo (no hay nada histórico que
persistir) → **sin migración 015**. No requiere ninguna clave ni GitHub Secret.

### Mantenimiento (límite conocido, documentado)

Al ser una tabla curada, hay que **refrescarla una vez al año**: añadir a
`MACRO_EVENTS` (en `app/analysis/macro_calendar.py`) las fechas del año siguiente
de FOMC, BCE y los datos USA (fuentes oficiales citadas en el módulo). Si la tabla
se queda sin fechas futuras, el bloque deja de aparecer y se registra un **aviso
en el log** (no rompe nada). Es el coste de no depender de una API de pago/incierta.

### Degradación elegante (best-effort)

Cualquier problema (tz no disponible, día sin eventos) → lista vacía y el bloque no
aparece. **Nunca rompe el parte** (mismo patrón que el resto de bloques).

### Dónde vive

- `app/analysis/macro_calendar.py` — tabla curada + `events_on(fecha)` (puro) +
  `todays_macro_events()` (fachada best-effort). Fuentes oficiales citadas en el
  docstring.
- `app/alerts/digest.py` — `render_macro_block` + `_macro_lines()`; enganchado en
  `send_daily_digest` (siempre) y `send_intraday_digest` (solo apertura).

### Tests

`tests/test_macro_calendar.py` (26 tests): eventos del día correctos, conversión
horaria con DST incluidas las semanas de desfase EE.UU./UE, solo alto impacto
USA/eurozona (tipos no admitidos ignorados), día sin eventos → no aparece, fallo de
fuente → degradación elegante, y la integración en el digest (diario + apertura).

---

## Sesiones futuras

- (todas las sesiones planificadas completadas)
