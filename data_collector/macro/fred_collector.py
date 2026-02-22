# data_collector/macro/fred_collector.py — FRED API 미국 매크로 지표 수집
#
# FRED API 신청: https://fred.stlouisfed.org/docs/api/api_key.html
# 분당 120회 제한 → sleep 0.5초 이상
# 월 1회 자동 업데이트

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger("fred_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, get_latest_date, log_collection
from dotenv import load_dotenv
load_dotenv()

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# ── 수집 대상 지표 정의 ───────────────────────────────────

FRED_SERIES = [
    {"series_id": "FEDFUNDS",  "name": "연준 기준금리",              "unit": "%"},
    {"series_id": "CPIAUCSL",  "name": "미국 CPI",                  "unit": "index"},
    {"series_id": "PCEPI",     "name": "PCE 물가",                  "unit": "index"},
    {"series_id": "UNRATE",    "name": "실업률",                     "unit": "%"},
    {"series_id": "PAYEMS",    "name": "비농업고용(NFP)",            "unit": "천명"},
    {"series_id": "GDP",       "name": "GDP 성장률",                 "unit": "십억달러"},
    {"series_id": "T10Y2Y",    "name": "장단기금리스프레드",          "unit": "%"},
    {"series_id": "WALCL",     "name": "연준 대차대조표",             "unit": "백만달러"},
    {"series_id": "MANEMP",    "name": "ISM 제조업(대용)",           "unit": "천명"},
    {"series_id": "VIXCLS",    "name": "VIX 일별",                  "unit": "index"},
    {"series_id": "DTWEXBGS",  "name": "달러인덱스(Board)",          "unit": "index"},
    {"series_id": "DEXKOUS",   "name": "원달러 환율",                "unit": "KRW/USD"},
    {"series_id": "DGS10",     "name": "미국 10년 국채 수익률",       "unit": "%"},
    {"series_id": "DGS2",      "name": "미국 2년 국채 수익률",        "unit": "%"},
]


# ── FRED API 호출 ─────────────────────────────────────────

def _fetch_fred_series(series_id: str, start_date: str = "2000-01-01",
                       end_date: str = None) -> list:
    """
    FRED REST API 시계열 데이터 조회.

    Returns
    -------
    list of dict: [{date, value}, ...]
    """
    if not FRED_API_KEY:
        logger.error("FRED_API_KEY 미설정")
        return []

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "asc",
    }

    try:
        resp = requests.get(FRED_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        observations = data.get("observations", [])
        items = []
        for obs in observations:
            date_str = obs.get("date", "")
            value_str = obs.get("value", "")
            if value_str == "." or not value_str:
                continue  # FRED의 결측값 표시
            try:
                value = float(value_str)
            except (ValueError, TypeError):
                continue
            items.append({"date": date_str, "value": value})

        return items

    except requests.exceptions.RequestException as e:
        logger.error(f"FRED API 호출 실패 ({series_id}): {e}")
        return []


# ── 전체 수집 ─────────────────────────────────────────────

def collect_all(incremental: bool = True):
    """전체 FRED 매크로 지표 수집."""
    if not FRED_API_KEY:
        logger.error("FRED_API_KEY 미설정 — .env 확인")
        return 0

    init_db()
    total_rows = 0

    logger.info(f"FRED 매크로 지표 수집 시작 ({len(FRED_SERIES)}개 시리즈)")

    for series in FRED_SERIES:
        start_date = "2000-01-01"
        if incremental:
            latest = get_latest_date("us_macro")
            if latest and latest > "2000-01-01":
                start_date = latest

        items = _fetch_fred_series(series["series_id"], start_date=start_date)

        if items:
            rows = []
            for item in items:
                rows.append({
                    "date": item["date"],
                    "series_id": series["series_id"],
                    "series_name": series["name"],
                    "value": item["value"],
                    "unit": series["unit"],
                })
            inserted = upsert_rows("us_macro", rows)
            total_rows += inserted
            logger.info(f"  {series['name']} ({series['series_id']}): {inserted}건")
            log_collection("fred", series["series_id"], rows_added=inserted)

        time.sleep(0.6)  # 분당 120회 제한 준수

    logger.info(f"FRED 수집 완료: {total_rows}건")
    return total_rows


def run_monthly():
    """월간 자동 업데이트 (매월 1일 08:00)."""
    logger.info("월간 FRED 매크로 업데이트")
    return collect_all(incremental=True)


# ── 특정 시리즈 빠른 조회 (Agent 1 호출용) ────────────────

def get_latest_value(series_id: str) -> dict:
    """특정 FRED 시리즈의 최신 값을 반환 (DB 먼저, 없으면 API)."""
    from database.db_manager import query
    result = query(
        "SELECT date, value FROM us_macro WHERE series_id=? ORDER BY date DESC LIMIT 1",
        (series_id,)
    )
    if result:
        return {"series_id": series_id, "date": result[0]["date"], "value": result[0]["value"]}

    # DB에 없으면 API 직접 호출
    items = _fetch_fred_series(series_id, start_date="2024-01-01")
    if items:
        return {"series_id": series_id, "date": items[-1]["date"], "value": items[-1]["value"]}
    return {"series_id": series_id, "date": "", "value": None}


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — FRED 매크로 지표 수집")
    print("=" * 55)
    result = collect_all(incremental=False)
    print(f"\n  총 {result}건 수집 완료")
