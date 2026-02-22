# tools/scanner_tools.py — 종목 스캐닝 & 전략 계산 엔진
# Phase 4 구현: 기술지표 계산, 매수/추가매수 신호 판단, 손절 계산

import math
import numpy as np
from datetime import datetime

try:
    from config.settings import (
        DONCHIAN_PERIOD, VOLUME_SURGE_RATIO, TICK_SPEED_MIN,
        RSI_LOWER, RSI_UPPER, ATR_PERIOD,
        INITIAL_STOP_ATR, TRAILING_STOP_ATR, PYRAMID_STOP_PCT,
        OVERNIGHT_THRESHOLD, PYRAMID_PRICE_TRIGGER, PYRAMID_VOLUME_RATIO,
        PYRAMID_TICK_RATIO, PYRAMID_VOLUME_TREND_MIN,
        PYRAMID_ATR_MULTIPLIER, PYRAMID_MIN_TRIGGER_PCT, PYRAMID_MAX_COUNT,
        TIME_DECAY_ATR,
    )
except ImportError:
    DONCHIAN_PERIOD=20; VOLUME_SURGE_RATIO=2.0; TICK_SPEED_MIN=5
    RSI_LOWER=50; RSI_UPPER=70; ATR_PERIOD=14
    INITIAL_STOP_ATR=2.0; TRAILING_STOP_ATR=3.0; PYRAMID_STOP_PCT=-0.03
    OVERNIGHT_THRESHOLD=0.07; PYRAMID_PRICE_TRIGGER=0.05
    PYRAMID_VOLUME_RATIO=0.50; PYRAMID_TICK_RATIO=0.40; PYRAMID_VOLUME_TREND_MIN=5
    PYRAMID_ATR_MULTIPLIER=1.5; PYRAMID_MIN_TRIGGER_PCT=0.02; PYRAMID_MAX_COUNT=2
    TIME_DECAY_ATR={1: 2.0, 2: 1.0, 3: -0.3}


def calc_atr(df, period=ATR_PERIOD):
    """Average True Range (ATR) — numpy 벡터화 (Wilder 방식)."""
    if len(df) < period + 1: return 0.0
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    tr = np.maximum(h[1:] - l[1:],
                    np.abs(h[1:] - c[:-1]),
                    np.abs(l[1:] - c[:-1]))
    if len(tr) < period:
        return float(tr.mean()) if len(tr) > 0 else 0.0
    atr = float(tr[:period].mean())
    for val in tr[period:]:
        atr = (atr * (period - 1) + val) / period
    return round(atr, 2)


def calc_rsi(df, period=14):
    """Relative Strength Index (RSI) — numpy 벡터화."""
    closes = df["close"].values.astype(float)
    if len(closes) < period + 1: return 50.0
    delta  = np.diff(closes)
    gains  = np.where(delta > 0,  delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    if len(gains) < period: return 50.0
    ag = gains[:period].mean()
    al = losses[:period].mean()
    for g, lo in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g)  / period
        al = (al * (period - 1) + lo) / period
    if al == 0: return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_bollinger(df, period=20):
    """Bollinger Bands — numpy 벡터화. 반환: {upper, mid, lower}"""
    closes = df["close"].values.astype(float)
    if len(closes) < period:
        last = float(closes[-1]) if len(closes) > 0 else 0
        return {"upper": last, "mid": last, "lower": last}
    recent = closes[-period:]
    mid    = float(recent.mean())
    std    = float(recent.std(ddof=0))
    return {"upper": round(mid + 2*std, 2), "mid": round(mid, 2), "lower": round(mid - 2*std, 2)}


def calc_donchian(df, period=DONCHIAN_PERIOD):
    """Donchian Channel 계산. 반환: {upper, lower}"""
    if len(df)<period: return {"upper":0.0,"lower":0.0}
    recent=df.tail(period)
    return {"upper":float(recent['high'].max()),"lower":float(recent['low'].min())}


