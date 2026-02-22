# QUANTUM FLOW â ë©ì¸ ì¤í ì§ìì 
# ì¤í: python main.py
#
# ì¤í ìì:
# 1) íê²½ë³ì íì¸
# 2) Agent 1: ê±°ìê²½ì  ë¶ì (macro_analyst)
# 3) Agent 2: ì¢ëª© ì¤ìº (market_scanner)
# 4) Agent 3: ì ëµ ê²°ì  (head_strategist)
# 5) Agent 4: ìì¥ ê°ì ë£¨í (market_watcher) â ì¥ì¤ ìì ì¤í

from dotenv import load_dotenv
import os
import asyncio
import logging
import sys

load_dotenv()

from tools.trade_logger import (
    set_macro_snapshot, log_risk_event, end_of_day_routine, get_daily_trades
)
from tools.dashboard_tools import (
    create_and_send_daily_dashboard, create_and_send_weekly_dashboard
)

# ë¡ê¹ ì¤ì 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def check_env():
    """íì íê²½ë³ì íì¸"""
    required = [
        "KIS_APP_KEY", "KIS_APP_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OPENAI_API_KEY",
    ]
    optional = ["FRED_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"â ëë½ë íì íê²½ë³ì: {missing}")
        print("   â Codespace Secrets ëë .env íì¼ìì ì¤ì íì¸ì")
        return False

    missing_opt = [k for k in optional if not os.getenv(k)]
    if missing_opt:
        print(f"â¹ï¸  ì íì  íê²½ë³ì ë¯¸ì¤ì  (ê¸°ë¥ ì í): {missing_opt}")

    print("â íê²½ë³ì íì¸ ìë£")
    return True


async def main():
    """ë©ì¸ ì¤ì¼ì¤í¸ë ì´ì í¨ì"""
    print("\n" + "=" * 60)
    print("  ð QUANTUM FLOW â AI ê¸°ë° íêµ­ ì£¼ì ìëë§¤ë§¤ ìì¤í")
    print("=" * 60)

    if not check_env():
        return

    # Agent imports (ìë¬ ì ê°ë³ ìì´ì í¸ ë¹íì±í)
    try:
        from agents.macro_analyst import macro_analyst_run
    except ImportError as e:
        logger.error(f"macro_analyst import ì¤í¨: {e}")
        macro_analyst_run = None

    try:
        from agents.market_scanner import market_scanner_run
    except ImportError as e:
        logger.error(f"market_scanner import ì¤í¨: {e}")
        market_scanner_run = None

    try:
        from agents.head_strategist import head_strategist_run
    except ImportError as e:
        logger.error(f"head_strategist import ì¤í¨: {e}")
        head_strategist_run = None

    try:
        from agents.market_watcher import market_watcher_run
    except ImportError as e:
        logger.error(f"market_watcher import ì¤í¨: {e}")
        market_watcher_run = None

    # ââ STEP 1: ê±°ìê²½ì  ë¶ì ââ
    print("\n" + "â" * 40)
    print("STEP 1: ê±°ìê²½ì  ë¶ì")
    print("â" * 40)
    if macro_analyst_run:
        try:
            macro_result = await macro_analyst_run()
            risk_status = macro_result.get("risk_status", "?")
            print(f"\n  â ê²°ê³¼: Risk-{risk_status}")
            # ë§¤í¬ë¡ ì¤ëì· ì ì¥ (ì¼ì¼ ë¶ìì©)
            set_macro_snapshot(macro_result)
        except Exception as e:
            logger.error(f"STEP 1 ì¤í¨: {e}")
            macro_result = None
    else:
        print("  â ï¸ macro_analyst ë¹íì±í")
        macro_result = None

    # Risk-OFFë©´ ì¢ë£
    from shared_state import get_state, get_positions
    if get_state("risk_off"):
        risk_params = get_state("risk_params") or {}
        log_risk_event("RISK_OFF", level="CRITICAL",
                       trigger="macro_analyst", message="ì¥ ìì ì  Risk-OFF íì ")
        if risk_params.get("emergency_liquidate"):
            print("\nð¨ CRITICAL: ê¸´ê¸ ì²­ì° ëª¨ë")
            if head_strategist_run:
                await head_strategist_run()
            # ê¸´ê¸ ì¢ë£ ììë ì¼ì¼ ë¦¬í¬í¸ ìì±
            result = end_of_day_routine(get_positions(), get_state("daily_loss") or 0.0)
            print(f"\nð ì¼ì¼ ë¦¬í¬í¸ ì ì¥: {result['filepath']}")
            print("\nð QUANTUM FLOW ê¸´ê¸ ì¢ë£")
            return

    # ââ STEP 2: ì¢ëª© ì¤ìº ââ
    print("\n" + "â" * 40)
    print("STEP 2: ì¢ëª© ì¤ìº")
    print("â" * 40)
    if market_scanner_run:
        try:
            scan_result = await market_scanner_run()
            candidates = scan_result.get("candidates", 0)
            print(f"\n  â ê²°ê³¼: {candidates}ì¢ëª© ê°ì ë±ë¡")
        except Exception as e:
            logger.error(f"STEP 2 ì¤í¨: {e}")
            scan_result = None
    else:
        print("  â ï¸ market_scanner ë¹íì±í")
        scan_result = None

    # ââ STEP 3: ì ëµ ê²°ì  ââ
    print("\n" + "â" * 40)
    print("STEP 3: ì ëµ ê²°ì ")
    print("â" * 40)
    if head_strategist_run:
        try:
            strategy_result = await head_strategist_run()
            actions = len(strategy_result.get("actions", []))
            print(f"\n  â ê²°ê³¼: {actions}ê±´ ë§ ë§¤ ê²°ì ")
        except Exception as e:
            logger.error(f"STEP 3 ì¤í¨: {e}")
            strategy_result = None
    else:
        print("  â ï¸ head_strategist ë¹íì±í")
        strategy_result = None

    # ââ STEP 4: ìì¥ ê°ì (ì¥ì¤ ë£¨í) ââ
    print("\n" + "â" * 40)
    print("STEP 4: ìì¥ ê°ì (Ctrl+Cë¡ ì¢ë£)")
    print("â" * 40)
    if market_watcher_run:
        try:
            await market_watcher_run()
        except KeyboardInterrupt:
            print("\nâ¹ï¸ ì¬ì©ì ì¢ë£ ìì²­")
        except Exception as e:
            logger.error(f"STEP 4 ì¤í¨: {e}")
    else:
        print("  â ï¸ market_watcher ë¹íì±í")

    # ââ ì¥ ë§ê°: ì¼ì¼ ë§¤ë§¤ ë¦¬í¬í¸ ìì± ââ
    print("\n" + "â" * 40)
    print("ì¥ ë§ê°: ì¼ì¼ ë§¤ë§¤ ë¦¬í¬í¸ ìì±")
    print("â" * 40)
    try:
        positions = get_positions()
        daily_loss = get_state("daily_loss") or 0.0
        result = end_of_day_routine(positions, daily_loss)
        perf = result["performance"]
        print(f"  ð ë¦¬í¬í¸ ì ì¥: {result['filepath']}")
        print(f"  ð ë§¤ë§¤ {perf.get('total_trades', 0)}ê±´ | "
              f"ì¹ë¥  {perf.get('win_rate', 0):.0%} | "
              f"ì¤íPnL {perf.get('realized_pnl', 0):+.2%}")

        # 일별 대시보드 이미지 생성 및 텔레그램 전송
        trades = get_daily_trades()
        create_and_send_daily_dashboard(perf, trades, positions)

        # 금요일이면 주별 대시보드도 전송
        from datetime import datetime as _dt
        if _dt.now().weekday() == 4:  # 금요일
            create_and_send_weekly_dashboard()


    except Exception as e:
        logger.error(f"ì¼ì¼ ë¦¬í¬í¸ ìì± ì¤í¨: {e}")

    print("\n" + "=" * 60)
    print("  ð QUANTUM FLOW ì¤í ìë£")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nâ¹ï¸ QUANTUM FLOW ì¢ë£")
        sys.exit(0)
