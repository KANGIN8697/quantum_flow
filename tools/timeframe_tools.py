# tools/timeframe_tools.py — 다중 타임프레임 분석 모듈
# 1분봉 웹소켓 데이터를 실시간으로 15분봉/5분봉으로 리샘플링
# Agent 4에서 주기적으로 호출하여 shared_state에 추세 정보 갱신
#
# 백테스트 근거:
#   15분봉 MA3>MA8>MA20 정배열 시 승률 30% → 43% (+13%p)
#   10:00~10:30 시간대 + 정배열 → EV +0.089% (양의 기대값)

import logging
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

logger = logging.getLogger("timeframe_tools")

KST = timezone(timedelta(hours=9))
_lock = threading.Lock()

# ── 종목별 1분봉 버퍼 (메모리 내 축적) ──────────────────────────
# {code: [{"dt": datetime, "o": float, "h": float, "l": float, "c": float, "v": int}, ...]}
_min1_buffer = defaultdict(list)

# 15분봉 리샘플링 결과 캐시
# {code: [{"dt": datetime, "o","h","l","c","v", "ma3","ma8","ma20"}, ...]}
_tf15_cache = {}

# 5분봉 리샘플링 결과 캐시
_tf5_cache = {}

# 최대 버퍼 크기 (1분봉 기준 약 7시간분)
MAX_BUFFER_SIZE = 420


def push_min1_bar(code: str, dt: datetime, o: float, h: float,
                  l: float, c: float, v: int):
    """
    1분봉 데이터를 버퍼에 추가한다.
    웹소켓 피더 또는 Agent 4 체결 루프에서 호출.

    Parameters:
        code: 종목코드 (예: "005930")
        dt:   봉 시작 시각 (KST datetime)
        o,h,l,c: 시가,고가,저가,종가
        v:    거래량
    """
    with _lock:
        buf = _min1_buffer[code]
        buf.append({"dt": dt, "o": o, "h": h, "l": l, "c": c, "v": v})
        # 버퍼 크기 제한
        if len(buf) > MAX_BUFFER_SIZE:
            _min1_buffer[code] = buf[-MAX_BUFFER_SIZE:]


