"""
stock_eval_tools.py
주가 상승 가능성 평가 도구
- 모멘텀 (5/20/60일 수익률)
- 거래량 폭발 (20일 평균 대비)
- 이동평균선 정배열
- 상대강도 (코스피 대비)
- 52주 신고가 근접도
- 기관/외국인 순매수 (KIS API)
- 공매도 비율
- 섹터 모멘텀
"""

import os
import logging
import datetime as dt
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
    "105560": "IT",      # KB금융
    "055550": "금융",    # 신한지주
    "003550": "금융",    # LG
    "066570": "IT",      # LG전자
    "096770": "건설",    # SK이노베이션
    "028260": "IT",      # 삼성물산
    "034730": "IT",      # SK
    "012330": "자동차",  # 현대모비스
    "009150": "화학",    # 삼성전기
}


# ──────────────────────────────────────────────
# 1. 가격 데이터 수집 (yfinance)
# ──────────────────────────────────────────────
def _to_yf_ticker(code: str) -> str:
    """한국 종목코드 → yfinance 티커 변환"""
    code = code.replace(".KS", "").replace(".KQ", "")
    if code.startswith("^"):
        return code
    return f"{code}.KS"


def fetch_price_data(code: str, period: str = "6mo"):
    """yfinance에서 가격 데이터 가져오기"""
    if yf is None:
        logger.warning("yfinance 미설치")
        return None
    try:
        ticker = _to_yf_ticker(code)
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df is None or df.empty:
            logger.warning(f"{code}: 가격 데이터 없음")
            return None
        # MultiIndex 처리
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        logger.error(f"{code} 가격 수집 실패: {e}")
        return None


