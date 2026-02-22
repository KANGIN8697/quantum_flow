# data_collector/text/dart_collector.py — DART 공시 수집 (최근 5년)
#
# DART API 신청: https://opendart.fss.or.kr/
# 주 1회 자동 업데이트
# 주요 20종목은 전문 저장, 나머지는 제목+URL만

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger("dart_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, insert_rows_ignore, get_latest_date, log_collection
from dotenv import load_dotenv
load_dotenv()

DART_API_KEY = os.getenv("DART_API_KEY", "")
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# ── 수집 대상 공시 유형 ───────────────────────────────────

TARGET_REPORT_TYPES = {
    "A001": "유상증자결정",
    "A002": "무상증자결정",
    "A003": "감자결정",
    "B001": "분기보고서",
    "B002": "반기보고서",
    "B003": "사업보고서",
    "C001": "주요사항보고서",
    "D001": "지분공시",
}

# 전문 저장 대상 (주요 20종목)
FULL_TEXT_TICKERS = [
    "005930", "000660", "005380", "000270", "006400",
    "051910", "035420", "035720", "005490", "068270",
    "207940", "003670", "373220", "247540", "086520",
    "017670", "055550", "105560", "028260", "012330",
]


# ── DART 공시 목록 조회 ───────────────────────────────────

def fetch_disclosures(stock_code: str = None, start_date: str = None,
                      end_date: str = None, page_count: int = 100) -> list:
    """
    DART OpenAPI /list 엔드포인트로 공시 목록 수집.

    Parameters
    ----------
    stock_code : 종목코드 (None이면 전체)
    start_date : 시작일 (YYYYMMDD)
    end_date   : 종료일 (YYYYMMDD)
    page_count : 페이지당 건수 (max 100)

    Returns
    -------
    list of dict
    """
    if not DART_API_KEY:
        logger.error("DART_API_KEY 미설정")
        return []

    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    all_items = []
    page_no = 1

    while True:
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de": start_date,
            "end_de": end_date,
            "sort": "date",
            "sort_mth": "desc",
            "page_no": str(page_no),
            "page_count": str(page_count),
        }
        if stock_code:
            params["stock_code"] = stock_code

        try:
            resp = requests.get(DART_LIST_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "000":
                msg = data.get("message", "")
                if "조회된 데이터가 없습니다" in msg:
                    break
                logger.warning(f"DART API: {data.get('status')} — {msg}")
                break

            items = data.get("list", [])
            if not items:
                break

            for item in items:
                report_nm = item.get("report_nm", "")
                # 공시 유형 필터
                pblntf_ty = item.get("pblntf_ty", "")
                if pblntf_ty and pblntf_ty not in TARGET_REPORT_TYPES:
                    continue

                rcept_no = item.get("rcept_no", "")
                rcept_dt = item.get("rcept_dt", "")
                date_iso = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}" if len(rcept_dt) == 8 else rcept_dt

                ticker = item.get("stock_code", "")
                all_items.append({
                    "date": date_iso,
                    "ticker": ticker,
                    "corp_name": item.get("corp_name", ""),
                    "report_type": pblntf_ty,
                    "title": report_nm,
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                    "summary": None,
                    "rcept_no": rcept_no,
                })

            # 다음 페이지
            total_count = int(data.get("total_count", 0))
            total_pages = int(data.get("total_page", 1))
            if page_no >= total_pages:
                break
            page_no += 1
            time.sleep(0.3)

        except requests.exceptions.RequestException as e:
            logger.error(f"DART API 호출 실패: {e}")
            break

    return all_items


# ── 전체 수집 (최근 5년) ──────────────────────────────────

def collect_all(years: int = 5):
    """최근 N년치 DART 공시 수집."""
    if not DART_API_KEY:
        logger.error("DART_API_KEY 미설정 — .env 확인")
        return 0

    init_db()
    total_rows = 0
    start_time = time.time()

    end_date = datetime.now()
    start_date = end_date - timedelta(days=years * 365)

    # 3개월 단위로 분할 조회 (DART 응답 제한 우회)
    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=90), end_date)

        s_str = current_start.strftime("%Y%m%d")
        e_str = current_end.strftime("%Y%m%d")

        logger.info(f"  DART 조회: {s_str} ~ {e_str}")
        items = fetch_disclosures(start_date=s_str, end_date=e_str)

        if items:
            inserted = insert_rows_ignore("dart_disclosures", items)
            total_rows += inserted
            log_collection("dart", f"disclosures_{s_str}", rows_added=inserted)

        current_start = current_end + timedelta(days=1)
        time.sleep(1)  # DART 부하 방지

    elapsed = time.time() - start_time
    logger.info(f"DART 공시 수집 완료: {total_rows}건 / {elapsed:.0f}초")
    return total_rows


def run_weekly():
    """주간 업데이트 (매주 일요일 09:00)."""
    logger.info("주간 DART 공시 업데이트")
    items = fetch_disclosures(
        start_date=(datetime.now() - timedelta(days=8)).strftime("%Y%m%d")
    )
    if items:
        init_db()
        inserted = insert_rows_ignore("dart_disclosures", items)
        log_collection("dart", "weekly_update", rows_added=inserted)
        logger.info(f"DART 주간 업데이트: {inserted}건")
        return inserted
    return 0


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — DART 공시 수집")
    print("=" * 55)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5, help="수집 기간 (년)")
    parser.add_argument("--ticker", type=str, help="특정 종목만")
    args = parser.parse_args()

    if args.ticker:
        items = fetch_disclosures(stock_code=args.ticker,
                                  start_date=(datetime.now() - timedelta(days=365*args.years)).strftime("%Y%m%d"))
        print(f"  {len(items)}건 조회")
        if items:
            init_db()
            inserted = insert_rows_ignore("dart_disclosures", items)
            print(f"  {inserted}건 저장")
    else:
        collect_all(years=args.years)
