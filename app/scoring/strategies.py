"""
Estrategias de flow score por asset_class.

Una estrategia por clase: recibe filas de raw_snapshots y devuelve ScoreResult.

Proxies usados:
  index / etf / commodity / stock → volumen anómalo * signo de precio
  crypto                          → z-score volumen_24h (+ fusión funding/OI si perp)
  onchain (stablecoins)           → cambio en supply (mint=+, burn=-)
  macro / rates (^TNX)            → z-score de 'close' INVERTIDO (yield sube=bono cae)
  macro / currency (DXY)          → z-score de 'close' (informativo, risk-off)
  macro / volatility (VIX)        → z-score de 'close' invertido (VIX alto=risk-off=-1)
  macro / crypto_sentiment (FNG)  → mapeo lineal 0-100 → -1..+1 (no z-score)
"""

import numpy as np
import pandas as pd

from app.scoring.base import ScoreResult, _low_result, _make_result
from app.scoring.zscore import (
    MIN_OBS_DEFAULT,
    ZResult,
    rolling_zscore,
    series_from_snapshots,
    sign_from_price_direction,
)


# ── 1. Volumen anómalo (index / etf / commodity / stock) ──────────────────────

class VolumeFlowStrategy:
    """
    Proxy: z-score del volumen diario en la ventana.
    Signo: matizado por dirección del precio en la misma ventana.
      volumen_alto + precio_sube  → inflow  (+)
      volumen_alto + precio_cae  → outflow (−)
      volumen neutro / bajo       → score ≈ 0

    Lee: raw_snapshots.volume, raw_snapshots.close
    """
    proxy_name = "volume_zscore_signed"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        vol_series = series_from_snapshots(rows, "volume")
        if vol_series.empty:
            return _low_result(self.proxy_name)

        zr = rolling_zscore(vol_series, window, min_obs)
        if zr.score is None:
            return _make_result(zr, self.proxy_name)

        # Matizar con dirección de precio
        close_series = series_from_snapshots(rows, "close")
        direction = sign_from_price_direction(close_series, window) if not close_series.empty else 0.0

        signed = zr.score * direction if direction != 0.0 else zr.score
        signed = float(np.clip(signed, -1.0, 1.0))

        return ScoreResult(
            score=signed,
            raw_zscore=zr.zscore,
            proxy_used=self.proxy_name,
            n_obs=zr.n_obs,
            confidence=zr.confidence,
        )


# ── 2. Crypto spot (BTC, ETH, top-20 dinámico) ────────────────────────────────

class CryptoVolumeStrategy:
    """
    Proxy compuesto para crypto spot (coingecko):
      (a) z-score del campo extra.volume_24h  (principal)
    El signo es siempre positivo porque en crypto spot, volumen_24h alto
    indica interés/presión compradora en el contexto del mercado.
    Si volume_24h no está en extra, usa raw_snapshots.volume como fallback.

    Lee: extra.volume_24h (o volume como fallback)
    """
    proxy_name = "crypto_volume_24h_zscore"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        # Intenta volume_24h en extra primero
        vol_series = series_from_snapshots(rows, "volume_24h")
        if vol_series.empty:
            vol_series = series_from_snapshots(rows, "volume")
        if vol_series.empty:
            return _low_result(self.proxy_name)

        zr = rolling_zscore(vol_series, window, min_obs)
        return _make_result(zr, self.proxy_name)


# ── 3. Stablecoins on-chain (STABLES_USDT, STABLES_USDC) ─────────────────────

class StablecoinSupplyStrategy:
    """
    Proxy: cambio diario en supply circulante (close = supply total en USD).
    Mint (supply sube)  → inflow de pólvora al mercado → +
    Burn (supply cae)   → liquidez saliendo              → −

    Calcula z-score de los cambios diarios (diff), no del nivel absoluto.
    Lee: raw_snapshots.close (supply total en USD del día)
         extra.change_usd como alternativa directa si disponible
    """
    proxy_name = "stablecoin_supply_change_zscore"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        supply_series = series_from_snapshots(rows, "close")
        if len(supply_series) < 2:
            return _low_result(self.proxy_name)

        # z-score sobre los cambios diarios
        changes = supply_series.diff().dropna()
        zr = rolling_zscore(changes, window, min_obs)
        return _make_result(zr, self.proxy_name)


# ── 4. Bono 10Y (^TNX) — signo INVERTIDO ─────────────────────────────────────

class BondYieldStrategy:
    """
    Proxy: z-score del rendimiento del bono 10Y, con signo INVERTIDO.
    Rendimiento sube → precio del bono BAJA → outflow de bonos → score −
    Rendimiento baja → precio del bono SUBE → inflow a bonos  → score +

    Lee: raw_snapshots.close (= yield en %)
    """
    proxy_name = "bond_yield_zscore_inverted"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        yield_series = series_from_snapshots(rows, "close")
        if yield_series.empty:
            return _low_result(self.proxy_name)

        zr = rolling_zscore(yield_series, window, min_obs)
        if zr.score is None:
            return _make_result(zr, self.proxy_name)

        return ScoreResult(
            score=float(np.clip(-zr.score, -1.0, 1.0)),   # INVERTIDO
            raw_zscore=zr.zscore,
            proxy_used=self.proxy_name,
            n_obs=zr.n_obs,
            confidence=zr.confidence,
        )


