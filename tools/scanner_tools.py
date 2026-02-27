# scanner_tools.py — 종목 스캐닝 도구 (돈치안 채널, RSI, 거래량 분석)
# market_scanner.py에서 사용

import logging
from typing import Optional

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import yfinance as yf
except ImportError:
    yf = None

from config.settings import (
    DONCHIAN_PERIOD, VOLUME_SURGE_RATIO,
    RSI_LOWER, RSI_UPPER,
    ADX_PERIOD, ADX_THRESHOLD, VWAP_LOOKBACK,
    ATR_PERIOD, DONCHIAN_PROXIMITY_PCT,
)
from tools.utils import safe_float

logger = logging.getLogger("scanner_tools")

# ── 코스피 시총 상위 종목 (후보군) ──────────────────────────
# 실제로는 KIS API로 동적 조회하나, 초기에는 고정 리스트 사용
KOSPI_TOP_CODES = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "373220",  # LG에너지솔루션
    "207940",  # 삼성바이오로직스
    "005380",  # 현대차
    "000270",  # 기아
    "068270",  # 셀트리온
    "051910",  # LG화학
    "006400",  # 삼성SDI
    "035420",  # NAVER
    "035720",  # 카카오
    "055550",  # 신한지주
    "105560",  # KB금융
    "003550",  # LG
    "066570",  # LG전자
    "012330",  # 현대모비스
    "028260",  # 삼성물산
    "034730",  # SK
    "009150",  # 삼성전기
    "096770",  # SK이노베이션
    "032830",  # 삼성생명
    "003670",  # 포스코홀딩스
    "010130",  # 고려아연
    "030200",  # KT
    "017670",  # SK텔레콤
    "086790",  # 하나금융지주
    "018260",  # 삼성에스디에스
    "316140",  # 우리금융지주
    "011200",  # HMM
    "259960",  # 크래프톤
]


def _to_yf_ticker(code: str) -> str:
    """한국 종목코드 → yfinance 티커"""
    return f"{code}.KS"


