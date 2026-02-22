"""
stock_eval_tools.py
주가 상승 가능성 평가 도구
- 모멘텀 (5/20/60일 수익률)
- 거래량 폭발 (20일 평균 대비)
- 이동평균선 정배열
- 상대강도 (코스피 대비)
- 52주 신고가 근접도
- 기관/외국인 순매수 (KIS API)
- 섹터 모멘텀
"""

import logging
import time
import datetime as dt
from typing import Optional

def safe_float(val, default=0.0):
    """pandas Series/numpy -> float safely"""
    try:
        if hasattr(val, 'iloc'): val = val.iloc[-1]
        if hasattr(val, 'item'): return float(val.item())
        return float(val)
    except (TypeError, ValueError, IndexError): return default



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

logger = logging.getLogger("stock_eval")

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
KOSPI_TICKER = "^KS11"
KOSDAQ_TICKER = "^KQ11"

# 섹터별 대표 ETF (한국)
SECTOR_ETFS = {
    "반도체": "091160.KS",    # KODEX 반도체
    "2차전지": "305720.KS",   # KODEX 2차전지
    "바이오": "244580.KS",    # KODEX 바이오
    "자동차": "091180.KS",    # KODEX 자동차
    "금융": "091170.KS",      # KODEX 은행
    "철강": "117680.KS",      # KODEX 철강
    "IT": "315270.KS",        # KODEX IT
    "화학": "117690.KS",      # KODEX 화학
    "건설": "117700.KS",      # KODEX 건설
    "에너지": "117460.KS",    # KODEX 에너지화학
}

# 종목 → 섹터 매핑 (주요 종목)
STOCK_SECTOR_MAP = {
    "005930": "반도체",  # 삼성전자
    "000660": "반도체",  # SK하이닉스
    "373220": "2차전지", # LG에너지솔루션
    "051910": "화학",    # LG화학
    "006400": "2차전지", # 삼성SDI
    "005380": "자동차",  # 현대차
    "000270": "자동차",  # 기아
    "035420": "IT",      # NAVER
    "035720": "IT",      # 카카오
    "207940": "바이오",  # 삼성바이오로직스
    "068270": "바이오",  # 셀트리온
    "105560": "금융",    # KB금융
    "055550": "금융",    # 신한지주
    "003550": "화학",    # LG
    "066570": "IT",      # LG전자
    "096770": "에너지",  # SK이노베이션
    "028260": "건설",    # 삼성물산
    "034730": "IT",      # SK
    "012330": "자동차",  # 현대모비스
    "009150": "화학",    # 삼성전기
}

# ── 가격 데이터 캐시 (같은 스캔 사이클 내 중복 호출 방지) ──
_price_cache = {}
_cache_ts = time.time()  # 현재 시각으로 초기화 (첫 호출 시 불필요한 클리어 방지)
PRICE_CACHE_TTL = 300  # 5분
MAX_CACHE_SIZE = 100   # 캐시 최대 항목 수 (메모리 누수 방지)


def _clear_stale_cache():
    """캐시 TTL 초과 또는 크기 초과 시 자동 정리"""
    global _price_cache, _cache_ts
    now = time.time()
    if now - _cache_ts > PRICE_CACHE_TTL or len(_price_cache) > MAX_CACHE_SIZE:
        _price_cache.clear()
        _cache_ts = now


# ──────────────────────────────────────────────
# 1. 가격 데이터 수집 (yfinance + 캐시)
# ──────────────────────────────────────────────
def _to_yf_ticker(code: str) -> str:
    """한국 종목코드 → yfinance 티커 변환"""
    code = code.replace(".KS", "").replace(".KQ", "")
    if code.startswith("^"):
        return code
    return f"{code}.KS"


def fetch_price_data(code: str, period: str = "6mo") -> Optional["pd.DataFrame"]:
    """yfinance에서 가격 데이터 가져오기 (캐시 적용)"""
    if yf is None:
        logger.warning("yfinance 미설치")
        return None

    _clear_stale_cache()

    cache_key = f"{code}_{period}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    try:
        ticker = _to_yf_ticker(code)
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df is None or df.empty:
            logger.warning(f"{code}: 가격 데이터 없음")
            return None
        # MultiIndex 처리
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        _price_cache[cache_key] = df
        return df
    except Exception as e:
        logger.error(f"{code} 가격 수집 실패: {e}")
        return None