# ──────────────────────────────────────────────
# 2. 모멘텀 점수
# ──────────────────────────────────────────────
def calc_momentum(df) -> dict:
    """5일/20일/60일 수익률 기반 모멘텀"""
    if df is None or len(df) < 60:
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    cur = float(close.iloc[-1])

    ret_5d = (cur / float(close.iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
    ret_20d = (cur / float(close.iloc[-21]) - 1) * 100 if len(df) >= 21 else 0
    ret_60d = (cur / float(close.iloc[-61]) - 1) * 100 if len(df) >= 61 else 0

    score = 0
    # 5일 수익률
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

    # 60일 수익률
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
def calc_volume_surge(df) -> dict:
    """최근 거래량 vs 20일 평균"""
    if df is None or len(df) < 21:
        return {"score": 0, "detail": "데이터 부족"}

    vol = df["Volume"]
    avg_20 = float(vol.iloc[-21:-1].mean())
    if avg_20 == 0:
        return {"score": 0, "ratio": 0}

    recent_vol = float(vol.iloc[-1])
    ratio = recent_vol / avg_20

    # 최근 5일 평균도 확인 (지속적 거래량 증가)
    avg_5 = float(vol.iloc[-5:].mean())
    ratio_5d = avg_5 / avg_20

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
def calc_ma_alignment(df) -> dict:
    """5 > 20 > 60 > 120일 이동평균선 정배열"""
    if df is None or len(df) < 120:
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    ma5 = float(close.iloc[-5:].mean())
    ma20 = float(close.iloc[-20:].mean())
    ma60 = float(close.iloc[-60:].mean())
    ma120 = float(close.iloc[-120:].mean())
    cur = float(close.iloc[-1])

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
def calc_relative_strength(df, code: str) -> dict:
    """코스피 대비 상대 수익률"""
    if df is None or len(df) < 21:
        return {"score": 0, "detail": "데이터 부족"}

    # 코스피 데이터
    kospi_df = fetch_price_data(KOSPI_TICKER, period="3mo")
    if kospi_df is None or len(kospi_df) < 21:
        return {"score": 0, "detail": "코스피 데이터 없음"}

    # 20일 수익률 비교
    stock_ret = float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-21]) - 1
    kospi_ret = float(kospi_df["Close"].iloc[-1]) / float(kospi_df["Close"].iloc[-21]) - 1

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
def calc_52w_high_proximity(df) -> dict:
    """현재가가 52주 고가 대비 위치"""
    if df is None or len(df) < 60:
        return {"score": 0, "detail": "데이터 부족"}

    close = df["Close"]
    high_52w = float(df["High"].max())
    low_52w = float(df["Low"].min())
    cur = float(close.iloc[-1])

    if high_52w == low_52w:
        return {"score": 0, "pct": 0}

    # 52주 범위 내 위치 (0~100%)
    position = (cur - low_52w) / (high_52w - low_52w) * 100
    # 고가 대비 %
    from_high = (cur / high_52w - 1) * 100

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
# 7. 기관/외국인 순매수 (KIS API 연동 준비)
# ──────────────────────────────────────────────
def fetch_investor_data(code: str) -> dict:
    """
    기관/외국인 순매수 데이터.
    KIS API 투자자별 매매동향 엔드포인트 사용.
    API가 없으면 yfinance 대안 사용.
    """
    result = {"score": 0, "foreign_net": 0, "inst_net": 0, "detail": ""}

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
                # 최근 5일 누적
                foreign_total = 0
                inst_total = 0
                days_checked = min(5, len(output))
                for i in range(days_checked):
                    row = output[i]
                    foreign_total += int(row.get("frgn_ntby_qty", 0))
                    inst_total += int(row.get("orgn_ntby_qty", 0))

                result["foreign_net"] = foreign_total
                result["inst_net"] = inst_total

                score = 0
                # 외국인 5일 순매수
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

                # 기관 5일 순매수
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

                result["score"] = score
                result["detail"] = f"외국인 {foreign_total:+,}주, 기관 {inst_total:+,}주 (5일)"
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
    ret_20d = (float(close.iloc[-1]) / float(close.iloc[-21]) - 1) * 100

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
# 9. 종합 평가
# ──────────────────────────────────────────────
def evaluate_stock(code: str, macro_sectors: dict = None) -> dict:
    """
    종목 종합 평가.
    macro_sectors: macro_analyst에서 넘어온 유망/회피 섹터 정보
      예: {"sectors": ["반도체", "2차전지"], "avoid_sectors": ["건설"]}
    """
    logger.info(f"📊 {code} 종목 평가 시작...")

    # 가격 데이터 수집
    df = fetch_price_data(code, period="6mo")

    # 각 지표 계산
    momentum = calc_momentum(df)
    volume = calc_volume_surge(df)
    ma_align = calc_ma_alignment(df)
    rel_strength = calc_relative_strength(df, code)
    high_52w = calc_52w_high_proximity(df)
    investor = fetch_investor_data(code)
    sector = calc_sector_momentum(code)

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

    # 종합 점수 (멀티플라이어 전 원점수)
    raw_score = (
        momentum["score"]
        + volume["score"]
        + ma_align["score"]
        + rel_strength["score"]
        + high_52w["score"]
        + investor["score"]
        + sector["score"]
        + macro_bonus
    )
    # 멀티플라이어 적용
    total_score = round(raw_score * sector_multiplier)

    # 등급 산출
    if total_score >= 15:
        grade = "A+"
        position_pct = 1.0
        action = "적극매수"
    elif total_score >= 10:
        grade = "A"
        position_pct = 0.8
        action = "매수"
    elif total_score >= 6:
        grade = "B"
        position_pct = 0.6
        action = "조건부매수"
    elif total_score >= 2:
        grade = "C"
        position_pct = 0.4
        action = "소량매수"
    elif total_score >= -2:
        grade = "D"
        position_pct = 0.0
        action = "매수보류"
    else:
        grade = "F"
        position_pct = 0.0
        action = "매수금지"

    result = {
        "code": code,
        "grade": grade,
        "total_score": total_score,
        "raw_score": raw_score,
        "position_pct": position_pct,
        "action": action,
        "details": {
            "momentum": momentum,
            "volume": volume,
            "ma_alignment": ma_align,
            "relative_strength": rel_strength,
            "high_52w": high_52w,
            "investor": investor,
            "sector": sector,
            "macro_bonus": macro_bonus,
            "sector_multiplier": sector_multiplier,
        },
        "timestamp": dt.datetime.now().isoformat(),
    }

    logger.info(
        f"✅ {code} 평가 완료: {grade} ({total_score}점) → {action}"
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
    logging.basicConfig(level=logging.INFO)
    result = evaluate_stock("005930")  # 삼성전자
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
