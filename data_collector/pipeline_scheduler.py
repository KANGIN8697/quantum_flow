# data_collector/pipeline_scheduler.py — 데이터 파이프라인 통합 스케줄러
#
# 로컬 Windows PC에서 실행되는 독립형 스케줄러
# APScheduler 기반 24시간 상시 운영
#
# 스케줄:
#   매일 06:00   — RSS 뉴스 수집
#   매일 15:35   — 키움 당일 분봉 증분 수집
#   매일 16:00   — Regime 자동 분류
#   매일 16:30   — 벡터 DB 당일 데이터 임베딩 추가
#   매주 월 07:00 — yfinance 해외 지표 업데이트
#   매월 1일 08:00 — ECOS/FRED 매크로 지표 업데이트
#   매주 일 09:00 — DART 공시 주간 업데이트

import os
import sys
import logging
import signal
import asyncio
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "..", "logs", "pipeline.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("pipeline")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.db_manager import init_db, db_status_report


# ── 장 휴일 체크 ─────────────────────────────────────────

def _is_holiday() -> bool:
    """한국 공휴일 + 주말 체크."""
    try:
        import holidays
        kr_holidays = holidays.KR(years=datetime.now().year)
        today = datetime.now().date()
        return today.weekday() >= 5 or today in kr_holidays
    except ImportError:
        return datetime.now().weekday() >= 5


# ── Job 래퍼 함수들 ──────────────────────────────────────

def job_news_daily():
    """06:00 — RSS 뉴스 수집."""
    logger.info("[뉴스] 일일 RSS 수집 시작")
    try:
        from data_collector.text.news_collector import run_daily
        count = run_daily()
        logger.info(f"[뉴스] {count}건 수집 완료")
    except Exception as e:
        logger.error(f"[뉴스] 실패: {e}")


def job_kiwoom_daily():
    """15:35 — 키움 당일 분봉 증분 수집."""
    if _is_holiday():
        logger.info("[키움] 장 휴일 스킵")
        return
    logger.info("[키움] 당일 분봉 증분 수집")
    try:
        from data_collector.price.kiwoom_collector import run_daily
        run_daily()
    except Exception as e:
        logger.error(f"[키움] 실패: {e}")


def job_regime_daily():
    """16:00 — Regime 자동 분류."""
    logger.info("[Regime] 일일 분류 시작")
    try:
        from data_collector.regime.regime_classifier import run_daily
        result = run_daily()
        logger.info(f"[Regime] 결과: {result.get('regime', '?')} (score={result.get('score', 0)})")
    except Exception as e:
        logger.error(f"[Regime] 실패: {e}")


def job_vector_daily():
    """16:30 — 벡터 DB 당일 임베딩 추가."""
    logger.info("[벡터] 당일 임베딩 추가")
    try:
        from data_collector.vector.vector_store_builder import add_today
        count = add_today()
        logger.info(f"[벡터] {count}건 추가")
    except Exception as e:
        logger.error(f"[벡터] 실패: {e}")


def job_global_weekly():
    """매주 월요일 07:00 — yfinance 해외 지표 업데이트."""
    logger.info("[yfinance] 주간 해외 지표 업데이트")
    try:
        from data_collector.price.global_collector import run_weekly
        count = run_weekly()
        logger.info(f"[yfinance] {count}건 업데이트")
    except Exception as e:
        logger.error(f"[yfinance] 실패: {e}")


def job_macro_monthly():
    """매월 1일 08:00 — ECOS + FRED 매크로 지표 업데이트."""
    logger.info("[매크로] 월간 업데이트 시작")
    try:
        from data_collector.macro.ecos_collector import run_monthly as ecos_run
        from data_collector.macro.fred_collector import run_monthly as fred_run
        ecos_count = ecos_run()
        fred_count = fred_run()
        logger.info(f"[매크로] ECOS {ecos_count}건, FRED {fred_count}건")
    except Exception as e:
        logger.error(f"[매크로] 실패: {e}")


def job_dart_weekly():
    """매주 일요일 09:00 — DART 공시 주간 업데이트."""
    logger.info("[DART] 주간 공시 업데이트")
    try:
        from data_collector.text.dart_collector import run_weekly
        count = run_weekly()
        logger.info(f"[DART] {count}건 업데이트")
    except Exception as e:
        logger.error(f"[DART] 실패: {e}")