def check_buy_signal(code, current_price, volume_today, volume_yesterday_same_time, tick_speed, df_ohlcv):
    """
    매수 신호 판단 (최대 100점)
    필수: F1 돈치안돌파(25) + F2 거래량급증(15-25) + F3 틱속도(10-20)
    보조: RSI(8-15) + 볼린저상단돌파(15)
    """
    score=0; filters_passed=[]; reasons=[]
    dc=calc_donchian(df_ohlcv); bb=calc_bollinger(df_ohlcv); rsi=calc_rsi(df_ohlcv)

    f1=current_price>=dc["upper"] and dc["upper"]>0
    if f1: score+=25; filters_passed.append("F1_DONCHIAN"); reasons.append("돈치안돌파")
    else: reasons.append("돈치안미달")

    vr=volume_today/volume_yesterday_same_time if volume_yesterday_same_time>0 else 0.0
    if vr>=4.0: score+=25; filters_passed.append("F2_VOLUME_4X"); f2=True; reasons.append(f"거래량4배({vr:.1f}x)")
    elif vr>=3.0: score+=20; filters_passed.append("F2_VOLUME_3X"); f2=True; reasons.append(f"거래량3배({vr:.1f}x)")
    elif vr>=VOLUME_SURGE_RATIO: score+=15; filters_passed.append("F2_VOLUME_2X"); f2=True; reasons.append(f"거래량2배({vr:.1f}x)")
    else: reasons.append(f"거래량부족({vr:.1f}x)"); f2=False

    if tick_speed>=20: score+=20; filters_passed.append("F3_TICK_20+"); f3=True; reasons.append(f"틱속도20+")
    elif tick_speed>=10: score+=15; filters_passed.append("F3_TICK_10+"); f3=True; reasons.append(f"틱속도10+")
    elif tick_speed>=TICK_SPEED_MIN: score+=10; filters_passed.append("F3_TICK_5+"); f3=True; reasons.append(f"틱속도5+")
    else: reasons.append("틱속도부족"); f3=False

    if RSI_LOWER<=rsi<60: score+=8; reasons.append(f"RSI50-59({rsi:.1f})")
    elif 60<=rsi<66: score+=12; reasons.append(f"RSI60-65({rsi:.1f})")
    elif 66<=rsi<RSI_UPPER: score+=15; reasons.append(f"RSI66-69({rsi:.1f})")
    elif rsi>=RSI_UPPER: reasons.append(f"RSI과열({rsi:.1f})")
    else: reasons.append(f"RSI낮음({rsi:.1f})")

    if current_price>=bb["upper"] and bb["upper"]>0: score+=15; reasons.append("볼린저상단돌파")

    return {
        "signal": f1 and f2 and f3, "score": score,
        "reason": " | ".join(reasons), "filters_passed": filters_passed,
        "rsi": rsi, "donchian_upper": dc["upper"], "bollinger_upper": bb["upper"], "vol_ratio": round(vr,2),
    }


def check_pyramid_signal(code, entry_price, avg_price, current_price,
    entry_volume, current_volume, entry_tick_speed, current_tick_speed,
    df_recent, pyramid_count=0, no_pyramid_after="15:00",
    entry_atr=None):
    """
    추가매수(피라미딩) 조건 판단.
    [기능4] ATR 기반 동적 피라미딩:
      현재가 > 평단 + max(1.5 × ATR, 평단 × 2%) 이면 가격 조건 충족.
      entry_atr: 진입 시 스냅샷한 ATR 값 (없으면 고정 % 폴백).
    """
    from datetime import datetime

    # [1] 최대 피라미딩 횟수 초과
    if pyramid_count >= PYRAMID_MAX_COUNT:
        return False
    # [2] 시간 제한
    if datetime.now().strftime("%H:%M") >= no_pyramid_after:
        return False
    # [3] 가격 조건 — ATR 기반 동적 or 고정 % 폴백
    if entry_atr and entry_atr > 0:
        atr_target = avg_price + entry_atr * PYRAMID_ATR_MULTIPLIER
        min_target = avg_price * (1 + PYRAMID_MIN_TRIGGER_PCT)
        target_price = max(atr_target, min_target)
    else:
        target_price = avg_price * (1 + PYRAMID_PRICE_TRIGGER)
    if current_price < target_price:
        return False
    # [4] 거래량 조건
    if entry_volume > 0 and current_volume < entry_volume * PYRAMID_VOLUME_RATIO:
        return False
    # [5] 틱 속도 조건
    if entry_tick_speed > 0 and current_tick_speed < entry_tick_speed * PYRAMID_TICK_RATIO:
        return False
    return True