# ── 5. DXY — informativo ──────────────────────────────────────────────────────

class DollarIndexStrategy:
    """
    Proxy: z-score del DXY (índice del dólar). Score informativo.
    Dólar fuerte → risk-off → dinero "hacia el dólar" (score +).
    No invierte; el signo positivo = flujo hacia el dólar.

    Lee: raw_snapshots.close
    """
    proxy_name = "dxy_zscore_informative"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        series = series_from_snapshots(rows, "close")
        if series.empty:
            return _low_result(self.proxy_name)
        zr = rolling_zscore(series, window, min_obs)
        return _make_result(zr, self.proxy_name)


# ── 6. VIX — informativo, invertido ──────────────────────────────────────────

class VIXStrategy:
    """
    Proxy: z-score del VIX, con signo INVERTIDO respecto a flujo de riesgo.
    VIX alto → miedo → risk-off → score − (fuga del riesgo).
    VIX bajo  → codicia → score + (apetito por riesgo).
    Marcado como informativo (no es flujo de liquidez directo).

    Lee: raw_snapshots.close
    """
    proxy_name = "vix_zscore_inverted_informative"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        series = series_from_snapshots(rows, "close")
        if series.empty:
            return _low_result(self.proxy_name)

        zr = rolling_zscore(series, window, min_obs)
        if zr.score is None:
            return _make_result(zr, self.proxy_name)

        return ScoreResult(
            score=float(np.clip(-zr.score, -1.0, 1.0)),   # INVERTIDO
            raw_zscore=zr.zscore,
            proxy_used=self.proxy_name,
            n_obs=zr.n_obs,
            confidence=zr.confidence,
        )


# ── 7. Fear & Greed crypto (CRYPTO_FNG) ──────────────────────────────────────

class FearGreedStrategy:
    """
    Proxy: mapeo lineal del índice 0-100 → [-1, +1].
    No usa z-score: el índice ya es un score normalizado por diseño.
    0   = miedo extremo → -1
    50  = neutral       →  0
    100 = codicia extr. → +1

    Lee: raw_snapshots.close (valor 0-100)
    """
    proxy_name = "fear_greed_linear_map"

    def compute(
        self,
        rows: list[dict],
        window: int,
        min_obs: int = MIN_OBS_DEFAULT,
    ) -> ScoreResult:
        series = series_from_snapshots(rows, "close")
        if series.empty:
            return _low_result(self.proxy_name)

        last_val = float(series.iloc[-1])
        # mapeo: (val - 50) / 50 → [-1, +1]
        score = float(np.clip((last_val - 50.0) / 50.0, -1.0, 1.0))
        n_obs = int(series.count())
        confidence = "ok" if n_obs >= min_obs else "low"

        return ScoreResult(
            score=score,
            raw_zscore=last_val,   # valor crudo para debug
            proxy_used=self.proxy_name,
            n_obs=n_obs,
            confidence=confidence,
        )


# ── Registro: asset_class + sector → estrategia ──────────────────────────────

_VOLUME_STRATEGY = VolumeFlowStrategy()
_CRYPTO_STRATEGY = CryptoVolumeStrategy()
_STABLECOIN_STRATEGY = StablecoinSupplyStrategy()
_BOND_STRATEGY = BondYieldStrategy()
_DXY_STRATEGY = DollarIndexStrategy()
_VIX_STRATEGY = VIXStrategy()
_FNG_STRATEGY = FearGreedStrategy()


def get_strategy(asset_class: str, sector: str | None) -> "Strategy":
    """
    Devuelve la estrategia correcta según asset_class y sector.

    Dispatch logic:
      index, etf, commodity, stock → VolumeFlowStrategy
      crypto                        → CryptoVolumeStrategy
      onchain / stablecoin          → StablecoinSupplyStrategy
      macro / rates                 → BondYieldStrategy (signo invertido)
      macro / volatility            → VIXStrategy (signo invertido)
      macro / currency              → DollarIndexStrategy
      macro / crypto_sentiment      → FearGreedStrategy
      macro (otras)                 → VolumeFlowStrategy (fallback genérico)
    """
    cls = asset_class or ""
    sec = sector or ""

    if cls in ("index", "etf", "commodity", "stock"):
        return _VOLUME_STRATEGY

    if cls == "crypto":
        return _CRYPTO_STRATEGY

    if cls == "onchain":
        return _STABLECOIN_STRATEGY

    if cls == "macro":
        if sec == "rates":
            return _BOND_STRATEGY
        if sec == "volatility":
            return _VIX_STRATEGY
        if sec == "currency":
            return _DXY_STRATEGY
        if sec == "crypto_sentiment":
            return _FNG_STRATEGY
        return _VOLUME_STRATEGY   # fallback macro genérico

    return _VOLUME_STRATEGY   # fallback global


# Tipo exportado para type hints
Strategy = VolumeFlowStrategy  # duck-typing: Protocol no instanciable directamente