def job_db_status():
    """23:00 — DB 상태 리포트."""
    report = db_status_report()
    logger.info("[DB 상태 리포트]")
    for table, info in report.items():
        logger.info(f"  {table:25s} | {info['rows']:>8,} rows | latest: {info['latest']}")

    # 텔레그램 리포트
    try:
        from tools.notifier_tools import _send
        lines = ["<b>[데이터 파이프라인 상태]</b>"]
        for table, info in report.items():
            if info["rows"] > 0:
                lines.append(f"  {table}: {info['rows']:,}건 (최신: {info['latest']})")
        _send("\n".join(lines))
    except Exception:
        pass


# ── 메인 스케줄러 ─────────────────────────────────────────

def run_scheduler():
    """APScheduler 기반 24시간 데이터 파이프라인."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    print("\n" + "=" * 60)
    print("  QUANTUM FLOW — 데이터 파이프라인 스케줄러")
    print("=" * 60)

    init_db()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # ── 일일 ──────────────────────────────────────────────
    scheduler.add_job(job_news_daily, CronTrigger(hour=6, minute=0),
                      id="news_daily", name="RSS 뉴스 수집")

    scheduler.add_job(job_kiwoom_daily, CronTrigger(hour=15, minute=35),
                      id="kiwoom_daily", name="키움 증분 수집")

    scheduler.add_job(job_regime_daily, CronTrigger(hour=16, minute=0),
                      id="regime_daily", name="Regime 분류")

    scheduler.add_job(job_vector_daily, CronTrigger(hour=16, minute=30),
                      id="vector_daily", name="벡터 DB 추가")

    # ── 주간 ──────────────────────────────────────────────
    scheduler.add_job(job_global_weekly, CronTrigger(day_of_week="mon", hour=7, minute=0),
                      id="global_weekly", name="해외 지표 주간")

    scheduler.add_job(job_dart_weekly, CronTrigger(day_of_week="sun", hour=9, minute=0),
                      id="dart_weekly", name="DART 주간")

    # ── 월간 ──────────────────────────────────────────────
    scheduler.add_job(job_macro_monthly, CronTrigger(day=1, hour=8, minute=0),
                      id="macro_monthly", name="매크로 월간")

    # ── 상태 리포트 ───────────────────────────────────────
    scheduler.add_job(job_db_status, CronTrigger(hour=23, minute=0),
                      id="db_status", name="DB 상태 리포트")

    # 등록된 Job 목록 출력
    print("\n  등록된 스케줄:")
    for job in scheduler.get_jobs():
        print(f"    {job.name:25s} | {job.trigger}")

    print(f"\n  DB 경로: {os.path.join(os.path.dirname(__file__), '..', 'database', 'market_data.db')}")
    print("  Ctrl+C로 종료\n")

    # SIGINT/SIGTERM 핸들링
    def _shutdown(signum, frame):
        logger.info("스케줄러 종료 중...")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("파이프라인 스케줄러 종료")


# ── 수동 실행 모드 ────────────────────────────────────────

def run_once(target: str):
    """특정 수집기를 즉시 1회 실행."""
    init_db()
    runners = {
        "news": job_news_daily,
        "kiwoom": job_kiwoom_daily,
        "regime": job_regime_daily,
        "vector": job_vector_daily,
        "global": job_global_weekly,
        "macro": job_macro_monthly,
        "dart": job_dart_weekly,
        "status": job_db_status,
    }
    if target in runners:
        runners[target]()
    else:
        print(f"  사용 가능: {', '.join(runners.keys())}")


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="QUANTUM FLOW 데이터 파이프라인")
    parser.add_argument("--run", type=str, help="특정 수집기 즉시 실행 (news/global/macro/...)")
    parser.add_argument("--status", action="store_true", help="DB 상태 확인")
    args = parser.parse_args()

    if args.status:
        init_db()
        report = db_status_report()
        print("=" * 60)
        print("  데이터 파이프라인 상태")
        print("=" * 60)
        for table, info in report.items():
            print(f"  {table:25s} | {info['rows']:>8,} rows | latest: {info['latest']}")
    elif args.run:
        run_once(args.run)
    else:
        run_scheduler()
