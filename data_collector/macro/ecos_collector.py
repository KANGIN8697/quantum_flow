# data_collector/macro/ecos_collector.py — 한국은행 ECOS API 매크로 지표 수집
#
# ECOS API 신청: https://ecos.bok.or.kr/api/
# 월 1회 자동 업데이트 스케줄
# 최대 제공 기간 전체 수집

import os
import sys
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger("ecos_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, get_latest_date, log_collection
from dotenv import load_dotenv
load_dotenv()

ECOS_API_KEY = os.getenv("ECOS_API_KEY", "")
ECOS_BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"

# ── 수집 대상 지표 정의 ───────────────────────────────────

ECOS_INDICATORS = [
    {
        "stat_code": "722Y001",
        "item_code1": "0101000",
        "name": "기준금리",
        "cycle": "M",       # M=월, Q=분기, A=연
        "unit": "%",
        "start": "200001",  # 수집 시작 (YYYYMM)
    },
    {
        "stat_code": "036Y001",
        "item_code1": "1400",
        "name": "GDP성장률",
        "cycle": "Q",
        "unit": "%",
        "start": "200001",
    },
    {
        "stat_code": "021Y201",
        "item_code1": "0",
        "name": "소비자물가지수(CPI)",
        "cycle": "M",
        "unit": "2020=100",
        "start": "200001",
    },
    {
        "stat_code": "060Y002",
        "item_code1": "0000",
        "name": "수출액(총계)",
        "cycle": "M",
        "unit": "백만달러",
        "start": "200001",
    },
    {
        "stat_code": "052Y001",
        "item_code1": "0101000",
        "name": "M2통화량",
        "cycle": "M",
        "unit": "십억원",
        "start": "200001",
    },
    {
        "stat_code": "008Y007",
        "item_code1": "0",
        "name": "실업률",
        "cycle": "M",
        "unit": "%",
        "start": "200001",
    },
    {
        "stat_code": "020Y001",
        "item_code1": "0",
        "name": "경상수지",
        "cycle": "M",
        "unit": "백만달러",
        "start": "200001",
    },
]


# ── ECOS API 호출 ─────────────────────────────────────────

def _fetch_ecos_series(stat_code: str, item_code1: str,
                       cycle: str, start: str, end: str = None) -> list:
    """
    ECOS REST API 단일 시계열 조회.

    Parameters
    ----------
    stat_code  : 통계표 코드
    item_code1 : 통계 항목 코드
    cycle      : M(월) / Q(분기) / A(연)
    start      : 시작기간 (YYYYMM 또는 YYYYQ)
    end        : 종료기간 (기본: 현재)

    Returns
    -------
    list of dict: [{date, value}, ...]
    """
    if not ECOS_API_KEY:
        logger.error("ECOS_API_KEY 미설정")
        return []

    if end is None:
        end = datetime.now().strftime("%Y%m")

    # URL: /서비스명/인증키/요청타입/언어/시작/종료/통계코드/주기/항목코드
    url = (
        f"{ECOS_BASE_URL}/{ECOS_API_KEY}/json/kr/1/10000/"
        f"{stat_code}/{cycle}/{start}/{end}/{item_code1}"
    )

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        result_block = data.get("StatisticSearch", {})
        if "row" not in result_block:
            msg = result_block.get("RESULT", {}).get("MESSAGE", "알 수 없는 오류")
            logger.warning(f"ECOS 응답 오류: {msg}")
            return []

        items = []
        for row in result_block["row"]:
            time_str = row.get("TIME", "")
            value_str = row.get("DATA_VALUE", "")

            # 날짜 정규화: "202301" → "2023-01-01", "2023Q1" → "2023-03-01"
            date_iso = _normalize_ecos_date(time_str, cycle)
            if not date_iso:
                continue

            try:
                value = float(value_str) if value_str else None
            except (ValueError, TypeError):
                value = None

            items.append({"date": date_iso, "value": value})

        return items

    except requests.exceptions.RequestException as e:
        logger.error(f"ECOS API 호출 실패: {e}")
        return []


def _normalize_ecos_date(time_str: str, cycle: str) -> str:
    """ECOS 날짜 문자열을 ISO 형식으로 변환."""
    try:
        if cycle == "M" and len(time_str) >= 6:
            return f"{time_str[:4]}-{time_str[4:6]}-01"
        elif cycle == "Q" and "Q" in time_str:
            year = time_str[:4]
            quarter = int(time_str[-1])
            month = quarter * 3
            return f"{year}-{month:02d}-01"
        elif cycle == "A" and len(time_str) == 4:
            return f"{time_str}-12-01"
        else:
            return f"{time_str[:4]}-{time_str[4:6]}-01" if len(time_str) >= 6 else ""
    except (ValueError, IndexError):
        return ""


# ── 전체 수집 ─────────────────────────────────────────────

def collect_all(incremental: bool = True):
    """전체 ECOS 매크로 지표 수집."""
    if not ECOS_API_KEY:
        logger.error("ECOS_API_KEY 미설정 — .env 확인")
        return 0

    init_db()
    total_rows = 0

    logger.info(f"ECOS 매크로 지표 수집 시작 ({len(ECOS_INDICATORS)}개 지표)")

    for ind in ECOS_INDICATORS:
        start = ind["start"]
        if incremental:
            latest = get_latest_date("kr_macro")
            if latest and latest > "2000-01-01":
                # latest "2025-12-01" → "202512"
                start = latest.replace("-", "")[:6]

        items = _fetch_ecos_series(
            stat_code=ind["stat_code"],
            item_code1=ind["item_code1"],
            cycle=ind["cycle"],
            start=start,
        )

        if items:
            rows = []
            for item in items:
                rows.append({
                    "date": item["date"],
                    "indicator_code": ind["stat_code"],
                    "indicator_name": ind["name"],
                    "value": item["value"],
                    "unit": ind["unit"],
                })
            inserted = upsert_rows("kr_macro", rows)
            total_rows += inserted
            logger.info(f"  {ind['name']}: {inserted}건")
            log_collection("ecos", ind["name"], rows_added=inserted)

        time.sleep(0.5)  # ECOS 부하 방지

    logger.info(f"ECOS 수집 완료: {total_rows}건")
    return total_rows


def run_monthly():
    """월간 자동 업데이트 (매월 1일 08:00)."""
    logger.info("월간 ECOS 매크로 업데이트")
    return collect_all(incremental=True)


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — ECOS 매크로 지표 수집")
    print("=" * 55)
    result = collect_all(incremental=False)
    print(f"\n  총 {result}건 수집 완료")
