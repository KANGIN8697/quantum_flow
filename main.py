# main.py — QUANTUM FLOW 메인 실행 파일 (Phase 9)
# asyncio KST 스케줄러 + 전체 에이전트 파이프라인 통합
# 실행: python main.py [--dry-run] [--paper] [--real]

import os
import sys
import json
import asyncio
import logging
import argparse
import traceback
from datetime import datetime, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── 공통 상수 ─────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")
_TODAY = date.today().strftime("%Y%m%d")

# ── 로그 설정 ─────────────────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "reports")
os.makedirs(_LOG_DIR, exist_ok=True)

_LOG_FILE = os.path.join(_LOG_DIR, f"{_TODAY}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("quantum_flow")


# ── 배너 ──────────────────────────────────────────────────────

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     Q  U  A  N  T  U  M            F  L  O  W   —  AI 한국 주식 자동매매 시스템     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""


# ── 환경변수 검증 ────────────────────────────────────────────

def check_env(use_paper: bool) -> bool:
    """
    필수 환경변수가 설정되어 있는지 확인한다.

    Parameters
    ----------
    use_paper : bool
        True → 모의투자 키 확인, False → 실전투자 키 확인

    Returns
    -------
    bool : 모든 필수 항목이 설정되어 있으면 True
    """
    missing = []

    if use_paper:
        if not os.getenv("KIS_PAPER_APP_KEY"):
            missing.append("KIS_PAPER_APP_KEY")
        if not os.getenv("KIS_PAPER_APP_SECRET"):
            missing.append("KIS_PAPER_APP_SECRET")
        if not os.getenv("KIS_PAPER_ACCOUNT"):
            missing.append("KIS_PAPER_ACCOUNT  (예: 50123456-01)")
    else:
        if not os.getenv("KIS_APP_KEY"):
            missing.append("KIS_APP_KEY")
        if not os.getenv("KIS_APP_SECRET"):
            missing.append("KIS_APP_SECRET")
        if not os.getenv("KIS_ACCOUNT"):
            missing.append("KIS_ACCOUNT  (예: 12345678-01)")

    # OpenAI — 선택(없으면 기본값 사용)
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("⚠️  OPENAI_API_KEY 미설정 — LLM 기능 비활성화 (기본값 사용)")

    # Telegram — 선택(없으면 알림 비활성화)
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        logger.warning("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 알림 비활성화")

    if missing:
        logger.error("❌ 필수 환경변수 누락:")
        for m in missing:
            logger.error(f"   • {m}")
        logger.error("   .env 파일에 위 항목을 입력한 뒤 재실행하세요.")
        return False

    mode = "모의투자" if use_paper else "실전투자"
    logger.info(f"✅ 환경변수 검증 완료 [{mode}]")
    return True


# ── 시간 유틸리티 ─────────────────────────────────────────────

async def wait_until(target_hhmm: str, log: logging.Logger = logger):
    """
    KST 기준 target_hhmm (예: "09:10") 까지 비동기 대기한다.
    이미 지난 시각이면 즉시 리턴.

    Parameters
    ----------
    target_hhmm : str  예) "09:10", "15:20"
    log         : logging.Logger
    """
    hh, mm = map(int, target_hhmm.split(":"))
    now = datetime.now(KST)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    diff = (target - now).total_seconds()
    if diff <= 0:
        log.info(f"  ⏩ [{target_hhmm}] 이미 지난 시각 — 즉시 진행")
        return

    log.info(f"  ⏳ [{target_hhmm}] 까지 대기 중... ({diff/60:.1f}분 남음)")
    await asyncio.sleep(diff)
    log.info(f"  🕐 [{target_hhmm}] 도달 — 다음 단계 진행")


def now_kst_str() -> str:
    """현재 KST 시간을 'HH:MM:SS' 형식으로 반환."""
    return datetime.now(KST).strftime("%H:%M:%S")


# ── 일별 결산 보고 ────────────────────────────────────────────

async def send_end_of_day_report(log: logging.Logger = logger):
    """
    orders_YYYYMMDD.json을 읽어 일별 손익 통계를 계산하고
    Telegram 으로 전송한다.
    """
    try:
        from tools.notifier_tools import notify_daily_report
    except ImportError:
        log.warning("  notifier_tools 로드 실패 — 결산 보고 건너뜀")
        return

    orders_file = os.path.join(
        _LOG_DIR, f"orders_{_TODAY}.json"
    )

    if not os.path.exists(orders_file):
        log.warning(f"  ⚠️  주문 파일 없음: {orders_file}")
        await asyncio.get_event_loop().run_in_executor(
            None,
            notify_daily_report,
            {"message": "오늘 체결된 주문 없음 또는 파일 미생성"},
        )
        return

    try:
        with open(orders_file, "r", encoding="utf-8") as f:
            orders = json.load(f)
    except Exception as e:
        log.error(f"  ❌ 주문 파일 읽기 오류: {e}")
        return

    # 통계 계산
    total_trades = len(orders)
    buy_orders   = [o for o in orders if o.get("side") == "BUY"]
    sell_orders  = [o for o in orders if o.get("side") in ("SELL", "SELL_IOC")]

    # 손익 계산: sell 주문에 profit_krw 필드가 있으면 합산
    profits      = [o.get("profit_krw", 0) for o in sell_orders if o.get("profit_krw") is not None]
    total_pnl    = sum(profits) if profits else 0
    win_count    = sum(1 for p in profits if p > 0)
    lose_count   = sum(1 for p in profits if p <= 0)
    win_rate     = (win_count / len(profits) * 100) if profits else 0.0

    # 거시 분석 요약 로드
    macro_summary = ""
    macro_file = os.path.join(_LOG_DIR, f"macro_{_TODAY}.json")
    if os.path.exists(macro_file):
        try:
            with open(macro_file, "r", encoding="utf-8") as f:
                macro_data = json.load(f)
            risk = macro_data.get("analysis", {}).get("risk", "?")
            reason = macro_data.get("analysis", {}).get("reason", "")[:80]
            macro_summary = f"Risk-{risk} | {reason}"
        except Exception:
            pass

    report = {
        "date":          _TODAY,
        "total_trades":  total_trades,
        "buy_count":     len(buy_orders),
        "sell_count":    len(sell_orders),
        "total_pnl_krw": total_pnl,
        "win_count":     win_count,
        "lose_count":    lose_count,
        "win_rate_pct":  round(win_rate, 1),
        "macro_summary": macro_summary,
    }

    log.info("=" * 55)
    log.info(f"  📊 일별 결산 보고 [{_TODAY}]")
    log.info(f"  총 거래: {total_trades}건  (매수 {len(buy_orders)} / 매도 {len(sell_orders)})")
    log.info(f"  총 손익: {total_pnl:+,}원")
    log.info(f"  승률: {win_rate:.1f}% ({win_count}승 {lose_count}패)")
    if macro_summary:
        log.info(f"  거시: {macro_summary}")
    log.info("=" * 55)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, notify_daily_report, report)
        log.info("  ✅ Telegram 결산 보고 전송 완료")
    except Exception as e:
        log.error(f"  ⚠️  Telegram 전송 실패: {e}")


