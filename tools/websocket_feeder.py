# tools/websocket_feeder.py — KIS 웹소켓 실시간 체결가 + 호가 수신기
# Phase 3 구현: 실시간 수신, 틱 속도 계산, 자동 재연결 (최대 3회)

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime

import websockets
from dotenv import load_dotenv

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


load_dotenv()

USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"

# ── 웹소켓 URL ─────────────────────────────────
if USE_PAPER:
    WS_URL = "ws://ops.koreainvestment.com:31000/tryitout/H0STCNT0"
    MODE_LABEL = "모의투자"
else:
    WS_URL = "ws://ops.koreainvestment.com:21000/tryitout/H0STCNT0"
    MODE_LABEL = "실전투자"

TR_TICK  = "H0STCNT0"
TR_QUOTE = "H0STASP0"
MAX_RECONNECT = 3
RECONNECT_DELAY = 1


class KISWebSocketFeeder:
    """
    KIS 웹소켓으로 실시간 체결가 + 호가를 수신하는 피더.
    자동 재연결 및 틱 속도 계산 기능 포함.
    """

    def __init__(self, stock_codes: list):
        self.stock_codes = stock_codes
        self.ws = None
        self.approval_key = None
        self._running = False
        self._reconnect_count = 0
        self._prices: dict = {}
        self._quotes: dict = {}
        self._tick_timestamps: dict = {
            code: deque(maxlen=100) for code in stock_codes
        }

    async def connect(self):
        from tools.token_manager import get_websocket_approval_key
        print(f"🔌 [{MODE_LABEL}] 웹소켓 연결 중... {WS_URL}")
        self.approval_key = get_websocket_approval_key()
        self.ws = await websockets.connect(WS_URL, ping_interval=20, ping_timeout=10)
        self._reconnect_count = 0
        print(f"✅ [{MODE_LABEL}] 웹소켓 연결 성공")
        for code in self.stock_codes:
            await self.subscribe(code, TR_TICK)
            await self.subscribe(code, TR_QUOTE)

    async def subscribe(self, code: str, tr_type: str):
        msg = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_type, "tr_key": code}}
        }
        await self.ws.send(json.dumps(msg))
        label = "체결가" if tr_type == TR_TICK else "호가"
        print(f"  📡 구독 등록: {code} [{label}]")

    async def on_message(self, message: str):
        if message.startswith("PINGPONG"):
            await self.ws.send("PONGPING")
            return
        if message.startswith("{"):
            try:
                data = json.loads(message)
                msg1 = data.get("body", {}).get("msg1", "")
                if msg1:
                    print(f"  ✉️  시스템 메시지: {msg1}")
            except json.JSONDecodeError:
                pass
            return
        parts = message.split("|")
        if len(parts) < 4:
            return
        tr_id = parts[1]
        fields = parts[3].split("^")
        if tr_id == TR_TICK and len(fields) >= 3:
            code = fields[0]
            price = fields[2]
            volume = fields[9] if len(fields) > 9 else "0"
            now = time.time()
            self._prices[code] = {
                "price": int(price), "volume": int(volume),
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            if code in self._tick_timestamps:
                self._tick_timestamps[code].append(now)
            print(
                f"  💹 [{code}] 체결가: {int(price):,}원  "
                f"거래량: {int(volume):,}  "
                f"틱속도: {self.get_tick_speed(code):.1f}/초  "
                f"{datetime.now().strftime('%H:%M:%S')}"
            )
        elif tr_id == TR_QUOTE and len(fields) >= 14:
            code = fields[0]
            self._quotes[code] = {
                "ask1": int(fields[3])  if fields[3].isdigit()  else 0,
                "bid1": int(fields[13]) if fields[13].isdigit() else 0,
                "time": datetime.now().strftime("%H:%M:%S"),
            }

    async def listen(self):
        self._running = True
        try:
            async for message in self.ws:
                if not self._running:
                    break
                await self.on_message(message)
        except websockets.exceptions.ConnectionClosed:
            print(f"⚠️  [{MODE_LABEL}] 웹소켓 연결 끊김 감지")
            if self._running:
                await self.reconnect()

    async def reconnect(self):
        while self._reconnect_count < MAX_RECONNECT:
            self._reconnect_count += 1
            print(f"🔄 재연결 시도 {self._reconnect_count}/{MAX_RECONNECT}... ({RECONNECT_DELAY}초 후)")
            await asyncio.sleep(RECONNECT_DELAY)
            try:
                await self.connect()
                print(f"✅ 재연결 성공! {len(self.stock_codes)}개 종목 재구독 완료")
                await self.listen()
                return
            except Exception as e:
                print(f"❌ 재연결 실패 ({self._reconnect_count}회): {e}")
        print(f"🚨 재연결 {MAX_RECONNECT}회 모두 실패. 텔레그램 경보 발송 시도...")
        try:
            from tools.notifier_tools import send_alert
            send_alert(f"🚨 QUANTUM FLOW: 웹소켓 재연결 {MAX_RECONNECT}회 실패. 수동 확인 필요.")
        except Exception:
            print("  (notifier_tools 미구현 — Phase 5에서 연동 예정)")
        self._running = False

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        print(f"🛑 [{MODE_LABEL}] 웹소켓 수신 종료")

    def get_latest_price(self, code: str) -> dict:
        return self._prices.get(code, {})

    def get_latest_quote(self, code: str) -> dict:
        return self._quotes.get(code, {})

    def get_tick_speed(self, code: str) -> float:
        if code not in self._tick_timestamps:
            return 0.0
        now = time.time()
        recent = [t for t in self._tick_timestamps[code] if now - t <= 1.0]
        return safe_float(len(recent))


if __name__ == "__main__":

    async def test_run():
        print("=" * 55)
        print("  QUANTUM FLOW — 웹소켓 수신기 테스트")
        print(f"  모드: {MODE_LABEL}")
        print(f"  종목: 삼성전자(005930) | 수신 시간: 10초")
        print("=" * 55)
        try:
            from tools.token_manager import ensure_token
            ensure_token()
        except EnvironmentError as e:
            print(f"\n{e}")
            print("\n📁 websocket_feeder.py 구조 확인 완료 — API 키 입력 후 재실행하세요.")
            return
        feeder = KISWebSocketFeeder(stock_codes=["005930"])
        try:
            await feeder.connect()
            print(f"\n⏱  10초간 실시간 데이터 수신 중...\n")
            try:
                await asyncio.wait_for(feeder.listen(), timeout=10)
            except asyncio.TimeoutError:
                pass
            print("\n" + "=" * 55)
            price_data = feeder.get_latest_price("005930")
            quote_data = feeder.get_latest_quote("005930")
            if price_data:
                print(f"  최종 체결가: {price_data['price']:,}원  ({price_data['time']})")
            if quote_data:
                print(f"  매도1호가:   {quote_data['ask1']:,}원")
                print(f"  매수1호가:   {quote_data['bid1']:,}원")
            print(f"  틱 속도:     {feeder.get_tick_speed('005930'):.1f}/초")
            print("\n  ✅ 테스트 완료!")
            print("=" * 55)
        except Exception as e:
            print(f"\n❌ 오류: {e}")
            print("\n💡 API 키와 네트워크 연결을 확인하세요.")
        finally:
            await feeder.stop()

    asyncio.run(test_run())
