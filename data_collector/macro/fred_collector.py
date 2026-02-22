# data_collector/macro/fred_collector.py — 미국 FRED 데이터 수집
# 연방준비제도 경제 데이터 수집

import os
import time
import logging
import requests
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("fred_collector")

# ── 환경변수 ───────────────────────────────────────────────────
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# ── FRED 시리즈 ID ─────────────────────────────────────────────
FRED_SERIES_DAILY = {
    "VIX": "VIXCLS",         # VIX 공포지수
    "DXY": "DTWEXBGS",       # 달러 인덱스
    "TNX": "DGS10",          # 미국 10년 국채 금리
    "SP500": "SP500",        # S&P 500
    "USDKRW": "DEXKOUS",     # 달러/원 환율
    "FEDFUNDS": "FEDFUNDS",  # 연방기금금리
    "T10Y2Y": "T10Y2Y",      # 10년-2년 금리차
    "T10YIE": "T10YIE",      # 10년 기대인플레이션
}

FRED_SERIES_MONTHLY = {
    "CPI": "CPIAUCSL",       # 소비자물가지수
    "CORE_CPI": "CPILFESL",  # 근원 CPI
    "PCE": "PCEPI",          # PCE 물가지수
    "UNRATE": "UNRATE",      # 실업률
    "UMCSENT": "UMCSENT",    # 소비자심리지수
}

FRED_SERIES_WEEKLY = {
    "M2": "WM2NS",           # M2 통화량
    "FED_ASSETS": "WALCL",   # 연준 총자산
    "ICSA": "ICSA",          # 신규 실업수당
}

# 캐시
_cache = {}
CACHE_TTL = 1800  # 30분


def fetch_fred_series(series_id: str, days_back: int = 10) -> dict:
    """FRED에서 특정 시리즈의 최근 데이터 조회"""
    if not FRED_API_KEY:
        return {"error": "FRED_API_KEY 미설정"}

    cache_key = f"fred_{series_id}_{days_back}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    end = date.today()
    start = end - timedelta(days=days_back)
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}"
        f"&api_key={FRED_API_KEY}"
        f"&file_type=json"
        f"&sort_order=desc"
        f"&limit=5"
        f"&observation_start={start.isoformat()}"
    )

    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        obs = data.get("observations", [])

        # 유효한 값 찾기 (. 은 미발표)
        for o in obs:
            if o["value"] != ".":
                result = {
                    "series_id": series_id,
                    "value": float(o["value"]),
                    "date": o["date"],
                    "timestamp": datetime.now().isoformat(),
                }
                _cache[cache_key] = {"data": result, "ts": time.time()}
                return result

        return {"error": "데이터 없음"}

    except Exception as e:
        logger.error(f"FRED {series_id} 조회 실패: {e}")
        return {"error": str(e)}


def fetch_all_fred_daily() -> dict:
    """모든 일간 FRED 지표 조회"""
    results = {}
    for name, sid in FRED_SERIES_DAILY.items():
        results[name] = fetch_fred_series(sid, days_back=10)
    return results


def fetch_all_fred_monthly() -> dict:
    """모든 월간 FRED 지표 조회"""
    results = {}
    for name, sid in FRED_SERIES_MONTHLY.items():
        results[name] = fetch_fred_series(sid, days_back=90)
    return results


def fetch_all_fred_weekly() -> dict:
    """모든 주간 FRED 지표 조회"""
    results = {}
    for name, sid in FRED_SERIES_WEEKLY.items():
        results[name] = fetch_fred_series(sid, days_back=30)
    return results


def fetch_fred_time_series(series_id: str, days_back: int = 365) -> list:
    """특정 시리즈의 시계열 데이터 조회"""
    if not FRED_API_KEY:
        return []

    end = date.today()
    start = end - timedelta(days=days_back)
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}"
        f"&api_key={FRED_API_KEY}"
        f"&file_type=json"
        f"&sort_order=asc"
        f"&observation_start={start.isoformat()}"
        f"&observation_end={end.isoformat()}"
    )

    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        obs = data.get("observations", [])

        time_series = []
        for o in obs:
            if o["value"] != ".":
                time_series.append({
                    "date": o["date"],
                    "value": float(o["value"]),
                })

        return time_series

    except Exception as e:
        logger.error(f"FRED 시계열 {series_id} 조회 실패: {e}")
        return []