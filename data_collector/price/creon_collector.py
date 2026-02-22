# data_collector/price/creon_collector.py — 대신증권 Creon Plus 분봉 수집
#
# 실행 환경: Windows 32bit Python (Anaconda 32bit 권장)
# 사전 조건:
#   1. 대신증권 계좌 개설 + Creon HTS 설치
#   2. 관리자 권한으로 Creon HTS 실행 상태
#   3. pip install pywin32 (32bit 환경)
#
# 사용법:
#   python creon_collector.py                  # 전 종목 전 타임프레임
#   python creon_collector.py --ticker 005930  # 삼성전자만
#   python creon_collector.py --timeframe 5    # 5분봉만
#
# ⚠️ 이 파일은 Linux/Mac에서 실행 불가 (COM 객체 필요)

import os
import sys
import time
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("creon_collector")

# ── Windows COM 런타임 체크 ─────────────────────────────────
_CREON_AVAILABLE = False
try:
    import win32com.client
    _CREON_AVAILABLE = True
except ImportError:
    logger.warning("pywin32 미설치 — Creon 수집 비활성화 (Windows 32bit 전용)")

# DB 경로 설정 (상위 디렉토리의 database/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, get_latest_date, log_collection


# ── KOSPI200 + KOSDAQ150 종목 리스트 조회 ──────────────────

def get_index_constituents() -> list:
    """
    Creon CpSysDib.CpCodeMgr를 이용해 KOSPI200 + KOSDAQ150 구성 종목 조회.
    반환: [{"ticker": "005930", "name": "삼성전자", "market": "KOSPI"}, ...]
    """
    if not _CREON_AVAILABLE:
        return _get_fallback_tickers()

    code_mgr = win32com.client.Dispatch("CpUtil.CpCodeMgr")
    tickers = []

    # KOSPI (시장구분 1)
    kospi_codes = code_mgr.GetStockListByMarket(1)
    for code in kospi_codes:
        name = code_mgr.CodeToName(code)
        section = code_mgr.GetStockSectionKind(code)
        if section == 1:  # 보통주만
            tickers.append({
                "ticker": code.replace("A", ""),
                "name": name,
                "market": "KOSPI",
            })

    # KOSDAQ (시장구분 2)
    kosdaq_codes = code_mgr.GetStockListByMarket(2)
    for code in kosdaq_codes:
        name = code_mgr.CodeToName(code)
        section = code_mgr.GetStockSectionKind(code)
        if section == 1:
            tickers.append({
                "ticker": code.replace("A", ""),
                "name": name,
                "market": "KOSDAQ",
            })

    logger.info(f"Creon 종목 조회: KOSPI {sum(1 for t in tickers if t['market']=='KOSPI')}개 "
                f"+ KOSDAQ {sum(1 for t in tickers if t['market']=='KOSDAQ')}개")
    return tickers


