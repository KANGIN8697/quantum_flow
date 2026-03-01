# QUANTUM FLOW — 메인 실행 진입점
# 실행 모드:
#   python main.py           → 스케줄러 모드 (24시간 상시 운영)
#   python main.py --once    → 1회 실행 모드 (순차 실행 후 종료)
#
# 스케줄러 시간표 (KST):
#   매시간  24시간 롤링 뉴스 스캔 (연합/Reuters/Google/네이버)
#   05:50  토큰 갱신
#   08:30  Agent 1: 거시경제 분석
#   08:50  Agent 2: 종목 스캔 (1차)
#   09:02  오버나이트 포지션 손절 체크 (종가 기준)
#   09:05  Agent 3: 전략 결정
#   09:10  Agent 4: 시장 감시 시작
#   11:30  Agent 2: 종목 스캔 (2차)
#   15:20  장마감 판단 (오버나이트 종합 평가 → 홀딩 or 청산)
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

USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"
MODE = "모의투자" if USE_PAPER else "실전투자"

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
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
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
        logger.error(f"macro_analyst import 실패: {e}", exc_info=True)

    try:
        from agents.market_scanner import market_scanner_run
        _market_scanner_run = market_scanner_run
    except ImportError as e:
        logger.error(f"market_scanner import 실패: {e}", exc_info=True)

    try:
        from agents.head_strategist import head_strategist_run
        _head_strategist_run = head_strategist_run
    except ImportError as e:
        logger.error(f"head_strategist import 실패: {e}", exc_info=True)

    try:
        from agents.market_watcher import MarketWatcher
        _market_watcher = MarketWatcher(check_interval=60)
    except ImportError as e:
        logger.error(f"market_watcher import 실패: {e}", exc_info=True)


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
        logger.error(f"토큰 갱신 실패: {e}", exc_info=True)
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

            # 텔레그램 알림
            try:
                from tools.notifier_tools import notify_macro_analysis
                notify_macro_analysis(macro_result, mode=MODE)
            except Exception as ntf_err:
                print(f"  ⚠ 거시분석 텔레그램 알림 실패: {ntf_err}")

            # Risk-OFF 긴급 처리
            if get_state("risk_off"):
                risk_params = get_state("risk_params") or {}
                log_risk_event("RISK_OFF", level="CRITICAL",
                               trigger="macro_analyst",
                               message="장 시작 전 Risk-OFF 판정")
                if risk_params.get("emergency_liquidate") and _head_strategist_run:
                    await _head_strategist_run()
        except Exception as e:
            logger.error(f"거시분석 실패: {e}", exc_info=True)
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
            logger.error(f"종목 스캔 실패: {e}", exc_info=True)
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
            logger.error(f"전략 결정 실패: {e}", exc_info=True)
    else:
        print("  ⚠️ head_strategist 비활성화")


