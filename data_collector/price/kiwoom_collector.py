# data_collector/price/kiwoom_collector.py — 키움증권 OpenAPI+ 증분 수집
#
# 실행 환경: Windows 32bit Python (Creon과 별도 가상환경!)
# 사전 조건:
#   1. 키움증권 계좌 + OpenAPI+ 설치
#   2. 키움 OpenAPI 로그인 상태
#   3. pip install pykiwoom (32bit 환경)
#
# 역할: 매일 장 마감 후(15:35) 당일 분봉 데이터를 Creon DB에 append
# ⚠️ 키움과 Creon은 동시 프로세스 실행 불가 — 별도 가상환경 필수

import os
import sys
import time
import logging
from datetime import datetime

logger = logging.getLogger("kiwoom_collector")

# ── Windows/키움 런타임 체크 ──────────────────────────────
_KIWOOM_AVAILABLE = False
try:
    from pykiwoom.kiwoom import Kiwoom
    _KIWOOM_AVAILABLE = True
except ImportError:
    logger.warning("pykiwoom 미설치 — 키움 수집 비활성화 (Windows 32bit 전용)")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, upsert_rows, log_collection


# ── 키움 로그인 ───────────────────────────────────────────

def _login() -> "Kiwoom":
    """키움 OpenAPI 로그인. 반환: Kiwoom 인스턴스."""
    if not _KIWOOM_AVAILABLE:
        raise RuntimeError("pykiwoom 미설치")

    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)

    state = kiwoom.GetConnectState()
    if state != 1:
        raise ConnectionError("키움 OpenAPI 로그인 실패")

    logger.info("키움 OpenAPI 로그인 성공")
    return kiwoom


# ── 당일 분봉 수집 ────────────────────────────────────────

def fetch_today_bars(kiwoom, ticker: str, timeframe: int = 5) -> list:
    """
    opt10080 (주식분봉차트조회요청)으로 당일 분봉 수집.

    Parameters
    ----------
    kiwoom    : Kiwoom 인스턴스
    ticker    : 종목코드 ("005930")
    timeframe : 분봉 주기 (5, 15, 60)

    Returns
    -------
    list of dict
    """
    today = datetime.now().strftime("%Y%m%d")
    rows = []

    # TR 요청
    kiwoom.SetInputValue("종목코드", ticker)
    kiwoom.SetInputValue("틱범위", str(timeframe))
    kiwoom.SetInputValue("수정주가구분", "1")

    kiwoom.CommRqData("분봉차트", "opt10080", 0, "0101")
    time.sleep(0.25)  # 키움 초당 5회 제한 (0.2초+)

    # 데이터 추출
    count = kiwoom.GetRepeatCnt("분봉차트", "분봉차트조회")
    for i in range(count):
        dt_raw = kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "체결시간").strip()
        # dt_raw: "20260222093500" → "2026-02-22 09:35"
        if not dt_raw or len(dt_raw) < 12:
            continue

        # 당일 데이터만 필터링
        if not dt_raw.startswith(today):
            break

        try:
            dt = datetime.strptime(dt_raw[:12], "%Y%m%d%H%M")
            dt_iso = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue

        rows.append({
            "datetime": dt_iso,
            "ticker": ticker,
            "open": abs(int(kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "시가").strip())),
            "high": abs(int(kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "고가").strip())),
            "low": abs(int(kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "저가").strip())),
            "close": abs(int(kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "현재가").strip())),
            "volume": abs(int(kiwoom.GetCommData("분봉차트", "분봉차트조회", i, "거래량").strip())),
        })

    return rows


# ── 일일 증분 업데이트 메인 ───────────────────────────────

def run_daily(tickers: list = None, timeframes: list = None):
    """
    매일 15:35 실행: 당일 분봉을 키움에서 수집하여 Creon DB에 append.

    Parameters
    ----------
    tickers    : 수집 대상 종목 (기본: 주요 20종목)
    timeframes : [5, 15, 60] (기본: 전체)
    """
    # 장 휴일 체크
    if _is_holiday():
        logger.info("장 휴일 — 키움 증분 수집 스킵")
        return

    if not _KIWOOM_AVAILABLE:
        logger.error("키움 비활성화 — Windows 32bit 환경에서 실행하세요")
        return

    init_db()
    kiwoom = _login()

    if timeframes is None:
        timeframes = [5, 15, 60]

    if tickers is None:
        from data_collector.price.creon_collector import _get_fallback_tickers
        tickers = [t["ticker"] for t in _get_fallback_tickers()]

    table_map = {5: "ohlcv_5m", 15: "ohlcv_15m", 60: "ohlcv_60m"}
    total_rows = 0

    for tf in timeframes:
        table = table_map[tf]
        for ticker in tickers:
            try:
                rows = fetch_today_bars(kiwoom, ticker, tf)
                if rows:
                    inserted = upsert_rows(table, rows)
                    total_rows += inserted
                time.sleep(0.25)  # 키움 호출 제한 준수
            except Exception as e:
                logger.error(f"키움 {ticker} {tf}분봉 실패: {e}")
                log_collection("kiwoom", f"{tf}m_daily", ticker, 0, "ERROR", str(e))

    log_collection("kiwoom", "daily_update", rows_added=total_rows)
    logger.info(f"키움 증분 수집 완료: {total_rows}건")

    # 텔레그램 알림
    try:
        from tools.notifier_tools import _send
        _send(f"<b>[키움 증분 수집]</b> 당일 분봉 {total_rows:,}건 추가")
    except Exception:
        pass


def _is_holiday() -> bool:
    """한국 공휴일 여부 확인."""
    try:
        import holidays
        kr_holidays = holidays.KR(years=datetime.now().year)
        today = datetime.now().date()
        # 주말 + 공휴일
        return today.weekday() >= 5 or today in kr_holidays
    except ImportError:
        # holidays 미설치 시 주말만 체크
        return datetime.now().weekday() >= 5


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    if not _KIWOOM_AVAILABLE:
        print("=" * 55)
        print("  [경고] Windows 32bit + 키움 OpenAPI 환경에서만 실행 가능")
        print("  현재 환경에서는 코드 구조만 확인됩니다.")
        print("=" * 55)
    else:
        run_daily()