# ── 재스캔 스케줄 (11:30) ─────────────────────────────────────

async def scheduled_rescan(log: logging.Logger = logger):
    """
    11:30 에 MarketScanner 를 2차 실행하여 watch_list 를 갱신한다.
    HeadStrategist 와 asyncio.gather 로 병렬 실행된다.
    """
    try:
        await wait_until("11:30", log)
        log.info("\n  🔄 [11:30] 2차 스캔 시작...")
        try:
            from agents.market_scanner import run_scanner
            watch_list = await run_scanner(round_label="2차")
            log.info(f"  ✅ 2차 스캔 완료: {len(watch_list)}개 종목 선정")
        except Exception as e:
            log.error(f"  ❌ 2차 스캔 오류: {e}")
            traceback.print_exc()
    except asyncio.CancelledError:
        log.info("  scheduled_rescan: CancelledError 수신 — 종료")


# ── 메인 파이프라인 ───────────────────────────────────────────

async def main(dry_run: bool = False):
    """
    QUANTUM FLOW 메인 파이프라인.

    KST 기준 스케줄:
      06:00  거시 분석 (macro_analyst)
      08:30  1차 스캔 (market_scanner) + MarketWatcher 시작
      09:10  HeadStrategist 매매 시작 (→ 15:20 자동 종료)
      11:30  2차 스캔 (HeadStrategist 와 병렬)
      15:20↑ HeadStrategist 강제 청산 후 종료
      이후   일별 결산 보고 전송

    Parameters
    ----------
    dry_run : bool
        True → 실제 주문 없이 로그만 출력
    """
    use_paper = os.getenv("USE_PAPER", "true").lower() == "true"
    mode_str  = "모의투자" if use_paper else "실전투자"
    dry_str   = " [DRY-RUN]" if dry_run else ""

    logger.info("=" * 60)
    logger.info(f"  QUANTUM FLOW 시작  — {mode_str}{dry_str}")
    logger.info(f"  날짜: {_TODAY}  |  KST: {now_kst_str()}")
    logger.info("=" * 60)

    # ── 환경변수 검증 ────────────────────────────────────────
    if not check_env(use_paper):
        logger.error("❌ 필수 환경변수 미설정 — 프로그램 종료")
        sys.exit(1)

    # ── 에이전트 임포트 ──────────────────────────────────────
    try:
        from agents.macro_analyst   import run_macro_analysis
        from agents.market_scanner  import run_scanner
        from agents.market_watcher  import MarketWatcher
        from agents.head_strategist import HeadStrategist
        from shared_state import get_state
    except ImportError as e:
        logger.error(f"❌ 모듈 임포트 오류: {e}")
        logger.error("   프로젝트 루트에서 python main.py 를 실행하세요.")
        sys.exit(1)

    # ══════════════════════════════════════════════════════════
    # STEP 1 — 거시 분석 (06:00 KST)
    # ══════════════════════════════════════════════════════════
    await wait_until("06:00", logger)
    logger.info(f"\n{'='*55}")
    logger.info(f"  [06:00] STEP 1 — 거시경제 분석 시작")
    logger.info(f"{'='*55}")

    macro_result = {}
    try:
        macro_result = await run_macro_analysis()
        risk = macro_result.get("analysis", {}).get("risk", "ON")

        if risk == "OFF":
            reason = macro_result.get("analysis", {}).get("reason", "")
            logger.warning(f"\n  🔴 거시 Risk-OFF 감지!")
            logger.warning(f"  근거: {reason}")
            logger.warning(f"  오늘 신규 매수를 제한하고 보수적으로 운영합니다.")
            # Risk-OFF 라도 기존 포지션 관리는 계속 진행
    except Exception as e:
        logger.error(f"  ❌ 거시 분석 오류: {e} → Risk-ON 기본값 사용")
        traceback.print_exc()

    # ══════════════════════════════════════════════════════════
    # STEP 2 — 1차 스캔 + MarketWatcher 시작 (08:30 KST)
    # ══════════════════════════════════════════════════════════
    await wait_until("08:30", logger)
    logger.info(f"\n{'='*55}")
    logger.info(f"  [08:30] STEP 2 — 1차 종목 스캔 시작")
    logger.info(f"{'='*55}")

    watch_list = []
    try:
        watch_list = await run_scanner(round_label="1차")
        logger.info(f"  ✅ 1차 스캔 완료: {len(watch_list)}개 종목")
    except Exception as e:
        logger.error(f"  ❌ 1차 스캔 오류: {e} → 기본 풀 사용")
        traceback.print_exc()

    # MarketWatcher 시작 (백그라운드)
    watcher = MarketWatcher()
    watcher_task = asyncio.create_task(watcher.run())
    logger.info("  ✅ MarketWatcher 백그라운드 시작")

    # ══════════════════════════════════════════════════════════
    # STEP 3 — HeadStrategist 준비 (09:05 KST)
    # ══════════════════════════════════════════════════════════
    await wait_until("09:05", logger)
    logger.info(f"\n{'='*55}")
    logger.info(f"  [09:05] STEP 3 — HeadStrategist 초기화")
    logger.info(f"{'='*55}")

    strategist = HeadStrategist(
        tick_interval=1.5,
        dry_run=dry_run,
    )

    # watch_list 주입 (scanner 결과 → shared_state 에도 저장됨)
    try:
        current_wl = get_state("watch_list") orwatch_list
        strategist.watch_list = list(current_wl)
        logger.info(f"  📋 감시 종목 {len(strategist.watch_list)}개 주입 완료")
    except Exception as e:
        logger.warning(f"  ⚠️  watch_list 주입 실패: {e}")

    # ══════════════════════════════════════════════════════════
    # STEP 4 — HeadStrategist 실행 + 2차 스캔 병렬 (09:10~15:20)
    # ══════════════════════════════════════════════════════════
    logger.info(f"\n{'='*55}")
    logger.info(f"  [09:10] STEP 4 — 매매 시작 (15:20 까지)")
    logger.info(f"  dry_run={dry_run}  |  감시종목: {len(strategist.watch_list)}개")
    logger.info(f"{'='*55}")

    rescan_task = asyncio.create_task(scheduled_rescan(logger))

    try:
        await asyncio.gather(
            strategist.run(),
            rescan_task,
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"  ❌ 매매 루프 예외: {e}")
        traceback.print_exc()
    finally:
        rescan_task.cancel()
        logger.info("  매매 루프 종료")

    # ══════════════════════════════════════════════════════════
    # STEP 5 — MarketWatcher 중지
    # ══════════════════════════════════════════════════════════
    logger.info(f"\n{'='*55}")
    logger.info(f"  [15:20+] STEP 5 — MarketWatcher 중지")
    logger.info(f"{'='*55}")
    try:
        watcher.stop()
        watcher_task.cancel()
        await asyncio.sleep(1)
        logger.info("  ✅ MarketWatcher 중지 완료")
    except Exception as e:
        logger.warning(f"  ⚠️  MarketWatcher 중지 오류: {e}")

    # ══════════════════════════════════════════════════════════
    # STEP 6 — 일별 결산 보고
    # ══════════════════════════════════════════════════════════
    logger.info(f"\n{'='*55}")
    logger.info(f"  STEP 6 — 일별 결산 보고")
    logger.info(f"{'='*55}")
    await send_end_of_day_report(logger)

    logger.info("\n" + "=" * 60)
    logger.info(f"  ✅ QUANTUM FLOW 정상 종료  KST: {now_kst_str()}")
    logger.info("=" * 60)


