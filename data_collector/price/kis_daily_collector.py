# data_collector/price/kis_daily_collector.py — KIS API 국내 일봉 수집
#
# 실행 환경: Python 3.10+ (64bit OK)
# KIS API inquire-daily-itemchartprice 사용
# 수정주가 기준 (FID_ORG_ADJ_PRC = 0)
# 상장 이후 전체 기간 — 페이지네이션 연속 조회 구현

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger("kis_daily_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, get_latest_date, log_collection

# ── 환경변수 로드 ─────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

# ── HTTP 세션 (TCP 재사용) ────────────────────────────────
_session = requests.Session()
_session.headers.update({
    "Content-Type": "application/json; charset=utf-8",
})


# ── 토큰 관리 ────────────────────────────────────────────

_access_token = ""
_token_expires = datetime.min


def _ensure_token():
    """KIS API 접근 토큰 발급/갱신."""
    global _access_token, _token_expires

    if _access_token and datetime.now() < _token_expires:
        return _access_token

    try:
        from tools.token_manager import ensure_token
        _access_token = ensure_token()
        _token_expires = datetime.now() + timedelta(hours=20)
        return _access_token
    except ImportError:
        pass

    # 직접 발급 폴백
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
    }
    resp = _session.post(url, json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    _token_expires = datetime.now() + timedelta(hours=20)
    logger.info("KIS 토큰 발급 완료")
    return _access_token


# ── KOSPI200 + KOSDAQ150 종목 리스트 ──────────────────────

def get_target_tickers() -> list:
    """수집 대상 종목 리스트 반환."""
    # 폴백 리스트 (실제 운영 시 Creon 또는 외부 소스에서 가져오기)
    from data_collector.price.creon_collector import _get_fallback_tickers
    return [t["ticker"] for t in _get_fallback_tickers()]


# ── 일봉 수집 (핵심) ──────────────────────────────────────

def fetch_daily_bars(ticker: str, start_date: str = "19900101",
                     end_date: str = None) -> list:
    """
    KIS API inquire-daily-itemchartprice로 일봉 데이터 수집.
    연속 조회(페이지네이션) 지원.

    Parameters
    ----------
    ticker     : 종목코드 ("005930")
    start_date : 시작일 (YYYYMMDD)
    end_date   : 종료일 (기본: 오늘)
    """
    token = _ensure_token()
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST03010100",
    }

    all_rows = []
    current_end = end_date

    while True:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": current_end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",  # 수정주가
        }

        try:
            resp = _session.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("rt_cd") != "0":
                msg = data.get("msg1", "")
                logger.warning(f"  {ticker}: API 오류 — {msg}")
                break

            items = data.get("output2", [])
            if not items:
                break

            for item in items:
                date_str = item.get("stck_bsop_date", "")
                if not date_str or len(date_str) != 8:
                    continue

                close_val = int(item.get("stck_clpr", "0"))
                if close_val <= 0:
                    continue

                date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                all_rows.append({
                    "date": date_iso,
                    "ticker": ticker,
                    "open": int(item.get("stck_oprc", "0")),
                    "high": int(item.get("stck_hgpr", "0")),
                    "low": int(item.get("stck_lwpr", "0")),
                    "close": close_val,
                    "volume": int(item.get("acml_vol", "0")),
                    "adj_close": close_val,
                })

            # 연속 조회: 마지막 항목의 날짜 -1일을 다음 조회 종료일로
            last_date = items[-1].get("stck_bsop_date", "")
            if not last_date or last_date <= start_date:
                break

            # 다음 페이지 종료일 설정
            try:
                next_end = (datetime.strptime(last_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            except ValueError:
                break

            if next_end <= start_date or next_end >= current_end:
                break
            current_end = next_end

            time.sleep(0.1)  # KIS API 호출 제한

        except requests.exceptions.RequestException as e:
            logger.error(f"  {ticker}: 네트워크 오류 — {e}")
            break

    logger.info(f"  {ticker}: {len(all_rows)}건 수집")
    return all_rows


# ── 전체 수집 메인 ────────────────────────────────────────

def collect_all(tickers: list = None, incremental: bool = True):
    """
    전 종목 일봉 수집.

    Parameters
    ----------
    tickers     : 수집 대상 (기본: 주요 종목)
    incremental : True면 마지막 수집일 이후만
    """
    if not KIS_APP_KEY:
        logger.error("KIS_APP_KEY 미설정 — .env 파일 확인")
        return 0

    init_db()

    if tickers is None:
        tickers = get_target_tickers()

    total_rows = 0
    start_time = time.time()

    logger.info(f"국내 일봉 수집 시작 ({len(tickers)}종목)")

    for i, ticker in enumerate(tickers, 1):
        try:
            start_date = "19900101"
            if incremental:
                latest = get_latest_date("daily_ohlcv", ticker)
                if latest:
                    next_day = (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1))
                    start_date = next_day.strftime("%Y%m%d")
                    if next_day.date() > datetime.now().date():
                        continue

            rows = fetch_daily_bars(ticker, start_date=start_date)
            if rows:
                inserted = upsert_rows("daily_ohlcv", rows)
                total_rows += inserted
                log_collection("kis", "daily_ohlcv", ticker, inserted)

            if i % 20 == 0:
                elapsed = time.time() - start_time
                logger.info(f"  진행: {i}/{len(tickers)} ({elapsed:.0f}초)")

            time.sleep(0.15)  # KIS 호출 제한

        except Exception as e:
            logger.error(f"  {ticker} 일봉 수집 실패: {e}")
            log_collection("kis", "daily_ohlcv", ticker, 0, "ERROR", str(e))

    elapsed = time.time() - start_time
    logger.info(f"\n국내 일봉 수집 완료: {total_rows:,}건 / {elapsed:.0f}초")

    try:
        from tools.notifier_tools import _send
        _send(
            f"<b>[국내 일봉 수집 완료]</b>\n"
            f"총 {total_rows:,}건 ({len(tickers)}종목)\n"
            f"소요: {elapsed/60:.1f}분"
        )
    except Exception:
        pass

    return total_rows


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — 국내 일봉 수집 (KIS API)")
    print("=" * 55)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="전체 기간 수집")
    parser.add_argument("--ticker", type=str, help="특정 종목만")
    args = parser.parse_args()

    if args.ticker:
        rows = fetch_daily_bars(args.ticker)
        if rows:
            init_db()
            inserted = upsert_rows("daily_ohlcv", rows)
            print(f"  {inserted}건 저장")
    else:
        collect_all(incremental=not args.full)
