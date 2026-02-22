# QUANTUM FLOW — 메인 실행 진입점
# 실행 모드:
#   python main.py           → 스케줄러 모드 (24시간 상시 운영)
#   python main.py --once    → 1회 실행 모드 (순차 실행 후 종료)
#
# 스케줄러 시간표 (KST):
#   05:50  토큰 갱신
#   08:30  Agent 1: 거시경제 분석
#   08:50  Agent 2: 종목 스캔 (1차)
#   09:05  Agent 3: 전략 결정
#   09:10  Agent 4: 시장 감시 시작
#   11:30  Agent 2: 종목 스캔 (2차)
#   15:20  강제 청산 (모든 보유 포지션)
#   15:35  일별 대시보드 + 리포트
#   15:35  주별 대시보드 (금요일만)
#   23:00  토큰 갱신 (이중 안전)

from dotenv import load_dotenv
import os
import asyncio
import logging
import sys
import signal

load_dotenv()

from tools.trade_logger import (
    set_macro_snapshot, log_risk_event, end_of_day_routine, get_daily_trades
)
from tools.dashboard_tools import (
    create_and_send_daily_dashboard, create_and_send_weekly_dashboard
)
from tools.market_calendar import is_market_open_day, market_time_label, KST
from shared_state import get_state, set_state, get_positions, remove_position
from datetime import datetime

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ══════════════════════════════════════════════════════════════
#  환경 검증
# ══════════════════════════════════════════════════════════════