def calc_pyramid_add_ratio(entry_atr, avg_price):
    """
    [기능4] 피라미딩 추가 수량 비율을 ATR에 반비례로 결정.
    변동성 큰 종목 → 적게, 안정적 종목 → 많이.
    반환: 0.15 ~ 0.40 범위의 추가 비율.
    """
    if not entry_atr or entry_atr <= 0 or avg_price <= 0:
        return 0.30  # 기본값
    volatility_pct = entry_atr / avg_price
    if volatility_pct >= 0.04:    # 고변동성 (4%+)
        return 0.15
    elif volatility_pct >= 0.025:  # 중변동성
        return 0.25
    else:                          # 저변동성
        return 0.40


def calc_stop_loss(entry_price, atr):
    """초기 손절가: 진입가 - ATR x INITIAL_STOP_ATR"""
    return round(entry_price - atr * INITIAL_STOP_ATR, 0)


def calc_trailing_stop(high_price, atr):
    """트레일링 손절가: 최고가 - ATR x TRAILING_STOP_ATR"""
    return round(high_price - atr * TRAILING_STOP_ATR, 0)


def calc_time_decay_stop(entry_price, atr, holding_days):
    """
    [기능5] 타임 디케이 손절가.
    일차별 ATR 배수가 줄어들며, Day 3+에서는 진입가 + 버퍼 이상.
    holding_days: 보유 일수 (1부터 시작, 장 마감 기준 카운트).
    """
    if holding_days <= 0:
        holding_days = 1
    # 설정된 일차별 배수 가져오기 (Day 3 이후는 Day 3 값 유지)
    max_day = max(TIME_DECAY_ATR.keys())
    day_key = min(holding_days, max_day)
    atm = TIME_DECAY_ATR.get(day_key, TIME_DECAY_ATR[max_day])
    return round(entry_price - atr * atm, 0)


def calc_effective_stop(entry_price, high_price, atr, holding_days):
    """
    [기능5] 실효 손절가 = MAX(타임디케이 손절, 트레일링 손절).
    두 기준 중 더 높은 값(더 보수적)을 적용.
    """
    td_stop = calc_time_decay_stop(entry_price, atr, holding_days)
    tr_stop = calc_trailing_stop(high_price, atr)
    return max(td_stop, tr_stop)


def calc_holding_days(entry_time_str):
    """
    진입 시각으로부터 보유 일수 계산 (장 마감 15:30 기준).
    15:30 이전 진입 → 당일을 Day 1로 카운트.
    15:30 이후 진입 → 익일을 Day 1로 카운트.
    """
    from datetime import timedelta as _td
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
    except (ValueError, TypeError):
        return 1

    now = datetime.now()
    market_close_hour = 15
    market_close_min = 30

    # 진입일 기준일 결정
    after_close = (entry_dt.hour > market_close_hour or
                   (entry_dt.hour == market_close_hour and entry_dt.minute >= market_close_min))

    entry_base = entry_dt.date()
    current_base = now.date()

    # 영업일 차이 (주말 제외 간이 계산)
    delta = (current_base - entry_base).days
    if delta <= 0:
        return 1

    # 주말 제외 (간이): 총 일수에서 주말 수 빼기
    weeks = delta // 7
    remainder = delta % 7
    weekday_start = entry_base.weekday()
    weekend_days = weeks * 2
    for i in range(1, remainder + 1):
        d = (weekday_start + i) % 7
        if d >= 5:  # 토(5), 일(6)
            weekend_days += 1
    biz_days = delta - weekend_days

    if after_close:
        # 장 마감 이후 진입: 익일부터 Day 1 → biz_days 그대로 사용
        return max(biz_days, 1)
    else:
        # 장중 진입: 당일이 Day 1 → 영업일 차이에 +1
        return max(biz_days + 1, 1)


def calc_pyramid_stop(avg_price):
    """추가매수 후 손절가: 평단 x (1 + PYRAMID_STOP_PCT)"""
    return round(avg_price * (1 + PYRAMID_STOP_PCT), 0)


