# data_collector/price/global_collector.py — yfinance 해외 지수/지표 일봉 수집
#
# 실행 환경: Python 3.10+ (64bit OK)
# 수집 대상: S&P500, 나스닥, VIX, 원달러, 금, 유가, 국채 등
# 무료 API이지만 간헐적 데이터 누락 가능 → NaN 체크 + 재시도

import os
import sys
import time
import logging
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd
import numpy as np

logger = logging.getLogger("global_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, get_latest_date, log_collection

# ── 수집 대상 정의 ────────────────────────────────────────

GLOBAL_TICKERS = {
    # 미국 지수
    "^GSPC":     "S&P500",
    "^IXIC":     "나스닥",
    "^DJI":      "다우존스",

    # 변동성
    "^VIX":      "VIX",
    "^VVIX":     "VVIX",

    # 환율
    "DX-Y.NYB":  "달러인덱스",
    "KRW=X":     "원달러",
    "JPY=X":     "엔달러",

    # 채권
    "^TNX":      "미국10년국채",
    "^IRX":      "미국2년국채",

    # 원자재
    "CL=F":      "WTI원유",
    "GC=F":      "금",
    "HG=F":      "구리",

    # 아시아 지수
    "000001.SS": "상하이종합",
    "^HSI":      "항셍",
    "^N225":     "닛케이225",
}


# ── 개별 종목 수집 ────────────────────────────────────────

def fetch_ticker_history(ticker: str, name: str,
                         start: str = None, end: str = None,
                         max_retries: int = 3) -> list:
    """
    yfinance로 일봉 데이터 수집.

    Parameters
    ----------
    ticker : yfinance ticker 심볼
    name   : 한글 이름
    start  : 시작일 (YYYY-MM-DD), None이면 전체 기간
    end    : 종료일, None이면 오늘
    max_retries : NaN 발생 시 재시도 횟수

    Returns
    -------
    list of dict
    """
    for attempt in range(max_retries):
        try:
            t = yf.Ticker(ticker)

            if start:
                df = t.history(start=start, end=end or datetime.now().strftime("%Y-%m-%d"))
            else:
                df = t.history(period="max")

            if df.empty:
                logger.warning(f"  {ticker} ({name}): 데이터 없음")
                return []

            # 컬럼 소문자 정규화
            df.columns = [c.lower() for c in df.columns]

            # NaN 체크
            nan_count = df[["close"]].isna().sum().sum()
            if nan_count > 0:
                logger.warning(f"  {ticker}: NaN {nan_count}건 발견 (시도 {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                # 마지막 시도: NaN 행 제거
                df = df.dropna(subset=["close"])

            rows = []
            for idx, row in df.iterrows():
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                rows.append({
                    "date": date_str,
                    "ticker": ticker,
                    "name": name,
                    "open": round(float(row.get("open", 0)), 4) if pd.notna(row.get("open")) else None,
                    "high": round(float(row.get("high", 0)), 4) if pd.notna(row.get("high")) else None,
                    "low": round(float(row.get("low", 0)), 4) if pd.notna(row.get("low")) else None,
                    "close": round(float(row["close"]), 4),
                    "volume": int(row.get("volume", 0)) if pd.notna(row.get("volume")) else 0,
                })

            logger.info(f"  {ticker} ({name}): {len(rows)}건")
            return rows

        except Exception as e:
            logger.error(f"  {ticker} ({name}): 수집 실패 (시도 {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(3)

    return []


# ── 전체 수집 ─────────────────────────────────────────────

def collect_all(incremental: bool = True):
    """
    전체 해외 지표 수집.

    Parameters
    ----------
    incremental : True면 마지막 수집일 이후만, False면 전체 기간
    """
    init_db()

    total_rows = 0
    start_time = time.time()

    logger.info(f"해외 지표 수집 시작 ({len(GLOBAL_TICKERS)}종목)")

    for ticker, name in GLOBAL_TICKERS.items():
        start_date = None
        if incremental:
            latest = get_latest_date("global_daily", ticker)
            if latest:
                # 마지막 날짜 다음날부터
                start_date = (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                if start_date > datetime.now().strftime("%Y-%m-%d"):
                    logger.info(f"  {ticker} ({name}): 최신 상태")
                    continue

        rows = fetch_ticker_history(ticker, name, start=start_date)
        if rows:
            inserted = upsert_rows("global_daily", rows)
            total_rows += inserted
            log_collection("yfinance", "global_daily", ticker, inserted)

        time.sleep(0.5)  # yfinance 부하 방지

    elapsed = time.time() - start_time
    logger.info(f"\n해외 지표 수집 완료: {total_rows:,}건 / {elapsed:.0f}초")
    log_collection("yfinance", "collect_all", rows_added=total_rows)

    # 텔레그램 알림
    try:
        from tools.notifier_tools import _send
        _send(
            f"<b>[해외 지표 수집 완료]</b>\n"
            f"총 {total_rows:,}건 ({len(GLOBAL_TICKERS)}종목)\n"
            f"소요: {elapsed/60:.1f}분"
        )
    except Exception:
        pass

    return total_rows


def run_weekly():
    """주간 업데이트 (매주 월요일 07:00)."""
    logger.info("주간 해외 지표 업데이트 시작")
    return collect_all(incremental=True)


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — 해외 지표 수집 (yfinance)")
    print("=" * 55)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="전체 기간 수집")
    parser.add_argument("--ticker", type=str, help="특정 ticker만")
    args = parser.parse_args()

    if args.ticker:
        name = GLOBAL_TICKERS.get(args.ticker, args.ticker)
        rows = fetch_ticker_history(args.ticker, name)
        if rows:
            init_db()
            inserted = upsert_rows("global_daily", rows)
            print(f"  {inserted}건 저장 완료")
    else:
        collect_all(incremental=not args.full)