def _get_fallback_tickers() -> list:
    """Creon 미사용 환경용 주요 종목 폴백 리스트."""
    return [
        {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
        {"ticker": "000660", "name": "SK하이닉스", "market": "KOSPI"},
        {"ticker": "005380", "name": "현대차", "market": "KOSPI"},
        {"ticker": "000270", "name": "기아", "market": "KOSPI"},
        {"ticker": "006400", "name": "삼성SDI", "market": "KOSPI"},
        {"ticker": "051910", "name": "LG화학", "market": "KOSPI"},
        {"ticker": "035420", "name": "NAVER", "market": "KOSPI"},
        {"ticker": "035720", "name": "카카오", "market": "KOSPI"},
        {"ticker": "005490", "name": "POSCO홀딩스", "market": "KOSPI"},
        {"ticker": "068270", "name": "셀트리온", "market": "KOSPI"},
        {"ticker": "207940", "name": "삼성바이오로직스", "market": "KOSPI"},
        {"ticker": "003670", "name": "포스코퓨처엠", "market": "KOSPI"},
        {"ticker": "373220", "name": "LG에너지솔루션", "market": "KOSPI"},
        {"ticker": "247540", "name": "에코프로비엠", "market": "KOSDAQ"},
        {"ticker": "086520", "name": "에코프로", "market": "KOSDAQ"},
        {"ticker": "041510", "name": "에스엠", "market": "KOSDAQ"},
        {"ticker": "263750", "name": "펄어비스", "market": "KOSDAQ"},
        {"ticker": "196170", "name": "알테오젠", "market": "KOSDAQ"},
        {"ticker": "028300", "name": "HLB", "market": "KOSDAQ"},
        {"ticker": "017670", "name": "SK텔레콤", "market": "KOSPI"},
    ]


# ── Creon 접속 상태 확인 ──────────────────────────────────

def check_creon_connection() -> bool:
    """Creon Plus 접속 상태 확인."""
    if not _CREON_AVAILABLE:
        return False
    try:
        cp_cybos = win32com.client.Dispatch("CpUtil.CpCybos")
        connected = cp_cybos.IsConnect
        if not connected:
            logger.error("Creon HTS 미접속. 관리자 권한으로 Creon Plus 실행 필요.")
        return bool(connected)
    except Exception as e:
        logger.error(f"Creon 접속 확인 실패: {e}")
        return False


def _wait_for_limit_count():
    """Creon 잔여 호출 횟수 체크 — 0이면 대기."""
    if not _CREON_AVAILABLE:
        return
    cp_cybos = win32com.client.Dispatch("CpUtil.CpCybos")
    remain = cp_cybos.GetLimitRemainCount(1)  # 1: 시세 요청
    if remain <= 0:
        logger.info("Creon 호출 제한 — 15초 대기")
        time.sleep(15)


# ── 분봉 데이터 수집 (핵심 로직) ──────────────────────────

def fetch_minute_bars(ticker: str, timeframe: int = 5,
                      max_count: int = 2000) -> list:
    """
    Creon CpSysDib.StockChart로 분봉 데이터를 수집한다.

    Parameters
    ----------
    ticker    : 종목코드 (예: "005930")
    timeframe : 분봉 주기 (5, 15, 60)
    max_count : 한 번에 요청할 최대 건수 (Creon 한도 ~2000)

    Returns
    -------
    list of dict: [{datetime, ticker, open, high, low, close, volume}, ...]
    """
    if not _CREON_AVAILABLE:
        logger.error("Creon 비활성화 — Windows 32bit 환경에서 실행하세요")
        return []

    creon_ticker = f"A{ticker}" if not ticker.startswith("A") else ticker
    chart = win32com.client.Dispatch("CpSysDib.StockChart")

    # 필드 설정: 0=날짜, 1=시간, 2=시가, 3=고가, 4=저가, 5=종가, 8=거래량
    chart.SetInputValue(0, creon_ticker)
    chart.SetInputValue(1, ord("2"))   # 2: 개수 기준
    chart.SetInputValue(4, max_count)
    chart.SetInputValue(5, [0, 1, 2, 3, 4, 5, 8])
    chart.SetInputValue(6, ord("m"))   # m: 분봉
    chart.SetInputValue(7, timeframe)
    chart.SetInputValue(9, ord("1"))   # 1: 수정주가

    all_rows = []
    total_fetched = 0

    while True:
        _wait_for_limit_count()
        chart.BlockRequest()

        status = chart.GetDibStatus()
        if status != 0:
            msg = chart.GetDibMsg1()
            logger.warning(f"Creon 응답 오류 ({ticker}, {timeframe}분): status={status}, msg={msg}")
            break

        count = chart.GetHeaderValue(3)  # 수신 건수
        if count == 0:
            break

        for i in range(count):
            date_val = chart.GetDataValue(0, i)  # YYYYMMDD
            time_val = chart.GetDataValue(1, i)  # HHMM
            dt_str = f"{date_val} {time_val:04d}"
            # → "20260220 0935" 형식을 ISO로 변환
            try:
                dt = datetime.strptime(dt_str, "%Y%m%d %H%M")
                dt_iso = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue

            all_rows.append({
                "datetime": dt_iso,
                "ticker": ticker,
                "open": safe_float(chart.GetDataValue(2, i)),
                "high": safe_float(chart.GetDataValue(3, i)),
                "low": safe_float(chart.GetDataValue(4, i)),
                "close": safe_float(chart.GetDataValue(5, i)),
                "volume": int(chart.GetDataValue(6, i)),
            })

        total_fetched += count

        # 연속 조회 가능 여부
        if not chart.Continue:
            break

        time.sleep(0.07)  # 초당 15회 제한 준수

    logger.info(f"  {ticker} {timeframe}분봉: {total_fetched}건 수집")
    return all_rows


# ── 전체 수집 메인 함수 ───────────────────────────────────

def collect_all(timeframes: list = None, tickers: list = None):
    """
    전 종목 × 전 타임프레임 분봉 수집 후 DB 저장.

    Parameters
    ----------
    timeframes : [5, 15, 60] (기본: 전체)
    tickers    : 종목 리스트 (기본: KOSPI200 + KOSDAQ150)
    """
    if timeframes is None:
        timeframes = [5, 15, 60]

    if not _CREON_AVAILABLE:
        logger.error("Creon 미사용 환경 — 수집 중단")
        return

    if not check_creon_connection():
        return

    init_db()

    if tickers is None:
        stock_list = get_index_constituents()
        tickers = [s["ticker"] for s in stock_list]

    table_map = {5: "ohlcv_5m", 15: "ohlcv_15m", 60: "ohlcv_60m"}

    total_start = time.time()
    total_rows = 0

    for tf in timeframes:
        table = table_map[tf]
        logger.info(f"\n{'='*40}")
        logger.info(f"  {tf}분봉 수집 시작 ({len(tickers)}종목)")
        logger.info(f"{'='*40}")

        for i, ticker in enumerate(tickers, 1):
            try:
                # 이미 수집된 최신 날짜 확인
                latest = get_latest_date(table, ticker, date_col="datetime")

                rows = fetch_minute_bars(ticker, tf)
                if not rows:
                    continue

                # 이미 수집된 날짜 이후만 필터링
                if latest:
                    rows = [r for r in rows if r["datetime"] > latest]

                if rows:
                    inserted = upsert_rows(table, rows)
                    total_rows += inserted
                    log_collection("creon", f"{tf}m_bars", ticker, inserted)

                # 진행률 표시
                if i % 50 == 0:
                    elapsed = time.time() - total_start
                    logger.info(f"  진행: {i}/{len(tickers)} ({elapsed:.0f}초 경과)")

            except Exception as e:
                logger.error(f"  {ticker} {tf}분봉 수집 실패: {e}")
                log_collection("creon", f"{tf}m_bars", ticker, 0, "ERROR", str(e))
                continue

    elapsed = time.time() - total_start
    logger.info(f"\n  Creon 수집 완료: {total_rows:,}건 / {elapsed:.0f}초")

    # 텔레그램 알림
    _notify_completion(total_rows, elapsed)


def _notify_completion(total_rows: int, elapsed: float):
    """수집 완료 텔레그램 알림."""
    try:
        from tools.notifier_tools import _send
        _send(
            f"<b>[Creon 분봉 수집 완료]</b>\n"
            f"총 {total_rows:,}건 수집\n"
            f"소요 시간: {elapsed/60:.1f}분"
        )
    except Exception:
        pass


# ── CLI 엔트리포인트 ──────────────────────────────────────

if __name__ == "__main__":
    import argparse

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

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="Creon 분봉 데이터 수집")
    parser.add_argument("--ticker", type=str, help="특정 종목코드만 수집")
    parser.add_argument("--timeframe", type=int, choices=[5, 15, 60],
                        help="특정 타임프레임만 수집")
    args = parser.parse_args()

    timeframes = [args.timeframe] if args.timeframe else None
    tickers = [args.ticker] if args.ticker else None

    if not _CREON_AVAILABLE:
        print("=" * 55)
        print("  [경고] Windows 32bit + Creon HTS 환경에서만 실행 가능")
        print("  현재 환경에서는 코드 구조만 확인됩니다.")
        print("=" * 55)
    else:
        collect_all(timeframes=timeframes, tickers=tickers)
