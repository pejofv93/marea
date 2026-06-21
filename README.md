# MAREA — Monitor de flujos de liquidez intermercado

Sesiones completadas: **1** (scaffold + yfinance) · **2** (crypto + on-chain) · **3** (universo dinámico) · **4** (motor flow scores) · **5** (análisis intermercado) · **6** (mapa de exposición indirecta) · **7** (capa narrativa LLM) · **8** (motor de alertas + bot Telegram) · **9** (dashboard Streamlit) · **9b** (carril intradía)

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

## Sesiones futuras

- (todas las sesiones planificadas completadas)
