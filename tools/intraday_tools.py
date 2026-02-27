# tools/intraday_tools.py — 분봉 데이터 조회 및 추세 분석
# KIS API를 통해 15분봉/60분봉 데이터를 가져와 장중 추세 판단
# stock_eval에서 보너스/페널티로 활용

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("intraday_tools")

KST = timezone(timedelta(hours=9))

# ── 캐시 (분봉 데이터는 자주 바뀌므로 짧은 TTL) ──
import time as _time_mod
_intraday_cache = {}
_cache_ts = _time_mod.time()  # 현재 시각으로 초기화
CACHE_TTL = 120  # 2분
MAX_CACHE_SIZE = 60  # 캐시 최대 항목 수


def _clear_stale_cache():
    global _intraday_cache, _cache_ts
    now = _time_mod.time()
    if now - _cache_ts > CACHE_TTL or len(_intraday_cache) > MAX_CACHE_SIZE:
        _intraday_cache.clear()
        _cache_ts = now


# ═══════════════════════════════════════════════════════
#  KIS API 분봉 데이터 조회
# ═══════════════════════════════════════════════════════

def fetch_intraday_candles(
    code: str,
    interval_minutes: int = 15,
    count: int = 8,
) -> Optional[list]:
    """
    KIS API 분봉 데이터 조회

    Parameters:
        code: 종목코드 (예: "005930")
        interval_minutes: 분봉 간격 (15 또는 60)
        count: 조회할 봉 수

    Returns:
        [{"time": str, "open": float, "high": float,
          "low": float, "close": float, "volume": int}, ...]
        최신 봉이 첫 번째 (역순)
    """
    _clear_stale_cache()

    cache_key = f"{code}_{interval_minutes}m"
    if cache_key in _intraday_cache:
        return _intraday_cache[cache_key]

    try:
        from tools.kis_api import get_headers, BASE_URL
        import requests

        # KIS API 분봉 조회 엔드포인트
        # TR-ID: FHKST03010200 (국내주식 기간별 시세)
        headers = get_headers("FHKST03010200")

        # 현재 시각 기준 시작 시간 계산
        now = datetime.now(KST)
        end_time = now.strftime("%H%M%S")

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": end_time,
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "",  # 분봉 간격은 별도 설정
        }

        # 분봉 간격에 따라 다른 엔드포인트/파라미터 사용
        # KIS API 분봉 조회: /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=headers,
            params=params,
            timeout=10,
        )

        if resp.status_code != 200:
            logger.warning(f"{code} 분봉 조회 실패: HTTP {resp.status_code}")
            return None

        data = resp.json()
        output = data.get("output2", [])

        if not output:
            logger.warning(f"{code} 분봉 데이터 없음")
            return None

        # 데이터 파싱
        candles = []
        for row in output[:count]:
            try:
                candle = {
                    "time": row.get("stck_cntg_hour", ""),
                    "open": safe_float(row.get("stck_oprc", 0)),
                    "high": safe_float(row.get("stck_hgpr", 0)),
                    "low": safe_float(row.get("stck_lwpr", 0)),
                    "close": safe_float(row.get("stck_prpr", 0)),
                    "volume": int(row.get("cntg_vol", 0)),
                }
                if candle["close"] > 0:
                    candles.append(candle)
            except (ValueError, TypeError):
                continue

        if candles:
            _intraday_cache[cache_key] = candles

        return candles if candles else None

    except ImportError:
        logger.debug("KIS API 미연결 — 분봉 데이터 조회 불가")
        return None
    except Exception as e:
        logger.error(f"{code} 분봉 조회 오류: {e}", exc_info=True)
        return None


# ═══════════════════════════════════════════════════════
#  15분봉 추세 판단
# ═══════════════════════════════════════════════════════