def is_overnight_candidate(entry_price, current_price):
    """(레거시) 수익률 >= OVERNIGHT_THRESHOLD 이면 True. 종합 평가는 evaluate_overnight 사용."""
    if entry_price <= 0:
        return False
    return (current_price - entry_price) / entry_price >= OVERNIGHT_THRESHOLD


# ══════════════════════════════════════════════════════════════
#  오버나이트 종합 평가 시스템
# ══════════════════════════════════════════════════════════════

def _score_profit(entry_price: float, current_price: float) -> tuple:
    """
    수익률 점수 (max 25점).
    +3% 미만  →  0점 (데이트레이딩으로 정리)
    +3~5%    → 10점
    +5~7%    → 15점
    +7~10%   → 20점
    +10% 이상 → 25점
    """
    if entry_price <= 0:
        return 0, "수익률 계산 불가"
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct < 0.03:
        return 0, f"수익률 부족 ({pnl_pct:+.1%})"
    elif pnl_pct < 0.05:
        return 10, f"수익 +{pnl_pct:.1%}"
    elif pnl_pct < 0.07:
        return 15, f"수익 +{pnl_pct:.1%}"
    elif pnl_pct < 0.10:
        return 20, f"수익 +{pnl_pct:.1%}"
    else:
        return 25, f"강한 수익 +{pnl_pct:.1%}"


def _score_news(code: str) -> tuple:
    """
    뉴스/공시 센티먼트 점수 (max 25점).
    - 긍정 키워드 많으면 가산, 부정 키워드 있으면 감점
    - 뉴스 없으면 중립 (10점)
    """
    try:
        from config.settings import OVERNIGHT_NEWS_POSITIVE, OVERNIGHT_NEWS_NEGATIVE
    except ImportError:
        OVERNIGHT_NEWS_POSITIVE = []
        OVERNIGHT_NEWS_NEGATIVE = []

    try:
        from tools.news_tools import get_all_news
        news_list = get_all_news(code, days=1, max_per_source=10)
    except Exception:
        return 10, "뉴스 조회 실패 (중립)"

    if not news_list:
        return 10, "뉴스 없음 (중립)"

    positive_count = 0
    negative_count = 0
    for item in news_list:
        title = item.get("title", "")
        for kw in OVERNIGHT_NEWS_POSITIVE:
            if kw in title:
                positive_count += 1
                break
        for kw in OVERNIGHT_NEWS_NEGATIVE:
            if kw in title:
                negative_count += 1
                break

    # 점수 계산
    if negative_count >= 2:
        return 0, f"부정 뉴스 {negative_count}건 (위험)"
    elif negative_count == 1 and positive_count == 0:
        return 5, f"부정 뉴스 1건"
    elif positive_count == 0 and negative_count == 0:
        return 10, f"뉴스 {len(news_list)}건 (중립)"
    elif positive_count >= 3:
        return 25, f"긍정 뉴스 {positive_count}건 (강한 호재)"
    elif positive_count >= 1:
        score = min(10 + positive_count * 5 - negative_count * 5, 25)
        return max(score, 5), f"긍정 {positive_count}건 / 부정 {negative_count}건"
    else:
        return 10, "분류 불가 (중립)"


def _score_trend(df_ohlcv) -> tuple:
    """
    차트 추세 점수 (max 25점).
    - RSI: 50~70 구간이면 가산
    - 5일선 > 20일선 (골든크로스 상태)
    - 종가가 볼린저 상단 부근 또는 그 위
    - 양봉 마감 여부
    """
    if df_ohlcv is None or len(df_ohlcv) < 20:
        return 10, "차트 데이터 부족 (중립)"

    score = 0
    reasons = []

    # RSI 확인
    rsi = calc_rsi(df_ohlcv)
    if 55 <= rsi <= 70:
        score += 8
        reasons.append(f"RSI 양호({rsi:.0f})")
    elif 45 <= rsi < 55:
        score += 4
        reasons.append(f"RSI 중립({rsi:.0f})")
    elif rsi > 70:
        score += 2
        reasons.append(f"RSI 과열({rsi:.0f})")
    else:
        reasons.append(f"RSI 약세({rsi:.0f})")

    # 이동평균선: 5MA > 20MA (상승 추세)
    closes = df_ohlcv["close"].values.astype(float)
    ma5 = float(closes[-5:].mean())
    ma20 = float(closes[-20:].mean())
    if ma5 > ma20:
        score += 7
        reasons.append("5MA>20MA(상승)")
    else:
        score += 2
        reasons.append("5MA<20MA(하락)")

    # 볼린저 밴드 위치
    bb = calc_bollinger(df_ohlcv)
    last_close = float(closes[-1])
    if last_close >= bb["upper"]:
        score += 5
        reasons.append("볼린저 상단 돌파")
    elif last_close >= bb["mid"]:
        score += 3
        reasons.append("볼린저 중간 이상")
    else:
        reasons.append("볼린저 하단")

    # 양봉 마감
    last_open = float(df_ohlcv["open"].values[-1])
    if last_close > last_open:
        score += 5
        reasons.append("양봉 마감")
    else:
        score += 1
        reasons.append("음봉 마감")

    return min(score, 25), " | ".join(reasons)