# ──────────────────────────────────────────────
# 2. 모멘텀 점수
# ──────────────────────────────────────────────
def _validate_df(df, min_rows: int = 20) -> bool:
    """DataFrame 유효성 검증 (공통 헬퍼)"""
    if df is None or len(df) < min_rows:
        return False
    required = {"Close", "High", "Low", "Volume"}
    return required.issubset(set(df.columns))


def calc_momentum(df: "pd.DataFrame") -> dict:
    """5일/20일/60일 수익륨 기반 모멘텀"""
    if not _validate_df(df, 60):
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    cur = safe_float(close.iloc[-1])

    ret_5d = (cur / safe_float(close.iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
    ret_20d = (cur / safe_float(close.iloc[-21]) - 1) * 100 if len(df) >= 21 else 0
    ret_60d = (cur / safe_float(close.iloc[-61]) - 1) * 100 if len(df) >= 61 else 0

    score = 0
    # 5인 수익률
    if ret_5d > 5:
        score += 3
    elif ret_5d > 2:
        score += 2
    elif ret_5d > 0:
        score += 1
    elif ret_5d < -5:
        score -= 2
    elif ret_5d < -2:
        score -= 1

    # 20일 수익률
    if ret_20d > 10:
        score += 3
    elif ret_20d > 5:
        score += 2
    elif ret_20d > 0:
        score += 1
    elif ret_20d < -10:
        score -= 2
    elif ret_20d < -5:
        score -= 1

    # 60인 수익률
    if ret_60d > 20:
        score += 2
    elif ret_60d > 10:
        score += 1
    elif ret_60d < -20:
        score -= 2
    elif ret_60d < -10:
        score -= 1

    return {
        "score": score,
        "ret_5d": round(ret_5d, 2),
        "ret_20d": round(ret_20d, 2),
        "ret_60d": round(ret_60d, 2),
    }


# ──────────────────────────────────────────────
# 3. 거래량 폭발
# ──────────────────────────────────────────────
def calc_volume_surge(df: "pd.DataFrame") -> dict:
    """최근 거래량 vs 20일 평균"""
    if not _validate_df(df, 21):
        return {"score": 0, "detail": "데이터 부족"}

    vol = df["Volume"]
    avg_20 = safe_float(vol.iloc[-21:-1].mean())
    if avg_20 == 0:
        return {"score": 0, "ratio": 0}

    recent_vol = safe_float(vol.iloc[-1])
    ratio = recent_vol / (avg_20 or 1)

    # 최근 5일 평균도 확인 (지속적 거래량 증가)
    avg_5 = safe_float(vol.iloc[-5:].mean())
    ratio_5d = avg_5 / (avg_20 or 1)

    score = 0
    if ratio > 5:
        score += 4    # 거래량 5배 이상 폭발
    elif ratio > 3:
        score += 3
    elif ratio > 2:
        score += 2
    elif ratio > 1.5:
        score += 1

    # 5일간 지속적 증가 보너스
    if ratio_5d > 2:
        score += 2
    elif ratio_5d > 1.5:
        score += 1

    return {
        "score": score,
        "today_ratio": round(ratio, 2),
        "5d_avg_ratio": round(ratio_5d, 2),
    }


# ──────────────────────────────────────────────
# 4. 이동평균선 정배열
# ──────────────────────────────────────────────
def calc_ma_alignment(df: "pd.DataFrame") -> dict:
    """5 > 20 > 60 > 120일 이동평균선 정배열"""
    if not _validate_df(df, 120):
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    ma5 = safe_float(close.iloc[-5:].mean())
    ma20 = safe_float(close.iloc[-20:].mean())
    ma60 = safe_float(close.iloc[-60:].mean())
    ma120 = safe_float(close.iloc[-120:].mean())
    cur = safe_float(close.iloc[-1])

    score = 0
    aligned = []

    # 현재가 > MA5
    if cur > ma5:
        score += 1
        aligned.append("가격>MA5")

    # MA5 > MA20
    if ma5 > ma20:
        score += 1
        aligned.append("MA5>MA20")

    # MA20 > MA60
    if ma20 > ma60:
        score += 1
        aligned.append("MA20>MA60")

    # MA60 > MA120
    if ma60 > ma120:
        score += 1
        aligned.append("MA60>MA120")

    # 완벽한 정배열 보너스
    if score == 4:
        score += 2  # 총 6점

    # 역배열(하락추세) 페널티
    if cur < ma120 and ma5 < ma20 < ma60:
        score = -3

    return {
        "score": score,
        "aligned": aligned,
        "ma5": round(ma5, 0),
        "ma20": round(ma20, 0),
        "ma60": round(ma60, 0),
        "ma120": round(ma120, 0),
    }


# ──────────────────────────────────────────────
# 5. 코스피 대비 상대강도
# ──────────────────────────────────────────────
def calc_relative_strength(df: "pd.DataFrame", code: str) -> dict:
    """코스피 대비 상대 수익률"""
    if not _validate_df(df, 21):
        return {"score": 0, "detail": "데이터 부족"}

    # 코스피 데이터 (캐시 활용)
    kospi_df = fetch_price_data(KOSPI_TICKER, period="3mo")
    if kospi_df is None or len(kospi_df) < 21:
        return {"score": 0, "detail": "코스피 데이터 없음"}

    # 20인 수익률 비교
    stock_ret = safe_float(df["Close"].iloc[-1]) / safe_float(df["Close"].iloc[-21]) - 1
    kospi_ret = safe_float(kospi_df["Close"].iloc[-1]) / safe_float(kospi_df["Close"].iloc[-21]) - 1

    rs = (stock_ret - kospi_ret) * 100  # 상대강도 (%)

    score = 0
    if rs > 10:
        score += 3  # 코스피 대비 10% 이상 초과
    elif rs > 5:
        score += 2
    elif rs > 2:
        score += 1
    elif rs < -10:
        score -= 2
    elif rs < -5:
        score -= 1

    return {
        "score": score,
        "relative_strength_pct": round(rs, 2),
        "stock_ret_20d": round(stock_ret * 100, 2),
        "kospi_ret_20d": round(kospi_ret * 100, 2),
    }


# ──────────────────────────────────────────────
# 6. 52주 신고가 근접도
# ──────────────────────────────────────────────
def calc_52w_high_proximity(df: "pd.DataFrame") -> dict:
    """현재가가 52주 고가 대비 위치"""
    if not _validate_df(df, 60):
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    high_52w = safe_float(df["High"].max())
    low_52w = safe_float(df["Low"].min())
    cur = safe_float(close.iloc[-1])

    if high_52w == low_52w:
        return {"score": 0, "pct": 0}

    # 52주 범위 내 위치 (0~100%)
    position = (cur - low_52w) / (high_52w - low_52w) * 100
    # 고가 대비 %
    from_high = (cur / (high_52w or 1) - 1) * 100

    score = 0
    if from_high > -2:
        score += 3  # 신고가 근처 (2% 이내)
    elif from_high > -5:
        score += 2
    elif from_high > -10:
        score += 1
    elif from_high < -30:
        score -= 2  # 고점 대비 30% 이상 하락
    elif from_high < -20:
        score -= 1

    return {
        "score": score,
        "from_high_pct": round(from_high, 2),
        "position_pct": round(position, 2),
        "high_52w": round(high_52w, 0),
        "low_52w": round(low_52w, 0),
    }


# ──────────────────────────────────────────────
# 7. 기관/외국인 순매수 (KIS API 연동)
# ──────────────────────────────────────────────
def fetch_investor_data(code: str) -> dict:
    """
    기관/외국인 순매수 데이터 (3일 가중 누적)
    KIS API 투자자별 맢맡동향 엔드포인트 사용.
    API가 없으면 스킵.
    최근일에 높은 가중치 부여 → 수급 연속성 반영
    """
    try:
        from config.settings import (
            INVESTOR_CUMUL_DAYS,
            INVESTOR_WEIGHT_DAY1, INVESTOR_WEIGHT_DAY2, INVESTOR_WEIGHT_DAY3,
        )
    except ImportError:
        INVESTOR_CUMUL_DAYS = 3
        INVESTOR_WEIGHT_DAY1, INVESTOR_WEIGHT_DAY2, INVESTOR_WEIGHT_DAY3 = 1.5, 1.0, 0.5

    result = {"score": 0, "foreign_net": 0, "inst_net": 0, "detail": ""}

    # 일별 가중치 (최근일이 가장 높음)
    day_weights = [INVESTOR_WEIGHT_DAY1, INVESTOR_WEIGHT_DAY2, INVESTOR_WEIGHT_DAY3]

    # KIS API 시도
    try:
        from tools.kis_api import get_headers, BASE_URL
        import requests

        headers = get_headers("FHKST01010900")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
            headers=headers,
            params=params,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            output = data.get("output", [])
            if output and len(output) > 0:
                # 최근 3일 가중 누적 (연속성 반영)
                foreign_total = 0.0
                inst_total = 0.0
                foreign_raw = 0
                inst_raw = 0
                days_checked = min(INVESTOR_CUMUL_DAYS, len(output))
                consecutive_foreign_buy = 0
                consecutive_inst_buy = 0

                for i in range(days_checked):
                    row = output[i]
                    w = day_weights[i] if i < len(day_weights) else 0.5
                    f_qty = int(row.get("frgn_ntby_qty", 0))
                    i_qty = int(row.get("orgn_ntby_qty", 0))
                    foreign_total += f_qty * w
                    inst_total += i_qty * w
                    foreign_raw += f_qty
                    inst_raw += i_qty
                    # 연속 매수일 체크
                    if f_qty > 0:
                        consecutive_foreign_buy += 1
                    if i_qty > 0:
                        consecutive_inst_buy += 1

                result["foreign_net"] = round(foreign_total)
                result["inst_net"] = round(inst_total)
                result["foreign_raw"] = foreign_raw
                result["inst_raw"] = inst_raw

                score = 0
                # 외국인 가중 순매수
                if foreign_total > 100000:
                    score += 3
                elif foreign_total > 50000:
                    score += 2
                elif foreign_total > 10000:
                    score += 1
                elif foreign_total < -100000:
                    score -= 2
                elif foreign_total < -50000:
                    score -= 1

                # 기관 가중 순매수
                if inst_total > 100000:
                    score += 2
                elif inst_total > 50000:
                    score += 1
                elif inst_total < -100000:
                    score -= 2
                elif inst_total < -50000:
                    score -= 1

                # 외국인+기관 동시 순매수 보너스
                if foreign_total > 10000 and inst_total > 10000:
                    score += 2

                # 3일 연속 매수 보너스 (수급 연속성)
                if consecutive_foreign_buy == days_checked:
                    score += 1
                if consecutive_inst_buy == days_checked:
                    score += 1

                result["score"] = score
                result["consecutive_foreign"] = consecutive_foreign_buy
                result["consecutive_inst"] = consecutive_inst_buy
                result["detail"] = (
                    f"외국인 {foreign_raw:+,}주, 기관 {inst_raw:+,}주 "
                    f"({days_checked}일 가중, 연속매수 외{consecutive_foreign_buy}/기{consecutive_inst_buy})"
                )
                return result
    except Exception as e:
        logger.debug(f"KIS 투자자 데이터 조회 실패: {e}")

    result["detail"] = "투자자 데이터 없음 (KIS API 미연결)"
    return result


# ──────────────────────────────────────────────
# 8. 섹터 모멘텀
# ──────────────────────────────────────────────
def calc_sector_momentum(code: str) -> dict:
    """해당 종목 섹터의 최근 모멘텀"""
    sector = STOCK_SECTOR_MAP.get(code, None)
    if sector is None:
        return {"score": 0, "sector": "미분류", "detail": "섹터 정보 없음"}

    etf_ticker = SECTOR_ETFS.get(sector)
    if etf_ticker is None:
        return {"score": 0, "sector": sector, "detail": "섹터 ETF 없음"}

    df = fetch_price_data(etf_ticker, period="3mo")
    if df is None or len(df) < 21:
        return {"score": 0, "sector": sector, "detail": "데이터 부족"}

    close = df["Close"]
    ret_20d = (safe_float(close.iloc[-1]) / safe_float(close.iloc[-21]) - 1) * 100

    score = 0
    if ret_20d > 8:
        score += 3
    elif ret_20d > 4:
        score += 2
    elif ret_20d > 0:
        score += 1
    elif ret_20d < -8:
        score -= 2
    elif ret_20d < -4:
        score -= 1

    return {
        "score": score,
        "sector": sector,
        "sector_ret_20d": round(ret_20d, 2),
        "etf": etf_ticker,
    }


# ──────────────────────────────────────────────
# 9. VWAP 스코어링
# ──────────────────────────────────────────────
def calc_vwap_score(df: "pd.DataFrame", precomputed_vwap: dict = None) -> dict:
    """
    VWAP 대비 현재가 위치를 점수화
    - VWAP 위: +2점, 크게 위(+3% 이상): +1점 보너스
    - VWAP 아래: -2점

    precomputed_vwap: scanner_tools에서 이미 계산된 VWAP 결과 (중복 계산 방지)
    """
    if precomputed_vwap:
        vwap_data = precomputed_vwap
    else:
        try:
            from tools.scanner_tools import calc_vwap
            vwap_data = calc_vwap(df)
        except ImportError:
            return {"score": 0, "detail": "scanner_tools 미사용"}

    if vwap_data["vwap"] == 0:
        return {"score": 0, "detail": "VWAP 계산 불가"}

    score = 0
    if vwap_data["price_above_vwap"]:
        score += 2
        if vwap_data["deviation_pct"] >= 3.0:
            score += 1  # VWAP 대비 +3% 이상 보너스
    else:
        score -= 2
        if vwap_data["deviation_pct"] <= -3.0:
            score -= 1  # VWAP 대비 -3% 이하 추가 감점

    return {
        "score": score,
        "vwap": vwap_data["vwap"],
        "above_vwap": vwap_data["price_above_vwap"],
        "deviation_pct": vwap_data["deviation_pct"],
    }


# ──────────────────────────────────────────────
# 10. 종합 평가
# ──────────────────────────────────────────────
def evaluate_stock(code: str, macro_sectors: dict = None,
                    scanner_result: dict = None) -> dict:
    """
    종목 종합 평가.
    macro_sectors: macro_analyst에서 넘어온 유망/회피 섹터 정보
      예: {"sectors": ["반도체", "2차전지"], "avoid_sectors": ["건설"]}
    scanner_result: scanner_tools.apply_tech_filter() 결과 (중복 계산 방지)
      VWAP, ATR 등을 재활용
    """
    from config.settings import RS_ENTRY_THRESHOLD

    logger.info(f"📊 {code} 종목 평가 시작...")

    # 가격 데이터 수집 (캐시 활용)
    df = fetch_price_data(code, period="6mo")

    # 각 지표 계산
    momentum = calc_momentum(df)
    volume = calc_volume_surge(df)
    ma_align = calc_ma_alignment(df)
    rel_strength = calc_relative_strength(df, code)
    high_52w = calc_52w_high_proximity(df)
    investor = fetch_investor_data(code)
    sector = calc_sector_momentum(code)
    # ★ VWAP 스코어링 — scanner 결과가 있으면 재활용 (중복 계산 방지)
    precomputed_vwap = None
    if scanner_result and "vwap" in scanner_result:
        precomputed_vwap = {
            "vwap": scanner_result["vwap"],
            "price_above_vwap": scanner_result.get("vwap_above", False),
            "deviation_pct": scanner_result.get("vwap_deviation_pct", 0),
        }
    vwap_score = calc_vwap_score(df, precomputed_vwap=precomputed_vwap)

    # [기능6] 매크로 연동 — 섹터 멀티플라이어 기반
    macro_bonus = 0
    sector_multiplier = 1.0
    if macro_sectors:
        stock_sector = STOCK_SECTOR_MAP.get(code)
        if stock_sector:
            # 멀티플라이어 적용 (shared_state에서 전달)
            multipliers = macro_sectors.get("sector_multipliers", {})
            if multipliers and stock_sector in multipliers:
                sector_multiplier = multipliers[stock_sector]
            else:
                # 멀티플라이어 없을 때 기존 방식 폴백
                if stock_sector in macro_sectors.get("sectors", []):
                    macro_bonus = 3
                elif stock_sector in macro_sectors.get("avoid_sectors", []):
                    macro_bonus = -4

    # 종합 점수 (기존 8개 + VWAP = 9개 모듈)
    raw_score = (
        momentum["score"]
        + volume["score"]
        + ma_align["score"]
        + rel_strength["score"]
        + high_52w["score"]
        + investor["score"]
        + sector["score"]
        + vwap_score["score"]
        + macro_bonus
    )
    # 멀티플라이어 적용
    total_score = round(raw_score * sector_multiplier)

    # 등급 산출 (VWAP 추가로 점수 범위 확대 → 기준 조정)
    if total_score >= 17:
        grade = "A+"
        position_pct = 1.0
        action = "적극매수"
    elif total_score >= 12:
        grade = "A"
        position_pct = 0.8
        action = "매수"
    elif total_score >= 7:
        grade = "B"
        position_pct = 0.6
        action = "조건부매수"
    elif total_score >= 3:
        grade = "C"
        position_pct = 0.4
        action = "소량매수"
    elif total_score >= -1:
        grade = "D"
        position_pct = 0.0
        action = "매수보류"
    else:
        grade = "F"
        position_pct = 0.0
        action = "매수금지"

    # ★ RS 진입 게이트: RS 점수 미달 시 경고
    rs_warning = None
    if rel_strength["score"] < RS_ENTRY_THRESHOLD and grade in ("A+", "A", "B"):
        rs_warning = f"RS 미달 ({rel_strength['score']}점 < {RS_ENTRY_THRESHOLD}) — 진입 시 주의"

    result = {
        "code": code,
        "grade": grade,
        "total_score": total_score,
        "raw_score": raw_score,
        "position_pct": position_pct,
        "action": action,
        "rs_warning": rs_warning,
        "details": {
            "momentum": momentum,
            "volume": volume,
            "ma_alignment": ma_align,
            "relative_strength": rel_strength,
            "high_52w": high_52w,
            "investor": investor,
            "sector": sector,
            "vwap": vwap_score,
            "macro_bonus": macro_bonus,
            "sector_multiplier": sector_multiplier,
        },
        "timestamp": dt.datetime.now().isoformat(),
    }

    logger.info(
        f"✅ {code} 평가 완료: {grade} ({total_score}점) → {action}"
        + (f" ⚠️ {rs_warning}" if rs_warning else "")
    )
    return result


# ──────────────────────────────────────────────
# 10. 여러 종목 일괄 평가
# ──────────────────────────────────────────────
def evaluate_multiple(codes: list, macro_sectors: dict = None) -> list:
    """여러 종목 일괄 평가, 점수 높은 순 정렬"""
    results = []
    for code in codes:
        try:
            r = evaluate_stock(code, macro_sectors)
            results.append(r)
        except Exception as e:
            logger.error(f"{code} 평가 실패: {e}")
            results.append({
                "code": code,
                "grade": "F",
                "total_score": -99,
                "action": "평가실패",
                "error": str(e),
            })
    results.sort(key=lambda x: x.get("total_score", -99), reverse=True)
    return results


# ──────────────────────────────────────────────
# 테스트용
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import json

def safe_int(val, default=0):
    """int() 안전 래퍼 - pandas Series, NaN, 빈 문자열 등 처리"""
    try:
        if hasattr(val, 'iloc'):
            val = val.iloc[0] if len(val) > 0 else default
        if val is None or val == '':
            return default
        import math
        if isinstance(val, float) and math.isnan(val):
            return default
        return int(float(val))
    except (ValueError, TypeError, IndexError):
        return default

    logging.basicConfig(level=logging.INFO)
    result = evaluate_stock("005930")  # 삼성전자
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
