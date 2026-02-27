# tasks/watcher_task.py — 시장 감시 태스크 (Agent 4)
# MarketWatcher + KISWebSocketFeeder를 연결하여 실행

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def run_watcher_task():
    """
    Agent 4 감시 태스크:
    1. KISWebSocketFeeder 생성 + 종목 구독
    2. MarketWatcher에 체결강도 콜백 연결
    3. 웹소켓 listen + 감시 루프 동시 실행
    """
    from agents.market_watcher import MarketWatcher
    from tools.websocket_feeder import KISWebSocketFeeder
    from shared_state import get_positions, get_state

    print(f"\n{'='*55}")
    print(f"  Agent 4 — 시장 감시 + 웹소켓 피더 시작")
    print(f"  시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    # ── 감시 대상 종목 수집 (보유 + 감시리스트) ────────────────
    positions = get_positions()
    watch_list = get_state("watch_list") or []

    stock_codes = set(positions.keys())
    for item in watch_list:
        if isinstance(item, dict):
            stock_codes.add(item.get("code", ""))
        elif isinstance(item, str):
            stock_codes.add(item)
    stock_codes.discard("")

    # 최소 삼성전자는 포함 (빈 리스트 방지)
    if not stock_codes:
        stock_codes = {"005930"}
    stock_codes = list(stock_codes)

    print(f"  📡 웹소켓 구독 종목: {stock_codes}")

    # ── MarketWatcher 생성 ─────────────────────────────────────
    watcher = MarketWatcher(check_interval=60)

    # ── WebSocketFeeder 생성 + 콜백 연결 ──────────────────────
    feeder = KISWebSocketFeeder(stock_codes=stock_codes)
    watcher.attach_ws_feeder(feeder)

    # ── 웹소켓 연결 + 동시 실행 ────────────────────────────────
    try:
        await feeder.connect()

        # 웹소켓 listen과 감시 루프를 동시에 실행
        ws_task = asyncio.create_task(feeder.listen())
        watcher_task = asyncio.create_task(
            asyncio.get_running_loop().run_in_executor(
                None, _watcher_blocking_loop, watcher
            )
        )

        # 둘 중 하나가 끝나면 다른 것도 정리
        done, pending = await asyncio.wait(
            [ws_task, watcher_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"run_watcher_task 오류: {e}")
        print(f"  ❌ Agent 4 오류: {e}")
    finally:
        await feeder.stop()
        watcher.stop()
        print(f"  🛑 Agent 4 종료")


def _watcher_blocking_loop(watcher):
    """executor 내에서 실행되는 블로킹 감시 루프"""
    import time
    from agents.market_watcher import MODE_LABEL

    watcher._running = True
    while watcher._running:
        try:
            watcher.check_cycle()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  ❌ [MarketWatcher] 주기 오류: {e}")
        time.sleep(watcher.check_interval)
    print(f"  🛑 [{MODE_LABEL}] MarketWatcher 루프 종료")