async def job_overnight_check():
    """
    09:05 오버나이트 포지션 손절 체크.
    전일 종가 기준 손절가 이하면 즉시 시장가 매도.
    """
    if not is_market_open_day():
        return
    positions = get_positions()
    overnight_positions = {
        code: data for code, data in positions.items()
        if data.get("overnight")
    }
    if not overnight_positions:
        return

    print(f"\n{'─'*40}")
    print("오버나이트 포지션 손절 체크")
    print("─" * 40)

    from tools.order_executor import sell_market, get_balance
    from tools.trade_logger import log_trade
    from tools.notifier_tools import notify_trade_decision

    # 현재 잔고에서 실시간 가격 가져오기
    try:
        balance = get_balance()
        price_map = {p["code"]: p["current_price"] for p in balance.get("positions", [])}
    except Exception:
        price_map = {}

    for code, data in overnight_positions.items():
        stop_price = data.get("overnight_stop", 0)
        closing_price = data.get("closing_price", 0)
        current_price = price_map.get(code, 0)

        if current_price <= 0:
            logger.warning(f"  {code}: 현재가 조회 불가 — 손절 체크 스킵")
            continue
        if closing_price <= 0 or stop_price <= 0:
            logger.warning(f"  {code}: 종가/손절가 미설정 — 손절 체크 스킵")
            continue

        change_pct = (current_price / (closing_price or 1) - 1) * 100
        print(f"  {code}: 현재 {current_price:,.0f}원 / 종가 {closing_price:,.0f}원 / 손절선 {stop_price:,.0f}원 ({change_pct:+.1f}%)")

        if current_price <= stop_price:
            # ── 손절 실행 ──
            print(f"    → 손절 발동! (종가 대비 {change_pct:+.1f}%)")
            try:
                result = sell_market(code, qty=0)
                log_trade("OVERNIGHT_STOP", code,
                          reason=f"익일 손절 (종가 {closing_price:,.0f} → {current_price:,.0f})",
                          position_pct=data.get("entry_pct", 0))
                remove_position(code)

                try:
                    notify_trade_decision(
                        "OVERNIGHT_STOP", code,
                        data.get("entry_pct", 0), data.get("overnight_grade", "?"),
                        "손절", f"종가 {closing_price:,.0f} 대비 {change_pct:+.1f}%",
                    )
                except Exception as e:
                    logger.debug(f"suppressed: {e}")
            except Exception as e:
                logger.error(f"오버나이트 손절 실패 ({code}): {e}", exc_info=True)
        else:
            # 오버나이트 플래그 해제 (정상 진행)
            from shared_state import update_position
            update_position(code, {"overnight": False})
            print(f"    → 정상 (오버나이트 트랙 해제, 일반 감시 전환)")


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
    """
    15:20 강제 청산 — 종목별 오버나이트 종합 평가 후 분기.
    - 오버나이트 합격 (60점+) → 홀딩 (종가 기준 손절가 설정)
    - 오버나이트 불합격       → 시장가 매도
    """
    if not is_market_open_day():
        return
    positions = get_positions()
    if not positions:
        logger.info("15:20 강제 청산 — 보유 포지션 없음")
        return

    print(f"\n{'─'*40}")
    print("15:20 장마감 판단 (청산 or 오버나이트)")
    print("─" * 40)

    from tools.scanner_tools import evaluate_overnight

    # 종목별 OHLCV 데이터 로드 시도
    def _load_ohlcv(code):
        try:
            import yfinance as yf
            ticker = yf.Ticker(f"{code}.KS")
            df = ticker.history(period="3mo")
            if df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as e:
            logger.debug(f"main.py: {type(e).__name__}: {e}")
            return None

    overnight_held = []  # 오버나이트 홀딩 종목
    closed = []          # 청산 종목

    for code, data in list(positions.items()):
        entry_price = data.get("entry_price", data.get("avg_price", 0))
        current_price = data.get("current_price", 0)

        # 잔고에서 현재가 갱신 시도
        if current_price <= 0:
            try:
                balance = get_balance()
                for pos in balance.get("positions", []):
                    if pos["code"] == code:
                        current_price = pos["current_price"]
                        break
            except Exception as e:
                logger.debug(f"main.py: {type(e).__name__}: {e}")
                pass

        # OHLCV 로드 + 오버나이트 종합 평가
        df = _load_ohlcv(code)
        eval_result = evaluate_overnight(code, entry_price, current_price, df)

        hold = eval_result["hold"]
        score = eval_result["score"]
        grade = eval_result["grade"]
        bd = eval_result["breakdown"]

        print(f"\n  [{code}] 오버나이트 평가: {score}점 (등급 {grade})")
        print(f"    수익률: {bd['profit']['score']}/{bd['profit']['max']} — {bd['profit']['reason']}")
        print(f"    뉴  스: {bd['news']['score']}/{bd['news']['max']} — {bd['news']['reason']}")
        print(f"    추  세: {bd['trend']['score']}/{bd['trend']['max']} — {bd['trend']['reason']}")
        print(f"    거래량: {bd['volume']['score']}/{bd['volume']['max']} — {bd['volume']['reason']}")

        if hold:
            # ── 오버나이트 홀딩 ──
            closing_price = eval_result["closing_price"]
            stop_loss = eval_result["stop_loss"]

            # 포지션에 오버나이트 정보 기록
            update_position(code, {
                "overnight": True,
                "overnight_score": score,
                "overnight_grade": grade,
                "closing_price": closing_price,
                "overnight_stop": stop_loss,
                "overnight_date": datetime.now(KST).strftime("%Y-%m-%d"),
            })
            overnight_held.append(code)

            print(f"    → 오버나이트 홀딩 (종가 {closing_price:,.0f}원 / 손절 {stop_loss:,.0f}원)")

            try:
                notify_trade_decision(
                    "OVERNIGHT_HOLD", code,
                    data.get("entry_pct", 0), grade,
                    "오버나이트", f"종합 {score}점 | 종가 {closing_price:,.0f} | 손절 {stop_loss:,.0f}",
                )
            except Exception as e:
                logger.debug(f"main.py: {type(e).__name__}: {e}")
                pass

        else:
            # ── 강제 청산 ──
            try:
                result = sell_market(code, qty=0)
                log_trade("FORCE_CLOSE", code,
                          reason=f"15:20 청산 (오버나이트 {score}점, 등급 {grade})",
                          position_pct=data.get("entry_pct", 0))
                remove_position(code)
                closed.append(code)

                status = "성공" if result.get("success") else "실패"
                print(f"    → 강제 청산 {status}")

                try:
                    notify_trade_decision(
                        "FORCE_CLOSE", code,
                        data.get("entry_pct", 0), grade,
                        "강제청산", f"오버나이트 불합격 ({score}점)",
                    )
                except Exception as e:
                    logger.debug(f"main.py: {type(e).__name__}: {e}")
                    pass

            except Exception as e:
                logger.error(f"강제 청산 실패 ({code}): {e}", exc_info=True)

    # 요약
    print(f"\n  {'─'*30}")
    print(f"  청산: {len(closed)}종목  |  오버나이트 홀딩: {len(overnight_held)}종목")
    if overnight_held:
        print(f"  홀딩 종목: {', '.join(overnight_held)}")


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

        # LLM 비용 일일 요약 + 리셋
        from tools.cost_tracker import get_cost_tracker
        ct = get_cost_tracker()
        summary = ct.daily_summary()
        if summary["total_calls"] > 0:
            print(f"  💰 LLM 비용: ${summary['total_cost_usd']:.4f} "
                  f"({summary['total_calls']}건)")
            for model, info in summary["by_model"].items():
                print(f"     {model}: {info['calls']}건 ${info['cost_usd']:.4f}")
        ct.reset()
    except Exception as e:
        logger.error(f"일일 리포트 생성 실패: {e}", exc_info=True)


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
        logger.error(f"주별 대시보드 실패: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════
#  스케줄러 모드 (기본)
# ══════════════════════════════════════════════════════════════

async def job_hourly_news_scan():
    """
    시간별 뉴스 스캔 (매시간 00분, 24시간 운영).
    4개 소스(연합/Reuters/Google/네이버)에서 뉴스를 수집하여
    24시간 롤링 버퍼에 축적하고 트렌드를 분석한다.
    긴급도 변화 시에만 텔레그램 알림 + LLM 재분석 트리거.
    """
    try:
        from tools.macro_news_monitor import run_hourly_news_scan
        from shared_state import update_news_state

        # 뉴스 수집 + 분석
        result = run_hourly_news_scan()

        # shared_state 업데이트
        update_news_state(
            urgency=result["urgency"],
            changed=result["urgency_changed"],
            trend_windows=result.get("trend_windows", {}),
            narrative=result["trend_narrative"],
            urgent_items=result.get("urgent_items", []),
            total_articles=result["total_in_buffer"],
        )

        now_str = datetime.now(KST).strftime("%H:%M")
        logger.info(
            f"[뉴스스캔 {now_str}] 신규 {result['total_new']}건 / "
            f"총 {result['total_in_buffer']}건 / 긴급도 {result['urgency']}"
        )

        # ── 긴급도 변화 시 텔레그램 알림 ──
        if result["urgency_changed"] and result["urgency"] != "NONE":
            try:
                from tools.notifier_tools import send_telegram
                urgent_text = (
                    f"⚠️ 뉴스 긴급도 변화: {result['urgency']}\n"
                    f"트렌드: {result['trend_narrative']}\n"
                )
                if result.get("urgent_items"):
                    urgent_text += "주요 기사:\n"
                    for item in result["urgent_items"][:3]:
                        urgent_text += f"  • [{item['score']}점] {item['title'][:50]}\n"
                send_telegram(urgent_text)
            except Exception as ntf_err:
                logger.warning(f"뉴스 알림 전송 실패: {ntf_err}")

        # ── HIGH/CRITICAL 급상승 시 장중 재분석 트리거 ──
        if (result["urgency_changed"]
                and result["urgency"] in ("HIGH", "CRITICAL")
                and is_market_open_day()):
            # 장중 시간(09:00~15:20)에만 재분석 트리거
            now = datetime.now(KST)
            if 9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 20):
                try:
                    from agents.macro_analyst import run_intraday_reanalysis
                    logger.info(f"⚡ 긴급도 {result['urgency']} — 장중 재분석 트리거")
                    await run_intraday_reanalysis()
                except ImportError:
                    logger.warning("run_intraday_reanalysis 임포트 불가 — 재분석 스킵")
                except Exception as e:
                    logger.error(f"장중 재분석 실패: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"시간별 뉴스 스캔 실패: {e}", exc_info=True)
        try:
            from tools.notifier_tools import notify_error
            notify_error("hourly_news_scan", str(e))
        except Exception:
            pass


async def job_fred_release_setup():
    """06:00 — 오늘 FRED 발표 일정 조회 후 동적 Job 등록"""
    if not is_market_open_day():
        # 미국 발표는 한국 비개장일에도 수집 (야간 발표 대부분)
        pass
    try:
        from data_collector.macro.fred_release_scheduler import setup_daily_fred_jobs
        count = await setup_daily_fred_jobs(_scheduler_ref)
        logger.info(f"FRED 발표 Job {count}개 등록")
    except Exception as e:
        logger.error(f"FRED 발표 일정 설정 실패: {e}", exc_info=True)


async def job_fred_weekly_preview():
    """월요일 09:00 — 이번 주 FRED 발표 일정 텔레그램 알림"""
    now = datetime.now(KST)
    if now.weekday() != 0:  # 월요일만
        return
    try:
        from data_collector.macro.fred_release_scheduler import send_weekly_schedule_preview
        await send_weekly_schedule_preview()
    except Exception as e:
        logger.error(f"FRED 주간 일정 알림 실패: {e}", exc_info=True)


# 스케줄러 전역 참조 (fred_release_setup에서 동적 Job 등록용)
_scheduler_ref = None


async def run_scheduler():
    """APScheduler 기반 24시간 상시 운영 모드"""
    global _scheduler_ref
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    print("\n" + "=" * 60)
    print("  QUANTUM FLOW — 스케줄러 모드 (24시간 상시 운영)")
    print("=" * 60)

    if not check_env():
        return

    _load_agents()

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    _scheduler_ref = scheduler  # fred_release_setup에서 동적 Job 등록용

    # ── 24시간 뉴스 모니터링 (매시간 00분) ────────────────────
    for hour in range(24):
        scheduler.add_job(
            job_hourly_news_scan,
            CronTrigger(hour=hour, minute=0),
            id=f"news_scan_{hour:02d}",
            name=f"뉴스스캔 {hour:02d}:00",
        )

    # ── 일일 스케줄 ──────────────────────────────────────────
    # 토큰 갱신
    scheduler.add_job(job_refresh_token, CronTrigger(hour=5, minute=50),
                      id="token_refresh_morning", name="토큰 갱신 (아침)")
    scheduler.add_job(job_refresh_token, CronTrigger(hour=23, minute=0),
                      id="token_refresh_night", name="토큰 갱신 (야간)")

    # FRED 발표 일정 기반 동적 수집 (06:00 조회 → 발표 직후 자동 수집)
    scheduler.add_job(job_fred_release_setup, CronTrigger(hour=6, minute=0),
                      id="fred_release_setup", name="FRED 발표 일정 설정")
    # 월요일 09:00 이번 주 FRED 발표 미리보기
    scheduler.add_job(job_fred_weekly_preview, CronTrigger(hour=9, minute=0),
                      id="fred_weekly_preview", name="FRED 주간 일정 알림")

    # Agent 1: 거시경제 분석
    scheduler.add_job(job_macro_analysis, CronTrigger(hour=8, minute=30),
                      id="macro_analysis", name="Agent1 거시분석")

    # Agent 2: 종목 스캔 (08:50 + 11:30)
    scheduler.add_job(job_market_scan, CronTrigger(hour=8, minute=50),
                      id="market_scan_1", name="Agent2 종목스캔 1차")
    scheduler.add_job(job_market_scan, CronTrigger(hour=11, minute=30),
                      id="market_scan_2", name="Agent2 종목스캔 2차")

    # 오버나이트 포지션 손절 체크 (09:02, 장 시작 직후)
    scheduler.add_job(job_overnight_check, CronTrigger(hour=9, minute=2),
                      id="overnight_check", name="오버나이트 손절체크")

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
            logger.error(f"시장 감시 실패: {e}", exc_info=True)
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
            logger.error(f"MarketWatcher 주기 오류: {e}", exc_info=True)
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
