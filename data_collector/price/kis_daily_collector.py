# data_collector/price/kis_daily_collector.py — KIS API 일봉 데이터 수집
# 한국투자증권 API로 국내 주식 일봉 데이터 수집

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("kis_daily_collector")

# ── 환경변수 ───────────────────────────────────────────────────
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCESS_TOKEN = os.getenv("KIS_ACCESS_TOKEN", "")

# KIS API 기본 설정
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_URL = f"{BASE_URL}/oauth2/tokenP"

# 캐시
_cache = {}
CACHE_TTL = 1800  # 30분


def get_access_token() -> str:
    """KIS API 액세스 토큰 획득"""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return ""

    cache_key = "kis_token"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        }
        resp = requests.post(TOKEN_URL, headers=headers, json=body, timeout=10)
        data = resp.json()

        if resp.status_code == 200 and "access_token" in data:
            token = data["access_token"]
            _cache[cache_key] = {"data": token, "ts": time.time()}
            return token
        else:
            logger.error(f"토큰 획득 실패: {data}")
            return ""
    except Exception as e:
        logger.error(f"토큰 획득 오류: {e}")
        return ""


def get_headers() -> dict:
    """KIS API 요청 헤더"""
    token = get_access_token()
    if not token:
        return {}

    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010400",  # 국내주식 시세 조회
    }


def fetch_daily_price(code: str, days_back: int = 30) -> list:
    """특정 종목의 일봉 데이터 조회"""
    if not KIS_APP_KEY:
        return []

    cache_key = f"kis_daily_{code}_{days_back}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    headers = get_headers()
    if not headers:
        return []

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",  # 주식
        "FID_INPUT_ISCD": code,
        "FID_PERIOD_DIV_CODE": "D",  # 일봉
        "FID_ORG_ADJ_PRC": "0",  # 수정주가 미적용
    }

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()

        if resp.status_code != 200 or data.get("rt_cd") != "0":
            logger.error(f"KIS 일봉 조회 실패: {data}")
            return []

        output = data.get("output", [])
        daily_data = []

        for item in output:
            try:
                daily_data.append({
                    "date": item["stck_bsop_date"],
                    "open": int(item["stck_oprc"]),
                    "high": int(item["stck_hgpr"]),
                    "low": int(item["stck_lwpr"]),
                    "close": int(item["stck_clpr"]),
                    "volume": int(item["acml_vol"]),
                    "change": int(item["prdy_vrss"]),
                    "change_pct": float(item["prdy_ctrt"]),
                })
            except (KeyError, ValueError) as e:
                logger.warning(f"일봉 데이터 파싱 오류: {e}")
                continue

        # 날짜순 정렬 (오래된 순)
        daily_data.sort(key=lambda x: x["date"])

        _cache[cache_key] = {"data": daily_data, "ts": time.time()}
        return daily_data

    except Exception as e:
        logger.error(f"{code} 일봉 조회 오류: {e}")
        return []


def fetch_multiple_daily_prices(codes: list, days_back: int = 30) -> dict:
    """여러 종목의 일봉 데이터를 병렬 조회"""
    results = {}
    for code in codes:
        results[code] = fetch_daily_price(code, days_back)
    return results


def fetch_kospi_daily(days_back: int = 30) -> list:
    """코스피 지수 일봉 데이터 조회"""
    return fetch_daily_price("000001", days_back)  # 코스피 코드