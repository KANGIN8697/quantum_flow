# tools/scanner_tools.py — 종목 스캐닝 & 전략 계산 엔진
# Phase 4 구현: 기술지표 계산, 매수/추가매수 신호 판단, 손절 계산

import math
from datetime import datetime

try:
    from config.settings import (
        DONCHIAN_PERIOD, VOLUME_SURGE_RATIO, TICK_SPEED_MIN,
        RSI_LOWER, RSI_UPPER, ATR_PERIOD,
        INITIAL_STOP_ATR, TRAILING_STOP_ATR, PYRAMID_STOP_PCT,
        OVERNIGHT_THRESHOLD, PYRAMID_PRICE_TRIGGER, PYRAMID_VOLUME_RATIO,
        PYRAMID_TICK_RATIO, PYRAMID_VOLUME_TREND_MIN,
    )
except ImportError:
    DONCHIAN_PERIOD=20; VOLUME_SURGE_RATIO=2.0; TICK_SPEED_MIN=5
    RSI_LOWER=50; RSI_UPPER=70; ATR_PERIOD=14
    INITIAL_STOP_ATR=2.0; TRAILING_STOP_ATR=3.0; PYRAMID_STOP_PCT=-0.03
    OVERNIGHT_THRESHOLD=0.07; PYRAMID_PRICE_TRIGGER=0.05
    PYRAMID_VOLUME_RATIO=0.50; PYRAMID_TICK_RATIO=0.40; PYRAMID_VOLUME_TREND_MIN=5


def calc_atr(df, period=ATR_PERIOD):
    """Average True Range (ATR) 계산 (Wilder 방식)."""
    if len(df) < period + 1: return 0.0
    tr_list = []
    for i in range(1, len(df)):
        h=df['high'].iloc[i]; l=df['low'].iloc[i]; c=df['close'].iloc[i-1]
        tr_list.append(max(h-l, abs(h-c), abs(l-c)))
    if len(tr_list) < period:
        return float(sum(tr_list)/len(tr_list)) if tr_list else 0.0
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr*(period-1)+tr)/period
    return round(atr, 2)


def calc_rsi(df, period=14):
    """Relative Strength Index (RSI) 계산."""
    closes = list(df['close'])
    if len(closes) < period+1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d=closes[i]-closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    if len(gains) < period: return 50.0
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    for i in range(period, len(gains)):
        ag=(ag*(period-1)+gains[i])/period; al=(al*(period-1)+losses[i])/period
    if al==0: return 100.0
    return round(100-(100/(1+ag/al)), 2)


def calc_bollinger(df, period=20):
    """Bollinger Bands 계산. 반환: {upper, mid, lower}"""
    closes=list(df['close'])
    if len(closes)<period:
        last=closes[-1] if closes else 0
        return {"upper":last,"mid":last,"lower":last}
    recent=closes[-period:]; mid=sum(recent)/period
    std=(sum((x-mid)**2 for x in recent)/period)**0.5
    return {"upper":round(mid+2*std,2),"mid":round(mid,2),"lower":round(mid-2*std,2)}


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
    df_recent, pyramiding_done=False, no_pyramid_after="15:00"):
    """추가매수(피라미딩) 조건 5가지 모두 충족 시 True 반환."""
    from datetime import datetime
    if pyramiding_done: return False
    if datetime.now().strftime("%H:%M")>=no_pyramid_after: return False
    if current_price<avg_price*(1+PYRAMID_PRICE_TRIGGER): return False
    if entry_volume>0 and current_volume<entry_volume*PYRAMID_VOLUME_RATIO: return False
    if entry_tick_speed>0 and current_tick_speed<entry_tick_speed*PYRAMID_TICK_RATIO: return False
    return True


def calc_stop_loss(entry_price, atr):
    """초기 손절가: 진입가 - ATR x INITIAL_STOP_ATR"""
    return round(entry_price - atr*INITIAL_STOP_ATR, 0)

def calc_trailing_stop(high_price, atr):
    """트레일링 손절가: 최고가 - ATR x TRAILING_STOP_ATR"""
    return round(high_price - atr*TRAILING_STOP_ATR, 0)

def calc_pyramid_stop(avg_price):
    """추가매수 후 손절가: 평단 x (1 + PYRAMID_STOP_PCT)"""
    return round(avg_price*(1+PYRAMID_STOP_PCT), 0)

def is_overnight_candidate(entry_price, current_price):
    """수익률 >= OVERNIGHT_THRESHOLD 이면 오버나이트 트랙으로 전환."""
    if entry_price<=0: return False
    return (current_price-entry_price)/entry_price >= OVERNIGHT_THRESHOLD


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
