# data_collector/regime/regime_classifier.py — 시장 국면 분류
# 기술적 지표 기반으로 시장 국면 (Bull, Bear, Sideways) 분류

import os
import time
import logging
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

try:
    import pandas as pd
except ImportError:
    pd = None

logger = logging.getLogger("regime_classifier")

# ── 환경변수 ───────────────────────────────────────────────────
# 별도 API 키 필요 없음

# ── 국면 분류 기준 ─────────────────────────────────────────────
REGIME_THRESHOLDS = {
    "trend_strength": 0.02,    # 추세 강도 임계값 (2%)
    "volatility_threshold": 0.03,  # 변동성 임계값 (3%)
    "adx_min": 25,             # ADX 최소값
    "rsi_overbought": 70,
    "rsi_oversold": 30,
}

# 캐시
_cache = {}
CACHE_TTL = 3600  # 1시간


def calculate_sma(prices: list, period: int) -> list:
    """단순 이동평균 계산"""
    if len(prices) < period:
        return []
    return [sum(prices[i:i+period]) / period for i in range(len(prices) - period + 1)]


def calculate_rsi(prices: list, period: int = 14) -> float:
    """RSI 계산"""
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """ADX 계산 (추세 강도)"""
    if len(highs) < period + 1:
        return 0.0

    tr_values = []
    dm_plus_values = []
    dm_minus_values = []

    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        dm_plus = highs[i] - highs[i-1] if highs[i] - highs[i-1] > lows[i-1] - lows[i] else 0
        dm_minus = lows[i-1] - lows[i] if lows[i-1] - lows[i] > highs[i] - highs[i-1] else 0

        tr_values.append(tr)
        dm_plus_values.append(dm_plus)
        dm_minus_values.append(dm_minus)

    # ATR 계산
    atr = sum(tr_values[-period:]) / period
    di_plus = (sum(dm_plus_values[-period:]) / period) / atr * 100 if atr > 0 else 0
    di_minus = (sum(dm_minus_values[-period:]) / period) / atr * 100 if atr > 0 else 0

    dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
    adx = sum([dx] * period) / period  # 단순화

    return adx


def classify_market_regime(price_data: list) -> dict:
    """시장 국면 분류"""
    if not price_data or len(price_data) < 20:
        return {"regime": "UNKNOWN", "confidence": 0.0, "indicators": {}}

    cache_key = f"regime_{hash(str(price_data))}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        closes = [float(d["close"]) for d in price_data]
        highs = [float(d["high"]) for d in price_data]
        lows = [float(d["low"]) for d in price_data]

        # 추세 계산 (20일 SMA)
        sma20 = calculate_sma(closes, 20)
        if not sma20:
            return {"regime": "UNKNOWN", "confidence": 0.0, "indicators": {}}

        current_price = closes[-1]
        sma_current = sma20[-1]
        trend_strength = (current_price - sma_current) / sma_current

        # RSI
        rsi = calculate_rsi(closes, 14)

        # ADX
        adx = calculate_adx(highs, lows, closes, 14)

        # 변동성 (표준편차)
        volatility = np.std(closes[-20:]) / np.mean(closes[-20:]) if len(closes) >= 20 else 0

        # 국면 판정
        indicators = {
            "trend_strength": trend_strength,
            "rsi": rsi,
            "adx": adx,
            "volatility": volatility,
        }

        # Bull Market: 강한 상승 추세 + ADX 높음 + RSI 과매수 아님
        if (trend_strength > REGIME_THRESHOLDS["trend_strength"] and
            adx > REGIME_THRESHOLDS["adx_min"] and
            rsi < REGIME_THRESHOLDS["rsi_overbought"]):
            regime = "BULL"
            confidence = min(1.0, (trend_strength * 10 + adx / 25) / 2)

        # Bear Market: 강한 하락 추세 + ADX 높음
        elif (trend_strength < -REGIME_THRESHOLDS["trend_strength"] and
              adx > REGIME_THRESHOLDS["adx_min"]):
            regime = "BEAR"
            confidence = min(1.0, (-trend_strength * 10 + adx / 25) / 2)

        # Sideways: 낮은 추세 강도 + 높은 변동성 또는 ADX 낮음
        elif (abs(trend_strength) < REGIME_THRESHOLDS["trend_strength"] / 2 or
              adx < REGIME_THRESHOLDS["adx_min"] / 2):
            regime = "SIDEWAYS"
            confidence = 0.5 + (1 - abs(trend_strength) * 10) * 0.3

        else:
            regime = "NEUTRAL"
            confidence = 0.5

        result = {
            "regime": regime,
            "confidence": round(confidence, 2),
            "indicators": {k: round(v, 4) for k, v in indicators.items()},
            "timestamp": datetime.now().isoformat(),
        }

        _cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    except Exception as e:
        logger.error(f"국면 분류 오류: {e}")
        return {"regime": "ERROR", "confidence": 0.0, "indicators": {}, "error": str(e)}


def classify_multiple_regimes(price_dict: dict) -> dict:
    """여러 자산의 국면을 분류"""
    results = {}
    for symbol, data in price_dict.items():
        results[symbol] = classify_market_regime(data)
    return results