def check_env():
    """필수 환경변수 확인"""
    required = [
        "KIS_APP_KEY", "KIS_APP_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OPENAI_API_KEY",
    ]
    optional = ["FRED_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"⛔ 누락된 필수 환경변수: {missing}")
        print("   → Codespace Secrets 또는 .env 파일에서 설정하세요")
        return False

    missing_opt = [k for k in optional if not os.getenv(k)]
    if missing_opt:
        print(f"ℹ️  선택적 환경변수 미설정 (기능 제한): {missing_opt}")

    print("✅ 환경변수 확인 완료")
    return True


# ══════════════════════════════════════════════════════════════
#  Agent 래퍼 함수들 (개별 임포트 실패 방지)
# ══════════════════════════════════════════════════════════════

_macro_analyst_run = None
_market_scanner_run = None
_head_strategist_run = None
_market_watcher = None


def _load_agents():
    """에이전트 모듈을 로드한다. 실패해도 다른 에이전트에 영향 없음."""
    global _macro_analyst_run, _market_scanner_run, _head_strategist_run, _market_watcher

    try:
        from agents.macro_analyst import macro_analyst_run
        _macro_analyst_run = macro_analyst_run
    except ImportError as e:
        logger.error(f"macro_analyst import 실패: {e}")

    try:
        from agents.market_scanner import market_scanner_run
        _market_scanner_run = market_scanner_run
    except ImportError as e:
        logger.error(f"market_scanner import 실패: {e}")

    try:
        from agents.head_strategist import head_strategist_run
        _head_strategist_run = head_strategist_run
    except ImportError as e:
        logger.error(f"head_strategist import 실패: {e}")

    try:
        from agents.market_watcher import MarketWatcher
        _market_watcher = MarketWatcher(check_interval=60)
    except ImportError as e:
        logger.error(f"market_watcher import 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  스케줄러 Job 함수들
# ══════════════════════════════════════════════════════════════

async def job_refresh_token():
    """KIS API 토큰 갱신 (05:50, 23:00)"""
    if not is_market_open_day():
        return
    try:
        from tools.token_manager import ensure_token
        token = ensure_token()
        logger.info(f"토큰 갱신 완료: {token[:20]}...")
    except Exception as e:
        logger.error(f"토큰 갱신 실패: {e}")
        from tools.notifier_tools import notify_error
        notify_error("token_refresh", str(e))


async def job_macro_analysis():
    """Agent 1: 거시경제 분석 (08:30)"""
    if not is_market_open_day():
        logger.info("비개장일 — 거시분석 스킵")
        return
    print(f"\n{'─'*40}")
    print("STEP 1: 거시경제 분석")
    print("─" * 40)
    if _macro_analyst_run:
        try:
            macro_result = await _macro_analyst_run()
            risk_status = macro_result.get("risk_status", "?")
            print(f"  ✅ 결과: Risk-{risk_status}")
            set_macro_snapshot(macro_result)

            # Risk-OFF 긴급 처리
            if get_state("risk_off"):
                risk_params = get_state("risk_params") or {}
                log_risk_event("RISK_OFF", level="CRITICAL",
                               trigger="macro_analyst",
                               message="장 시작 전 Risk-OFF 판정")
                if risk_params.get("emergency_liquidate") and _head_strategist_run:
                    await _head_strategist_run()
        except Exception as e:
            logger.error(f"거시분석 실패: {e}")
    else:
        print("  ⚠️ macro_analyst 비활성화")


async def job_market_scan():
    """Agent 2: 종목 스캔 (08:50, 11:30)"""
    if not is_market_open_day():
        return
    now = datetime.now(KST).strftime("%H:%M")
    print(f"\n{'─'*40}")
    print(f"STEP 2: 종목 스캔 ({now})")
    print("─" * 40)
    if _market_scanner_run:
        try:
            scan_result = await _market_scanner_run()
            candidates = scan_result.get("candidates", 0)
            print(f"  ✅ 결과: {candidates}종목 감시 등록")
        except Exception as e:
            logger.error(f"종목 스캔 실패: {e}")
    else:
        print("  ⚠️ market_scanner 비활성화")


async def job_strategy_decision():
    """Agent 3: 전략 결정 (09:05)"""
    if not is_market_open_day():
        return
    if get_state("risk_off"):
        logger.info("Risk-OFF 상태 — 전략 결정 스킵")
        return
    print(f"\n{'─'*40}")
    print("STEP 3: 전략 결정")
    print("─" * 40)
    if _head_strategist_run:
        try:
            strategy_result = await _head_strategist_run()
            actions = len(strategy_result.get("actions", []))
            print(f"  ✅ 결과: {actions}건 매매 결정")
        except Exception as e:
            logger.error(f"전략 결정 실패: {e}")
    else:
        print("  ⚠️ head_strategist 비활성화")


def job_market_watcher_start():
    """Agent 4: 시장 감시 시작 (09:10) — 별도 스레드"""
    if not is_market_open_day():
        return
    if _market_watcher and not _market_watcher._running:
        _market_watcher.run()
        logger.info("MarketWatcher 감시 루프 시작")


def job_market_watcher_stop():
    """Agent 4: 시장 감시 중지 (15:25)"""
    if _market_watcher and _market_watcher._running:
        _market_watcher.stop()
        logger.info("MarketWatcher 감시 루프 중지")


async def job_force_close():
    """15:20 강제 청산 — 모든 보유 포지션을 시장가 매도"""
    if not is_market_open_day():
        return
    positions = get_positions()
    if not positions:
        logger.info("15:20 강제 청산 — 보유 포지션 없음")
        return

    print(f"\n{'─'*40}")
    print("15:20 강제 청산")
    print("─" * 40)

    from tools.order_executor import sell_market
    from tools.trade_logger import log_trade
    from tools.notifier_tools import notify_trade_decision

    for code, data in list(positions.items()):
        try:
            # 실제 매도 주문
            result = sell_market(code, qty=0)  # qty=0 → 전량
            log_trade("FORCE_CLOSE", code,
                      reason="15:20 장중 강제 청산",
                      position_pct=data.get("entry_pct", 0))
            remove_position(code)

            try:
                notify_trade_decision(
                    "FORCE_CLOSE", code,
                    data.get("entry_pct", 0), data.get("eval_grade", "?"),
                    "강제청산", "15:20 장마감 강제 청산",
                )
            except Exception:
                pass

            status = "성공" if result.get("success") else "실패"
            print(f"  {code}: 강제 청산 {status}")
        except Exception as e:
            logger.error(f"강제 청산 실패 ({code}): {e}")


async def job_daily_report():
    """15:35 일별 리포트 + 대시보드 생성"""
    if not is_market_open_day():
        return
    print(f"\n{'─'*40}")
    print("장 마감: 일일 매매 리포트 생성")
    print("─" * 40)
    try:
        positions = get_positions()
        daily_loss = get_state("daily_loss") or 0.0
        result = end_of_day_routine(positions, daily_loss)
        perf = result["performance"]
        print(f"  리포트 저장: {result['filepath']}")
        print(f"  매매 {perf.get('total_trades', 0)}건 | "
              f"승률 {perf.get('win_rate', 0):.0%} | "
              f"실현PnL {perf.get('realized_pnl', 0):+.2%}")

        # 일별 대시보드
        trades = get_daily_trades()
        create_and_send_daily_dashboard(perf, trades, positions)
    except Exception as e:
        logger.error(f"일일 리포트 생성 실패: {e}")


async def job_weekly_report():
    """15:35 주별 대시보드 (금요일만 실행)"""
    if not is_market_open_day():
        return
    now = datetime.now(KST)
    if now.weekday() != 4:  # 금요일만
        return
    try:
        create_and_send_weekly_dashboard()
        logger.info("주별 대시보드 전송 완료")
    except Exception as e:
        logger.error(f"주별 대시보드 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  스케줄러 모드 (기본)
# ══════════════════════════════════════════════════════════════

async def run_scheduler():
    """APScheduler 기반 24시간 상시 운영 모드"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    print("\n" + "=" * 60)
    print("  QUANTUM FLOW — 스케줄러 모드 (24시간 상시 운영)")
    print("=" * 60)

    if not check_env():
        return

    _load_agents()

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # ── 일일 스케줄 ──────────────────────────────────────────
    # 토큰 갱신
    scheduler.add_job(job_refresh_token, CronTrigger(hour=5, minute=50),
                      id="token_refresh_morning", name="토큰 갱신 (아침)")
    scheduler.add_job(job_refresh_token, CronTrigger(hour=23, minute=0),
                      id="token_refresh_night", name="토큰 갱신 (야간)")

    # Agent 1: 거시경제 분석
    scheduler.add_job(job_macro_analysis, CronTrigger(hour=8, minute=30),
                      id="macro_analysis", name="Agent1 거시분석")

    # Agent 2: 종목 스캔 (08:50 + 11:30)
    scheduler.add_job(job_market_scan, CronTrigger(hour=8, minute=50),
                      id="market_scan_1", name="Agent2 종목스캔 1차")
    scheduler.add_job(job_market_scan, CronTrigger(hour=11, minute=30),
                      id="market_scan_2", name="Agent2 종목스캔 2차")

    # Agent 3: 전략 결정
    scheduler.add_job(job_strategy_decision, CronTrigger(hour=9, minute=5),
                      id="strategy_decision", name="Agent3 전략결정")

    # Agent 4: 시장 감시 (시작/중지)
    scheduler.add_job(job_market_watcher_start, CronTrigger(hour=9, minute=10),
                      id="watcher_start", name="Agent4 감시시작")
    scheduler.add_job(job_market_watcher_stop, CronTrigger(hour=15, minute=25),
                      id="watcher_stop", name="Agent4 감시중지")

    # 15:20 강제 청산
    scheduler.add_job(job_force_close, CronTrigger(hour=15, minute=20),
                      id="force_close", name="15:20 강제청산")

    # 장 마감 리포트
    scheduler.add_job(job_daily_report, CronTrigger(hour=15, minute=35),
                      id="daily_report", name="일별 리포트")
    scheduler.add_job(job_weekly_report, CronTrigger(hour=15, minute=35),
                      id="weekly_report", name="주별 리포트")

    scheduler.start()

    # 등록된 스케줄 출력
    print("\n  등록된 스케줄:")
    for job in scheduler.get_jobs():
        print(f"    {job.name:<24s} → {job.trigger}")

    now = datetime.now(KST)
    label = market_time_label(now)
    print(f"\n  현재 시각: {now.strftime('%Y-%m-%d %H:%M:%S')} KST")
    print(f"  시장 상태: {label}")
    print(f"  개장일: {'Y' if is_market_open_day() else 'N'}")
    print("\n  Ctrl+C로 종료합니다.\n")

    # 이벤트 루프 유지
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    # 종료 처리
    job_market_watcher_stop()
    scheduler.shutdown(wait=False)
    print("\nQUANTUM FLOW 스케줄러 종료")


# ══════════════════════════════════════════════════════════════
#  1회 실행 모드 (--once)
# ══════════════════════════════════════════════════════════════

async def run_once():
    """기존 순차 실행 모드 — 1회 실행 후 종료"""
    print("\n" + "=" * 60)
    print("  QUANTUM FLOW — 1회 실행 모드")
    print("=" * 60)

    if not check_env():
        return

    _load_agents()

    # ── STEP 1: 거시경제 분석 ──
    await job_macro_analysis()

    # Risk-OFF면 조기 종료
    if get_state("risk_off"):
        risk_params = get_state("risk_params") or {}
        if risk_params.get("emergency_liquidate") and _head_strategist_run:
            await _head_strategist_run()
        result = end_of_day_routine(get_positions(), get_state("daily_loss") or 0.0)
        print(f"\n일일 리포트 저장: {result['filepath']}")
        print("\nQUANTUM FLOW 긴급 종료")
        return

    # ── STEP 2: 종목 스캔 ──
    await job_market_scan()

    # ── STEP 3: 전략 결정 ──
    await job_strategy_decision()

    # ── STEP 4: 시장 감시 (Ctrl+C로 종료) ──
    print(f"\n{'─'*40}")
    print("STEP 4: 시장 감시 (Ctrl+C로 종료)")
    print("─" * 40)
    if _market_watcher:
        try:
            _market_watcher._running = True
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _blocking_watcher_loop)
        except KeyboardInterrupt:
            print("\nℹ️ 사용자 종료 요청")
        except Exception as e:
            logger.error(f"시장 감시 실패: {e}")
        finally:
            if _market_watcher:
                _market_watcher._running = False
    else:
        print("  ⚠️ market_watcher 비활성화")

    # ── 장 마감 리포트 ──
    await job_daily_report()
    await job_weekly_report()

    print("\n" + "=" * 60)
    print("  QUANTUM FLOW 실행 완료")
    print("=" * 60)


def _blocking_watcher_loop():
    """executor 내에서 실행되는 블로킹 감시 루프"""
    import time
    while _market_watcher and _market_watcher._running:
        try:
            _market_watcher.check_cycle()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"MarketWatcher 주기 오류: {e}")
        time.sleep(_market_watcher.check_interval)


# ══════════════════════════════════════════════════════════════
#  진입점
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mode = "--once" if "--once" in sys.argv else "scheduler"

    try:
        if mode == "--once":
            asyncio.run(run_once())
        else:
            asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        print("\nℹ️ QUANTUM FLOW 종료")
        sys.exit(0)