def _score_volume(df_ohlcv) -> tuple:
    """
    거래량 건전성 점수 (max 25점).
    - 당일 거래량이 20일 평균 대비 높으면 가산
    - 거래량 증가 추세 (최근 3일 우상향)
    - 거래량 급감 시 감점 (세력 이탈 징후)
    """
    if df_ohlcv is None or len(df_ohlcv) < 20:
        return 10, "거래량 데이터 부족 (중립)"

    score = 0
    reasons = []
    volumes = df_ohlcv["volume"].values.astype(float)

    avg_vol_20 = float(volumes[-20:].mean())
    today_vol = float(volumes[-1])

    if avg_vol_20 <= 0:
        return 10, "평균거래량 0 (중립)"

    vol_ratio = today_vol / avg_vol_20

    # 거래량 비율
    if vol_ratio >= 3.0:
        score += 12
        reasons.append(f"거래량 폭증({vol_ratio:.1f}x)")
    elif vol_ratio >= 2.0:
        score += 10
        reasons.append(f"거래량 급증({vol_ratio:.1f}x)")
    elif vol_ratio >= 1.2:
        score += 7
        reasons.append(f"거래량 증가({vol_ratio:.1f}x)")
    elif vol_ratio >= 0.8:
        score += 4
        reasons.append(f"거래량 보통({vol_ratio:.1f}x)")
    else:
        score += 0
        reasons.append(f"거래량 급감({vol_ratio:.1f}x)")

    # 최근 3일 거래량 추세
    if len(volumes) >= 3:
        v3 = volumes[-3:]
        if v3[-1] > v3[-2] > v3[-3]:
            score += 8
            reasons.append("3일 연속 증가")
        elif v3[-1] > v3[-2]:
            score += 5
            reasons.append("2일 증가")
        elif v3[-1] < v3[-2] < v3[-3]:
            score += 0
            reasons.append("3일 연속 감소")
        else:
            score += 3
            reasons.append("거래량 혼조")

    # 프로그램 매수 징후 (상승 + 거래량 증가)
    closes = df_ohlcv["close"].values.astype(float)
    if len(closes) >= 2 and closes[-1] > closes[-2] and vol_ratio >= 1.5:
        score += 5
        reasons.append("가격↑+거래량↑")

    return min(score, 25), " | ".join(reasons)