def fetch_ohlcv(code: str, period: str = "3mo") -> Optional["pd.DataFrame"]:
    """yfinance에서 OHLCV 데이터 조회"""
    if yf is None:
        return None
    try:
        ticker = _to_yf_ticker(code)
        df = yf.download(ticker, period=period, progress=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        logger.error(f"{code} OHLCV 조회 실패: {e}", exc_info=True)
        return None


# ── 기술적 지표 계산 ──────────────────────────────────────

def calc_rsi(close: "pd.Series", period: int = 14) -> float:
    """RSI 계산 (최근값 반환)"""
    if close is None or len(close) < period + 1:
        return 50.0  # 기본값
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    val = safe_float(rsi.iloc[-1])
    return val if not pd.isna(val) else 50.0


def calc_donchian(df: "pd.DataFrame", period: int = 20) -> dict:
    """돈치안 채널 계산"""
    if df is None or len(df) < period:
        return {"upper": 0, "lower": 0, "breakout": False}

    high = df["High"].iloc[-period:]
    low = df["Low"].iloc[-period:]
    upper = safe_float(high.max())
    lower = safe_float(low.min())
    cur = safe_float(df["Close"].iloc[-1])

    return {
        "upper": upper,
        "lower": lower,
        "current": cur,
        "breakout": cur >= upper,  # 상단 돌파
        "breakdown": cur <= lower,  # 하단 이탈
    }


def calc_volume_ratio(df: "pd.DataFrame", base_days: int = 20) -> float:
    """최근 1일 거래량 / 20일 평균 거래량"""
    if df is None or len(df) < base_days + 1:
        return 1.0
    avg_vol = safe_float(df["Volume"].iloc[-(base_days + 1):-1].mean())
    if avg_vol == 0:
        return 1.0
    today_vol = safe_float(df["Volume"].iloc[-1])
    return round(today_vol / (avg_vol or 1), 2)


# ── ATR (Average True Range) ────────────────────────────────

def calc_atr(df: "pd.DataFrame", period: int = None) -> float:
    """
    ATR 계산 — 변동성 기반 동적 손절에 사용
    True Range = max(H-L, |H-prevC|, |L-prevC|)
    ATR = TR의 period일 이동평균
    """
    if period is None:
        period = ATR_PERIOD
    if df is None or len(df) < period + 1:
        return 0.0

    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    val = safe_float(atr.iloc[-1])
    return round(val, 2) if not pd.isna(val) else 0.0


# ── VWAP (Volume-Weighted Average Price) ────────────────────

def calc_vwap(df: "pd.DataFrame", period: int = None) -> dict:
    """
    일봉 기준 VWAP 계산
    TP = (High + Low + Close) / 3
    VWAP = Σ(TP × Volume) / Σ(Volume) over period
    """
    if period is None:
        period = VWAP_LOOKBACK
    if df is None or len(df) < period:
        return {"vwap": 0, "price_above_vwap": False, "deviation_pct": 0}

    recent = df.iloc[-period:]
    tp = (recent["High"] + recent["Low"] + recent["Close"]) / 3
    vol = recent["Volume"]

    vol_sum = float(vol.sum())
    if vol_sum == 0:
        return {"vwap": 0, "price_above_vwap": False, "deviation_pct": 0}

    vwap = float((tp * vol).sum() / (vol_sum or 1))
    cur = safe_float(df["Close"].iloc[-1])
    deviation = (cur / (vwap or 1) - 1) * 100 if vwap > 0 else 0

    return {
        "vwap": round(vwap, 2),
        "current_price": round(cur, 2),
        "price_above_vwap": cur > vwap,
        "deviation_pct": round(deviation, 2),
    }


# ── ADX (Average Directional Index) ────────────────────────

def calc_adx(df: "pd.DataFrame", period: int = None) -> dict:
    """
    ADX 계산 — 추세 강도 측정
    ADX ≥ 25: 추세 존재 (매매 적합)
    ADX < 25: 횡보 (진입 회피)
    """
    if period is None:
        period = ADX_PERIOD
    if df is None or len(df) < period * 2 + 1:
        return {"adx": 0, "plus_di": 0, "minus_di": 0, "trending": False}

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(high)

    # +DM, -DM 계산
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)

    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # Wilder Smoothing (지수이동평균)
    def wilder_smooth(arr, p):
        result = np.zeros(len(arr))
        result[p] = arr[1:p + 1].sum()
        for i in range(p + 1, len(arr)):
            result[i] = result[i - 1] - result[i - 1] / (p or 1) + arr[i]
        return result

    smooth_tr = wilder_smooth(tr, period)
    smooth_plus = wilder_smooth(plus_dm, period)
    smooth_minus = wilder_smooth(minus_dm, period)

    # +DI, -DI (0 나누기 경고 억제 — np.where로 안전 처리됨)
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = np.where(smooth_tr > 0, 100 * smooth_plus / (smooth_tr or 1), 0)
        minus_di = np.where(smooth_tr > 0, 100 * smooth_minus / (smooth_tr or 1), 0)

        # DX → ADX
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100 * np.abs(plus_di - minus_di) / (di_sum or 1), 0)

    # ADX = DX의 wilder smoothing
    adx = np.zeros(n)
    start_idx = period * 2
    if start_idx < n:
        adx[start_idx] = dx[period + 1:start_idx + 1].mean()
        for i in range(start_idx + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / (period or 1)

    adx_val = float(adx[-1])
    pdi_val = float(plus_di[-1])
    mdi_val = float(minus_di[-1])

    return {
        "adx": round(adx_val, 2),
        "plus_di": round(pdi_val, 2),
        "minus_di": round(mdi_val, 2),
        "trending": adx_val >= ADX_THRESHOLD,
        "trend_direction": "UP" if pdi_val > mdi_val else "DOWN",
    }


# ── 기술적 필터 ────────────────────────────────────────────

def apply_tech_filter(code: str, df: "pd.DataFrame" = None) -> dict:
    """
    종목에 기술적 필터 적용:
    ★ ADX ≥ 25 필수 게이트 (횡보장 진입 방지)
    1) RSI 50~70 범위 (모멘텀 구간)
    2) 돈치안 상단 근접 or 돌파
    3) 거래량 배율 >= VOLUME_SURGE_RATIO
    + VWAP 위 확인 (보너스)

    Returns: {"code": str, "passed": bool, "rsi": float, ...}
    """
    if df is None:
        df = fetch_ohlcv(code)
    if df is None or len(df) < DONCHIAN_PERIOD:
        return {"code": code, "passed": False, "reason": "데이터 부족"}

    # ★ ADX 게이트 (필수) — 추세 존재 + 상승 방향
    adx_result = calc_adx(df)
    adx_ok = adx_result["trending"] and adx_result["trend_direction"] == "UP"

    # RSI
    rsi = calc_rsi(df["Close"])
    rsi_ok = RSI_LOWER <= rsi <= RSI_UPPER

    # 돈치안
    donchian = calc_donchian(df, DONCHIAN_PERIOD)
    donchian_ok = donchian["breakout"] or (
        donchian["current"] >= donchian["upper"] * DONCHIAN_PROXIMITY_PCT
    )

    # 거래량
    vol_ratio = calc_volume_ratio(df)
    vol_ok = vol_ratio >= VOLUME_SURGE_RATIO

    # VWAP
    vwap_result = calc_vwap(df)
    vwap_above = vwap_result["price_above_vwap"]

    # ATR (포지션 관리용으로 함께 계산)
    atr_value = calc_atr(df)

    # 최소 2개 조건 충족 + ADX 게이트 필수
    conditions = [rsi_ok, donchian_ok, vol_ok]
    base_passed = sum(conditions) >= 2
    passed = adx_ok and base_passed

    return {
        "code": code,
        "passed": passed,
        "adx": adx_result["adx"],
        "adx_ok": adx_ok,
        "adx_direction": adx_result["trend_direction"],
        "rsi": round(rsi, 1),
        "rsi_ok": rsi_ok,
        "donchian_breakout": donchian["breakout"],
        "donchian_near_high": donchian_ok,
        "vol_ratio": vol_ratio,
        "vol_ok": vol_ok,
        "vwap": vwap_result["vwap"],
        "vwap_above": vwap_above,
        "vwap_deviation_pct": vwap_result["deviation_pct"],
        "atr": atr_value,
        "conditions_met": sum(conditions),
        "reject_reason": (
            "ADX < 25 (횡보장)" if not adx_result["trending"]
            else "ADX 하락추세 (-DI > +DI)" if adx_result["trend_direction"] == "DOWN"
            else "조건 미달" if not base_passed
            else None
        ),
    }


def scan_candidates(codes: list = None) -> list:
    """
    후보 종목 전체 스캔 → 기술적 필터 통과 종목 반환
    codes가 None이면 KOSPI_TOP_CODES 사용
    """
    if codes is None:
        codes = KOSPI_TOP_CODES

    results = []
    passed = []

    for code in codes:
        try:
            result = apply_tech_filter(code)
            results.append(result)
            if result["passed"]:
                passed.append(result)
        except Exception as e:
            logger.error(f"{code} 스캔 실패: {e}", exc_info=True)

    logger.info(f"스캔 완료: {len(codes)}종목 중 {len(passed)}종목 통과")
    return passed


# ── 테스트 ────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Scanner Tools 테스트 ===")
    # 삼성전자만 테스트
    result = apply_tech_filter("005930")
    print(f"삼성전자: {result}")