# ── CLI 진입점 ─────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="QUANTUM FLOW — AI 한국 주식 자동매매 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py                   # 모의투자 모드 (USE_PAPER=true)
  python main.py --dry-run         # dry-run (주문 없이 로그만)
  python main.py --real            # 실전투자 모드 강제 지정
  python main.py --paper --dry-run # 모의투자 + dry-run
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 주문을 보내지 않고 로그만 출력 (테스트용)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="모의투자 모드 강제 지정 (USE_PAPER=true 와 동일)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="실전투자 모드 강제 지정 (주의: 실제 자금 사용)",
    )
    return parser.parse_args()


# ── 단독 실행 테스트 ────────────────────────────────────────────
if __name__ == "__main__":
    args = parse_args()

    # --paper / --real 플래그로 환경변수 오버라이드
    if args.paper:
        os.environ["USE_PAPER"] = "true"
    elif args.real:
        print("\n⚠️  실전투자 모드입니다. 실제 자금이 사용됩니다.")
        confirm = input("계속하시겠습니까? (yes 입�%): ").strip().lower()
        if confirm != "yes":
            print("취소되었습니다.")
            sys.exit(0)
        os.environ["USE_PAPER"] = "false"

    print(BANNER)
    print(f"  시작 시각 (KST): {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  모드: {'모의투자' if os.getenv('USE_PAPER','true').lower()=='true' else '실전투자'}")
    print(f"  dry-run: {args.dry_run}")
    print()

    try:
        asyncio.run(main(dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\n\n  👋 QUANTUM FLOW 사용자 중단 (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        print(f"\n  ❌ 치명적 오류: {e}")
        traceback.print_exc()
        sys.exit(1)