def analyze_15m_trend(code: str) -> dict:
    """
    15분봉 기반 단기 추세 판단
    - 최근 8개 15분봉 (약 2시간)
    - MA3 vs MA8로 추세 방향 판단
    - 상승추세에서만 신규 매수 허용

    Returns:
        {
            "trend": "UP" | "DOWN" | "NEUTRAL",
            "ma3": float,
            "ma8": float,
            "score": int,  # +2(상승) / 0(중립) / -2(하락)
            "detail": str,
        }
    """
    from config.settings import INTRADAY_15M_BARS

    candles = fetch_intraday_candles(code, interval_minutes=15, count=INTRADAY_15M_BARS)

    if not candles or len(candles) < INTRADAY_15M_BARS:
        return {
            "trend": "NEUTRAL",
            "ma3": 0, "ma8": 0,
            "score": 0,
            "detail": "15분봉 데이터 부족",
        }

    # 종가 추출 (최신→과거 순서이므로 reverse)
    closes = [c["close"] for c in reversed(candles)]

    # MA3, MA8 계산
    ma3 = sum(closes[-3:]) / 3
    ma8 = sum(closes[-8:]) / 8

    # 추세 판단
    from config.settings import INTRADAY_15M_MA_THRESHOLD
    if ma3 > ma8 * (1 + INTRADAY_15M_MA_THRESHOLD):
        trend = "UP"
        score = 2
    elif ma3 < ma8 * (1 - INTRADAY_15M_MA_THRESHOLD):
        trend = "DOWN"
        score = -2
    else:
        trend = "NEUTRAL"
        score = 0

    # 추가: 봉 방향 확인 (최근 3개 봉 중 양봉 수)
    recent_3 = candles[:3]  # 최신 3개
    bullish_count = sum(1 for c in recent_3 if c["close"] > c["open"])
    if bullish_count == 3 and trend == "UP":
        score += 1  # 연속 양봉 보너스

    return {
        "trend": trend,
        "ma3": round(ma3, 0),
        "ma8": round(ma8, 0),
        "score": score,
        "bullish_candles": bullish_count,
        "detail": f"15분봉 추세: {trend} (MA3={ma3:,.0f} vs MA8={ma8:,.0f})",
    }


# ═══════════════════════════════════════════════════════
#  60분봉 중기 추세 판단
# ═══════════════════════════════════════════════════════

def analyze_60m_trend(code: str) -> dict:
    """
    60분봉 기반 중기 추세 판단
    - 최근 5개 60분봉 (장중 호출 적음)
    - 상승/하락 패턴 → stock_eval 보너스/페널티

    Returns:
        {
            "trend": "UP" | "DOWN" | "NEUTRAL",
            "score": int,
            "detail": str,
        }
    """
    from config.settings import INTRADAY_60M_BARS
from tools.utils import safe_float, safe_int

# ═══════════════════════════════════════════════════════
#  통합 분봉 분섭 (stock_eval에서 호출)
# ═══════════════════════════════════════════════════════

def get_intraday_score(code: str) -> dict:
    """
    15분봉 + 60분봉 통합 분석
    stock_eval의 evaluate_stock에서 보너스/페널티로 활용

    Returns:
        {
            "score": int,        # 종합 점수 (±3)
            "trend_15m": str,    # 15분봉 추세
            "trend_60m": str,    # 60분봉 추세
            "allow_entry": bool, # 신규 매수 허용 여부
            "detail": str,
        }
    """
    result_15m = analyze_15m_trend(code)
    result_60m = analyze_60m_trend(code)

    total_score = result_15m["score"] + result_60m["score"]

    # 매수 허용: 15분봉이 DOWN이 아닐 때
    allow_entry = result_15m["trend"] != "DOWN"

    return {
        "score": total_score,
        "trend_15m": result_15m["trend"],
        "trend_60m": result_60m["trend"],
        "allow_entry": allow_entry,
        "detail_15m": result_15m["detail"],
        "detail_60m": result_60m["detail"],
        "detail": f"분봉 종합: 15m={result_15m['trend']}, 60m={result_60m['trend']} ({total_score:+d}점)",
    }


# ── 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Intraday Tools 테스트 ===")
    result = get_intraday_score("005930")
    print(f"삼성전자: {result}")
