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
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
    except (ValueError, TypeError):
        return 1

    now = datetime.now()
    # 장 마감 시각(15:30) 기준으로 날짜 보정
    market_close_hour = 15
    market_close_min = 30

    # 진입일 기준일 결정
    if entry_dt.hour > market_close_hour or (entry_dt.hour == market_close_hour and entry_dt.minute >= market_close_min):
        entry_base = entry_dt.date()  # 진입일은 카운트 안 함
    else:
        entry_base = entry_dt.date()

    # 현재 기준일 결정
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
    return max(biz_days, 1)


def calc_pyramid_stop(avg_price):
    """추가매수 후 손절가: 평단 x (1 + PYRAMID_STOP_PCT)"""
    return round(avg_price * (1 + PYRAMID_STOP_PCT), 0)


def is_overnight_candidate(entry_price, current_price):
    """수익률 >= OVERNIGHT_THRESHOLD 이면 오버나이트 트랙으로 전환."""
    if entry_price <= 0:
        return False
    return (current_price - entry_price) / entry_price >= OVERNIGHT_THRESHOLD


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