def _resample(code: str, interval_min: int) -> list:
    """
    1분봉 버퍼를 N분봉으로 리샘플링.
    내부 함수, _lock 획득 후 호출할 것.

    Returns:
        [{"dt": datetime, "o","h","l","c","v"}, ...] 시간순
    """
    buf = _min1_buffer.get(code, [])
    if not buf:
        return []

    candles = []
    current_slot = None
    cur = None

    for bar in buf:
        dt = bar["dt"]
        # 슬롯 계산: 15분봉이면 09:00, 09:15, 09:30, ...
        minute_of_day = dt.hour * 60 + dt.minute
        slot = (minute_of_day // interval_min) * interval_min

        if slot != current_slot:
            if cur is not None:
                candles.append(cur)
            current_slot = slot
            cur = {
                "dt": dt.replace(minute=slot % 60, hour=slot // 60, second=0),
                "o": bar["o"], "h": bar["h"], "l": bar["l"],
                "c": bar["c"], "v": bar["v"],
            }
        else:
            if cur is not None:
                cur["h"] = max(cur["h"], bar["h"])
                cur["l"] = min(cur["l"], bar["l"])
                cur["c"] = bar["c"]
                cur["v"] += bar["v"]

    if cur is not None:
        candles.append(cur)

    return candles


def _calc_ma(values: list, period: int) -> float:
    """단순 이동평균. 부족하면 0.0 반환."""
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def update_tf15(code: str) -> dict:
    """
    종목의 15분봉을 리샘플링하고 MA(3,8,20)를 계산한다.
    Agent 4가 주기적으로(매 1분) 호출.

    Returns:
        {
            "trend": "UP" | "DOWN" | "NEUTRAL",
            "aligned": bool,  # MA3>MA8>MA20 정배열 여부
            "ma3": float, "ma8": float, "ma20": float,
            "last_close": float,
            "candle_count": int,
        }
    """
    with _lock:
        candles = _resample(code, 15)
        _tf15_cache[code] = candles

    if len(candles) < 3:
        return {
            "trend": "NEUTRAL", "aligned": False,
            "ma3": 0, "ma8": 0, "ma20": 0,
            "last_close": 0, "candle_count": len(candles),
        }

    closes = [c["c"] for c in candles]
    ma3 = _calc_ma(closes, 3)
    ma8 = _calc_ma(closes, 8)
    ma20 = _calc_ma(closes, 20)

    # 정배열 판정: MA3 > MA8 > MA20 (모두 유효한 값일 때)
    aligned = (ma3 > 0 and ma8 > 0 and ma20 > 0
               and ma3 > ma8 and ma8 > ma20)

    # 역배열: MA3 < MA8 < MA20
    reverse = (ma3 > 0 and ma8 > 0 and ma20 > 0
               and ma3 < ma8 and ma8 < ma20)

    if aligned:
        trend = "UP"
    elif reverse:
        trend = "DOWN"
    else:
        trend = "NEUTRAL"

    return {
        "trend": trend,
        "aligned": aligned,
        "ma3": round(ma3, 0),
        "ma8": round(ma8, 0),
        "ma20": round(ma20, 0),
        "last_close": closes[-1] if closes else 0,
        "candle_count": len(candles),
    }


def update_tf5(code: str) -> dict:
    """
    종목의 5분봉을 리샘플링하고 MA(3,8)를 계산한다.
    파동 확인용 (중추세).

    Returns:
        {
            "trend": "UP" | "DOWN" | "NEUTRAL",
            "aligned": bool,  # MA3>MA8 여부
            "bullish_3": bool, # 최근 3봉 연속 양봉 여부
            "ma3": float, "ma8": float,
            "last_close": float,
        }
    """
    with _lock:
        candles = _resample(code, 5)
        _tf5_cache[code] = candles

    if len(candles) < 3:
        return {
            "trend": "NEUTRAL", "aligned": False, "bullish_3": False,
            "ma3": 0, "ma8": 0, "last_close": 0,
        }

    closes = [c["c"] for c in candles]
    ma3 = _calc_ma(closes, 3)
    ma8 = _calc_ma(closes, 8)

    aligned = ma3 > 0 and ma8 > 0 and ma3 > ma8

    # 최근 3봉 연속 양봉
    recent_3 = candles[-3:]
    bullish_3 = all(c["c"] > c["o"] for c in recent_3)

    if aligned:
        trend = "UP"
    elif ma3 > 0 and ma8 > 0 and ma3 < ma8:
        trend = "DOWN"
    else:
        trend = "NEUTRAL"

    return {
        "trend": trend,
        "aligned": aligned,
        "bullish_3": bullish_3,
        "ma3": round(ma3, 0),
        "ma8": round(ma8, 0),
        "last_close": closes[-1] if closes else 0,
    }


def check_entry_condition(code: str) -> dict:
    """
    다중 타임프레임 진입 조건 종합 체크.
    Agent 3(head_strategist)에서 매매 결정 시 호출.

    백테스트 근거:
      15분봉 정배열 → 승률 30%→43% (+13%p)
      5분봉 양봉 3연속 추가 확인 시 추세 지속 확률↑

    Returns:
        {
            "allow_entry": bool,
            "tf15_trend": str,
            "tf5_trend": str,
            "tf15_aligned": bool,
            "tf5_aligned": bool,
            "reason": str,
        }
    """
    tf15 = update_tf15(code)
    tf5 = update_tf5(code)

    # 진입 허용 조건: 15분봉 정배열 (핵심 필터)
    allow = tf15["aligned"]

    # 사유 문자열
    if allow:
        reason = f"15분봉 정배열(MA3={tf15['ma3']:,.0f}>MA8={tf15['ma8']:,.0f}>MA20={tf15['ma20']:,.0f})"
        if tf5["bullish_3"]:
            reason += " + 5분봉 양봉3연속"
    elif tf15["trend"] == "DOWN":
        reason = f"15분봉 역배열(DOWN) → 진입 차단"
    else:
        reason = f"15분봉 비정배열({tf15['trend']}) → 진입 차단"

    return {
        "allow_entry": allow,
        "tf15_trend": tf15["trend"],
        "tf5_trend": tf5["trend"],
        "tf15_aligned": tf15["aligned"],
        "tf5_aligned": tf5["aligned"],
        "tf5_bullish_3": tf5.get("bullish_3", False),
        "reason": reason,
    }


def check_overnight_trend(code: str) -> dict:
    """
    Track 2 오버나이트 전환 시 추세 유지 확인.
    14:30에 Agent 3에서 호출.

    Returns:
        {
            "trend_ok": bool,  # 15분봉 여전히 정배열인가
            "tf15_trend": str,
            "reason": str,
        }
    """
    tf15 = update_tf15(code)

    trend_ok = tf15["aligned"]
    if trend_ok:
        reason = "15분봉 정배열 유지 → 오버나이트 추세 조건 충족"
    else:
        reason = f"15분봉 {tf15['trend']} → 오버나이트 추세 조건 미충족"

    return {
        "trend_ok": trend_ok,
        "tf15_trend": tf15["trend"],
        "reason": reason,
    }


def get_tf15_candles(code: str) -> list:
    """15분봉 캐시 반환 (디버그/리포트용)"""
    with _lock:
        return list(_tf15_cache.get(code, []))


def get_tf5_candles(code: str) -> list:
    """5분봉 캐시 반환 (디버그/리포트용)"""
    with _lock:
        return list(_tf5_cache.get(code, []))


def clear_buffers():
    """장 시작 전 버퍼 초기화 (일일 리셋)"""
    with _lock:
        _min1_buffer.clear()
        _tf15_cache.clear()
        _tf5_cache.clear()
    logger.info("타임프레임 버퍼 초기화 완료")


# ── KIS API 기반 15분봉 조회 (웹소켓 미연결 시 폴백) ─────────────
def fetch_tf15_from_api(code: str) -> dict:
    """
    KIS API로 15분봉 데이터를 직접 조회하여 추세 판단.
    웹소켓 1분봉 버퍼가 비어있을 때 폴백으로 사용.
    기존 intraday_tools.py의 analyze_15m_trend()를 개선.
    """
    try:
        from tools.intraday_tools import fetch_intraday_candles
        candles = fetch_intraday_candles(code, interval_minutes=15, count=20)
        if not candles or len(candles) < 3:
            return {"trend": "NEUTRAL", "aligned": False, "reason": "API 데이터 부족"}

        # 역순(최신→과거) → 시간순으로 변환
        closes = [c["close"] for c in reversed(candles)]
        ma3 = _calc_ma(closes, 3)
        ma8 = _calc_ma(closes, 8)
        ma20 = _calc_ma(closes, 20)

        aligned = (ma3 > 0 and ma8 > 0 and ma20 > 0
                   and ma3 > ma8 and ma8 > ma20)

        return {
            "trend": "UP" if aligned else ("DOWN" if ma3 < ma8 < ma20 else "NEUTRAL"),
            "aligned": aligned,
            "ma3": round(ma3, 0), "ma8": round(ma8, 0), "ma20": round(ma20, 0),
            "reason": f"API 15분봉: MA3={ma3:,.0f}, MA8={ma8:,.0f}, MA20={ma20:,.0f}",
        }
    except Exception as e:
        logger.error(f"fetch_tf15_from_api({code}) 오류: {e}")
        return {"trend": "NEUTRAL", "aligned": False, "reason": f"API 오류: {e}"}
