# data_collector/price/global_collector.py — 글로벌 가격 데이터 수집 (yfinance)
# 주요 글로벌 지수/상품 가격 수집

import os
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

def safe_float(val, default=0.0):
    """pandas Series/numpy -> float safely"""
    try:
        if hasattr(val, 'iloc'):
            val = val.iloc[-1]
        if hasattr(val, 'item'):
            return safe_float(val.item())
        return safe_float(val)
    except (TypeError, ValueError, IndexError):
        return default


load_dotenv()

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("global_collector")

# ── 환경변수 ───────────────────────────────────────────────────
# yfinance는 API 키 필요 없음

# ── 글로벌 심볼 매핑 ───────────────────────────────────────────
GLOBAL_SYMBOLS = {
    "SP500": "^GSPC",      # S&P 500
    "NASDAQ": "^IXIC",     # NASDAQ
    "DOW": "^DJI",         # Dow Jones
    "VIX": "^VIX",         # VIX 공포지수
    "DXY": "DX-Y.NYB",     # 달러 인덱스
    "TNX": "^TNX",         # 미국 10년 국채 금리
    "WTI": "CL=F",         # WTI 원유
    "GOLD": "GC=F",        # 금
    "SILVER": "SI=F",      # 은
    "COPPER": "HG=F",      # 구리
    "EURUSD": "EURUSD=X",  # EUR/USD
    "GBPUSD": "GBPUSD=X",  # GBP/USD
    "JPYUSD": "JPYUSD=X",  # JPY/USD
    "BTC": "BTC-USD",      # 비트코인
    "ETH": "ETH-USD",      # 이더리움
}

# 캐시
_cache = {}
CACHE_TTL = 600  # 10분


def fetch_global_price(symbol_key: str, period: str = "1d") -> dict:
    """특정 글로벌 심볼의 가격 데이터 조회"""
    if yf is None:
        return {"error": "yfinance 라이브러리 미설치"}

    symbol = GLOBAL_SYMBOLS.get(symbol_key)
    if not symbol:
        return {"error": f"알 수 없는 심볼: {symbol_key}"}

    cache_key = f"global_{symbol_key}_{period}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1d")

        if df.empty:
            return {"error": "데이터 없음"}

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        close = safe_float(latest["Close"])
        prev_close = safe_float(prev["Close"])
        change = close - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        result = {
            "symbol": symbol_key,
            "price": round(close, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(latest["Volume"]) if "Volume" in latest else 0,
            "date": str(df.index[-1].date()),
            "timestamp": datetime.now().isoformat(),
        }

        _cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    except Exception as e:
        logger.error(f"{symbol_key} 가격 조회 실패: {e}")
        return {"error": str(e)}


def fetch_all_global_prices() -> dict:
    """모든 글로벌 가격 데이터를 한번에 조회"""
    results = {}
    for key in GLOBAL_SYMBOLS.keys():
        results[key] = fetch_global_price(key)
    return results


def fetch_historical_global(symbol_key: str, days: int = 30) -> list:
    """특정 심볼의 과거 데이터 조회"""
    if yf is None:
        return []

    symbol = GLOBAL_SYMBOLS.get(symbol_key)
    if not symbol:
        return []

    try:
        ticker = yf.Ticker(symbol)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        df = ticker.history(start=start_date, end=end_date)

        if df.empty:
            return []

        data = []
        for idx, row in df.iterrows():
            data.append({
                "date": str(idx.date()),
                "open": safe_float(row["Open"]),
                "high": safe_float(row["High"]),
                "low": safe_float(row["Low"]),
                "close": safe_float(row["Close"]),
                "volume": int(row["Volume"]) if "Volume" in row else 0,
            })

        return data

    except Exception as e:
        logger.error(f"{symbol_key} 과거 데이터 조회 실패: {e}")
        return []