def evaluate_overnight(code: str, entry_price: float, current_price: float,
                       df_ohlcv=None) -> dict:
    """
    오버나이트 홀딩 종합 평가.

    4가지 요소를 종합하여 100점 만점으로 채점:
      1) 수익률 (25점) — 당일 수익률 기반
      2) 뉴스/공시 (25점) — 키워드 센티먼트
      3) 차트 추세 (25점) — RSI, 이평선, 볼린저, 양봉
      4) 거래량 건전성 (25점) — 거래량 비율, 추세

    Parameters
    ----------
    code          : 종목코드
    entry_price   : 평균 매수 단가
    current_price : 현재가 (= 장 마감 부근 가격)
    df_ohlcv      : 최근 60일 OHLCV DataFrame (없으면 수익률+뉴스만 평가)

    Returns
    -------
    dict: {
        hold: bool,           # True면 오버나이트 홀딩
        score: int,           # 총점 (0~100)
        grade: str,           # A/B/C/D
        breakdown: dict,      # 항목별 {score, reason}
        closing_price: float, # 종가 (익일 손절 기준가)
        stop_loss: float,     # 익일 손절가 (종가 기준)
    }
    """
    try:
        from config.settings import OVERNIGHT_MIN_SCORE, OVERNIGHT_STOP_PCT
    except ImportError:
        OVERNIGHT_MIN_SCORE = 60
        OVERNIGHT_STOP_PCT = -0.03

    # 각 항목 채점
    profit_score, profit_reason = _score_profit(entry_price, current_price)
    news_score, news_reason = _score_news(code)
    trend_score, trend_reason = _score_trend(df_ohlcv)
    volume_score, volume_reason = _score_volume(df_ohlcv)

    total_score = profit_score + news_score + trend_score + volume_score

    # 등급 판정
    if total_score >= 85:
        grade = "A"
    elif total_score >= 70:
        grade = "B"
    elif total_score >= OVERNIGHT_MIN_SCORE:
        grade = "C"
    else:
        grade = "D"

    # 종가 결정 (df_ohlcv의 마지막 close 또는 current_price)
    if df_ohlcv is not None and len(df_ohlcv) > 0:
        closing_price = float(df_ohlcv["close"].values[-1])
    else:
        closing_price = current_price

    # 익일 손절가 = 종가 기준
    stop_loss = round(closing_price * (1 + OVERNIGHT_STOP_PCT), 0)

    hold = total_score >= OVERNIGHT_MIN_SCORE

    # 수익률이 마이너스면 무조건 비홀딩
    if entry_price > 0 and current_price < entry_price:
        hold = False

    return {
        "hold": hold,
        "score": total_score,
        "grade": grade,
        "breakdown": {
            "profit":  {"score": profit_score, "max": 25, "reason": profit_reason},
            "news":    {"score": news_score,   "max": 25, "reason": news_reason},
            "trend":   {"score": trend_score,  "max": 25, "reason": trend_reason},
            "volume":  {"score": volume_score, "max": 25, "reason": volume_reason},
        },
        "closing_price": closing_price,
        "stop_loss": stop_loss,
    }


def calc_overnight_stop(closing_price: float, stop_pct: float = None) -> float:
    """
    익일 손절가 계산 — 종가 기준.
    평단이 아니라 그날 종가에서 손절 라인을 잡는다.

    Parameters
    ----------
    closing_price : 오버나이트 결정 당일 종가
    stop_pct      : 손절 비율 (기본값: settings.OVERNIGHT_STOP_PCT)
    """
    if stop_pct is None:
        try:
            from config.settings import OVERNIGHT_STOP_PCT
            stop_pct = OVERNIGHT_STOP_PCT
        except ImportError:
            stop_pct = -0.03
    return round(closing_price * (1 + stop_pct), 0)


if __name__ == "__main__":
    import pandas as pd, numpy as np
    print("=" * 55)
    print("  QUANTUM FLOW - 스캐너 툴 테스트")
    print("=" * 55)
    np.random.seed(42)
    dates=pd.date_range(end=pd.Timestamp.today(), periods=60, freq="B")
    c=[70000]
    for _ in range(59): c.append(int(c[-1]*(1+np.random.normal(0.001,0.015))))
    df=pd.DataFrame({"open":[int(p*0.99) for p in c],"high":[int(p*1.02) for p in c],
        "low":[int(p*0.98) for p in c],"close":c,
        "volume":np.random.randint(500000,3000000,60)}, index=dates)
    atr=calc_atr(df); rsi=calc_rsi(df); bb=calc_bollinger(df); dc=calc_donchian(df)
    tp=int(df["high"].tail(20).max()*1.005)
    r=check_buy_signal("005930",tp,3500000,1000000,12.5,df)
    print(f"ATR={atr:,.0f}  RSI={rsi:.2f}  매수신호={r['signal']}  점수={r['score']}")
    print(f"손절가={calc_stop_loss(tp,atr):,.0f}  오버나이트={is_overnight_candidate(tp,int(tp*1.08))}")
    print("\n  Phase 4 scanner_tools.py - 구현 완료!")
    print("=" * 55)
