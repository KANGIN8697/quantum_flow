# agents/head_strategist.py — 수석 전략가 에이전트 (Agent 3)
# Phase 7: 순수 룰 기반 실시간 매수/청산 판단 + 주문 실행 총괄
# asyncio 기반, LLM 미개입
# dry_run=True 시 주문 없이 로그만 출력

import os
import asyncio
import logging
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

# ── config & 공유상태 ──────────────────────────────────────────
try:
    from config.settings import (
        MAX_POSITIONS, POSITION_SIZE_RATIO, PYRAMID_ADD_RATIO,
        DAILY_LOSS_LIMIT, MARKET_OPEN_HOLD, FORCE_CLOSE_TIME,
        NO_PYRAMID_AFTER, OVERNIGHT_THRESHOLD, OVERNIGHT_STOP_PCT,
        ATR_PERIOD, INITIAL_STOP_ATR, TRAILING_STOP_ATR,
    )
    from shared_state import (
        get_state, set_state, get_positions,
        add_position, remove_position, add_to_blacklist,
    )
    from tools.order_executor import buy_ioc, sell_market, sell_ioc, get_balance
    from tools.scanner_tools import (
        check_buy_signal, check_pyramid_signal,
        calc_atr, calc_stop_loss, calc_trailing_stop, calc_pyramid_stop,
        is_overnight_candidate,
    )
    from tools.notifier_tools import notify_buy, notify_sell, notify_error
except ImportError:
    # 독립 실행 시 기본값
    MAX_POSITIONS       = 5
    POSITION_SIZE_RATIO = 0.20
    PYRAMID_ADD_RATIO   = 0.30
    DAILY_LOSS_LIMIT    = -0.03
    MARKET_OPEN_HOLD    = "09:10"
    FORCE_CLOSE_TIME    = "15:20"
    NO_PYRAMID_AFTER    = "15:00"
    OVERNIGHT_THRESHOLD = 0.07
    OVERNIGHT_STOP_PCT  = -0.05
    ATR_PERIOD          = 14
    INITIAL_STOP_ATR    = 2.0
    TRAILING_STOP_ATR   = 3.0

    def get_state(k): return None
    def set_state(k, v): pass
    def get_positions(): return {}
    def add_position(c, d): pass
    def remove_position(c): pass
    def add_to_blacklist(c): pass
    def buy_ioc(c, q, p, **k): return {"success": False, "order_no": ""}
    def sell_market(c, q, **k): return {"success": False}
    def sell_ioc(c, q, p, **k): return {"success": False}
    def get_balance(): return {"cash": 0, "positions": [], "total_eval": 0}
    def check_buy_signal(*a, **k): return {"signal": False, "score": 0, "reason": "fallback"}
    def check_pyramid_signal(*a, **k): return False
    def calc_atr(df, **k): return 0.0
    def calc_stop_loss(e, a): return round(e * 0.96, 0)
    def calc_trailing_stop(h, a): return round(h * 0.94, 0)
    def calc_pyramid_stop(a): return round(a * 0.97, 0)
    def is_overnight_candidate(e, c): return False
    def notify_buy(*a, **k): pass
    def notify_sell(*a, **k): pass
    def notify_error(*a, **k): pass

USE_PAPER  = os.getenv("USE_PAPER", "true").lower() == "true"
MODE_LABEL = "모의투자" if USE_PAPER else "실전투자"


# ── 로그 설정 ──────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    """일별 파일 + 콘솔 로거를 생성한다."""
    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "outputs", "reports",
    )
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{date.today().strftime('%Y%m%d')}.log")

    logger = logging.getLogger("HeadStrategist")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%H:%M:%S")
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


# ── KIS 일봉 OHLCV 조회 (동기, executor에서 실행) ──────────────

def _fetch_ohlcv_sync(code: str, period: int = ATR_PERIOD + 10):
    """
    KIS API로 일봉 OHLCV를 조회한다.
    실패 시 None 반환.
    """
    import pandas as pd
    import requests as req

    try:
        from tools.token_manager import ensure_token
        from tools.order_executor import (
            BASE_URL, APP_KEY, APP_SECRET, ACNT_PRDT,
        )

        token = ensure_token()
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "Content-Type":  "application/json",
            "authorization": f"Bearer {token}",
            "appkey":        APP_KEY,
            "appsecret":     APP_SECRET,
            "tr_id":         "FHKST03010100",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         code,
            "FID_INPUT_DATE_1":       "",
            "FID_INPUT_DATE_2":       datetime.now().strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_ORG_ADJ_PRC":        "0",
        }
        resp = req.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        for item in (data.get("output2") or [])[:period]:
            rows.append({
                "open":   float(item.get("stck_oprc", 0) or 0),
                "high":   float(item.get("stck_hgpr", 0) or 0),
                "low":    float(item.get("stck_lwpr", 0) or 0),
                "close":  float(item.get("stck_clpr", 0) or 0),
                "volume": int(item.get("acml_vol", 0) or 0),
            })

        if len(rows) < 5:
            return None

        df = pd.DataFrame(rows[::-1])   # 오래된 데이터가 위로
        return df

    except Exception as e:
        return None


# ── 간단 Rate Limiter ─────────────────────────────────────────

class _RateLimiter:
    """KIS API 초당 최대 호출 횟수를 제한한다 (기본 18회/초)."""

    def __init__(self, max_per_second: int = 18):
        self._sem = asyncio.Semaphore(max_per_second)
        self._delay = 1.0 / max_per_second

    async def acquire(self):
        await self._sem.acquire()
        asyncio.get_event_loop().call_later(1.0, self._sem.release)


# ═══════════════════════════════════════════════════════════════
# HeadStrategist 클래스
# ═══════════════════════════════════════════════════════════════

class HeadStrategist:
    """
    룰 기반 실시간 매수/청산 판단 에이전트.

    - 09:10 ~ 15:20 사이에서만 작동
    - 매 tick_interval(초)마다 전체 포지션 점검 + 신호 처리
    - dry_run=True 면 주문 없이 로그만 출력
    """

    def __init__(self, tick_interval: float = 1.0, dry_run: bool = False):
        self.tick_interval = tick_interval
        self.dry_run       = dry_run
        self._running      = False
        self._executor     = ThreadPoolExecutor(max_workers=4,
                                                thread_name_prefix="strategist")
        self._logger       = _setup_logger()
        self._rate_limiter = _RateLimiter(max_per_second=18)

        # OHLCV 캐시: code → (DataFrame, cached_timestamp)
        self._ohlcv_cache: dict = {}
        self._ohlcv_ttl: int    = 300   # 5분

        mode_str = "[DRY RUN]" if dry_run else f"[{MODE_LABEL}]"
        self._logger.info(f"HeadStrategist 초기화 {mode_str} tick={tick_interval}s")

    # ── 실행 / 중지 ────────────────────────────────────────────

    async def run(self):
        """
        메인 루프를 시작한다.
        09:10 전이면 대기, 15:20 이후면 전 청산 후 종료.
        """
        if self._running:
            self._logger.warning("HeadStrategist 이미 실행 중")
            return

        self._running = True
        self._logger.info(f"HeadStrategist 루프 시작 (dry_run={self.dry_run})")

        try:
            while self._running:
                now_str = datetime.now().strftime("%H:%M")

                # 장 시작 전 — 대기
                if now_str < MARKET_OPEN_HOLD:
                    self._logger.debug(f"장 시작 대기 ({now_str} < {MARKET_OPEN_HOLD})")
                    await asyncio.sleep(10)
                    continue

                # 강제 청산 시간
                if now_str >= FORCE_CLOSE_TIME:
                    self._logger.info(f"[{FORCE_CLOSE_TIME}] 강제 청산 시작")
                    await self.force_close_all(reason=f"{FORCE_CLOSE_TIME} 장마감")
                    self._running = False
                    break

                # 정상 루프
                try:
                    await self._tick()
                except Exception as e:
                    self._logger.error(f"틱 오류: {e}", exc_info=True)
                    try:
                        notify_error("HeadStrategist._tick", str(e), MODE_LABEL)
                    except Exception:
                        pass

                await asyncio.sleep(self.tick_interval)

        finally:
            self._executor.shutdown(wait=False)
            self._logger.info("HeadStrategist 루프 종료")

    def stop(self):
        """루프를 중지한다."""
        self._running = False
        self._logger.info("HeadStrategist 중지 요청")

    # ── 메인 틱 ───────────────────────────────────────────────

    async def _tick(self):
        """매 틱 실행: 우선순위 순서로 처리."""

        # 1. Risk-Off 체크
        if get_state("risk_off"):
            self._logger.warning("Risk-Off 감지 → 전 포지션 청산")
            await self.force_close_all(reason="RISK_OFF")
            self._running = False
            return

        # 2. 일 손실 한도 체크
        daily_loss = get_state("daily_loss") or 0.0
        if daily_loss <= DAILY_LOSS_LIMIT:
            self._logger.warning(
                f"일 손실 한도 초과: {daily_loss:.2%} ≤ {DAILY_LOSS_LIMIT:.2%}"
            )
            await self.force_close_all(reason="DAILY_LOSS_LIMIT")
            self._running = False
            return

        # 3. 보유 포지션 점검 (손절 → 트레일링 → 피라미딩)
        positions = get_positions()
        for code, pos in list(positions.items()):
            await self.check_stop_loss(code, pos)
            # 손절 후 포지션이 사라졌을 수 있으므로 재확인
            if code not in get_positions():
                continue
            await self.check_trailing_stop(code, pos)
            if code not in get_positions():
                continue
            await self.check_pyramid(code, pos)

        # 4. 신규 매수 (포지션 여유 있을 때만)
        risk_params = get_state("risk_params") or {}
        macro_off   = get_state("macro_risk_off") or False
        if (len(get_positions()) < MAX_POSITIONS
                and not get_state("risk_off")
                and not macro_off):
            watch_list = get_state("watch_list") or []
            current    = set(get_positions().keys())
            blacklist  = set(get_state("blacklist") or [])
            for code in watch_list[:15]:
                if code in current or code in blacklist:
                    continue
                if len(get_positions()) >= MAX_POSITIONS:
                    break
                await self.check_buy_signal(code)

        # 5. 대기열 처리
        await self.process_queue()

    # ── 손절 ──────────────────────────────────────────────────

    async def check_stop_loss(self, code: str, position: dict):
        """
        손절 조건 확인.
        - 피라미딩 미완료: 진입가 - ATR × INITIAL_STOP_ATR
        - 피라미딩 완료:   평단 × (1 + PYRAMID_STOP_PCT)
        """
        current_price   = float(position.get("current_price") or 0)
        entry_price     = float(position.get("entry_price") or 0)
        avg_price       = float(position.get("avg_price") or entry_price)
        atr             = float(position.get("atr") or 0)
        pyramiding_done = position.get("pyramiding_done", False)

        if current_price <= 0:
            return

        if pyramiding_done:
            stop_price = calc_pyramid_stop(avg_price)
            reason_tag = "피라미딩손절"
        else:
            stop_price = calc_stop_loss(entry_price, atr)
            reason_tag = "초기손절"

        if current_price <= stop_price:
            self._logger.warning(
                f"[{code}] {reason_tag}: 현재가 {current_price:,.0f} ≤ 손절가 {stop_price:,.0f}"
            )
            await self._execute_sell(code, position, reason=reason_tag)

    # ── 트레일링 손절 ─────────────────────────────────────────

    async def check_trailing_stop(self, code: str, position: dict):
        """
        트레일링 손절: 고점 - ATR × TRAILING_STOP_ATR
        IOC 지정가 시도 → 미체결 시 시장가 전환
        """
        current_price = float(position.get("current_price") or 0)
        high_price    = float(position.get("high_price") or
                              position.get("entry_price") or 0)
        atr           = float(position.get("atr") or 0)

        if current_price <= 0 or atr <= 0:
            return

        trail_stop = calc_trailing_stop(high_price, atr)

        if current_price > trail_stop:
            # 고점 갱신
            if current_price > high_price:
                updated = dict(position)
                updated["high_price"] = current_price
                add_position(code, updated)
            return

        qty  = int(position.get("qty") or 0)
        name = position.get("name", code)
        self._logger.warning(
            f"[{code}] 트레일링 손절: 현재가 {current_price:,.0f} ≤ 트레일 {trail_stop:,.0f}"
        )

        if self.dry_run:
            self._logger.info(f"[DRY RUN] {code} 트레일링 매도 스킵")
            return

        # IOC 지정가 시도
        await self._rate_limiter.acquire()
        result = await self._run_sync(sell_ioc, code, qty, int(current_price))
        if result.get("success"):
            await self._on_sell_done(code, position, int(current_price), "트레일링_IOC")
        else:
            # 미체결 → 시장가
            self._logger.info(f"[{code}] IOC 미체결 → 시장가 전환")
            await self._rate_limiter.acquire()
            result2 = await self._run_sync(sell_market, code, qty)
            if result2.get("success"):
                await self._on_sell_done(code, position, int(current_price), "트레일링_시장가")

    # ── 신규 매수 신호 ────────────────────────────────────────

    async def check_buy_signal(self, code: str):
        """
        scanner_tools.check_buy_signal()을 호출하여 매수 신호 확인.
        신호 발생 시 buy_ioc() 실행.
        """
        df = await self._get_ohlcv(code)
        if df is None or len(df) < 20:
            return

        current_price = float(df["close"].iloc[-1])
        if current_price <= 0:
            return

        volume_today     = int(df["volume"].iloc[-1])
        volume_yesterday = int(df["volume"].iloc[-2]) if len(df) >= 2 else volume_today

        # tick_speed: 웹소켓 연동 전까지 0으로 처리
        # (websocket_feeder 구현 후 shared_state에서 읽도록 개선 예정)
        tick_speed = float((get_state("tick_speeds") or {}).get(code, 0))

        result = check_buy_signal(
            code=code,
            current_price=current_price,
            volume_today=volume_today,
            volume_yesterday_same_time=volume_yesterday,
            tick_speed=tick_speed,
            df_ohlcv=df,
        )

        if not result.get("signal"):
            self._logger.debug(
                f"[{code}] 매수 신호 없음 (score={result.get('score', 0)})"
            )
            return

        # 매수 수량 계산
        await self._rate_limiter.acquire()
        balance    = await self._run_sync(get_balance)
        cash       = balance.get("cash", 0)
        invest_amt = cash * POSITION_SIZE_RATIO
        qty        = int(invest_amt / current_price)

        if qty <= 0:
            self._logger.warning(
                f"[{code}] 매수 불가: 현금 {cash:,}원으로 {current_price:,.0f}원 × 0주"
            )
            return

        atr       = calc_atr(df)
        stop_loss = int(calc_stop_loss(current_price, atr))

        self._logger.info(
            f"[{code}] ▶ 매수 신호! 점수={result['score']} "
            f"가격={current_price:,.0f} 수량={qty} 손절={stop_loss:,}"
        )

        if self.dry_run:
            self._logger.info(f"[DRY RUN] {code} 매수 스킵")
            return

        await self._rate_limiter.acquire()
        order_result = await self._run_sync(buy_ioc, code, qty, int(current_price))
        if order_result.get("success"):
            await self._on_buy_done(
                code, qty, int(current_price), atr, stop_loss,
                score=result.get("score", 0),
                entry_volume=volume_today,
                entry_tick=tick_speed,
            )

    # ── 피라미딩 ──────────────────────────────────────────────

    async def check_pyramid(self, code: str, position: dict):
        """
        피라미딩(추가매수) 조건 확인.
        조건 충족 시 buy_ioc() 실행 후 평단 / 손절가 재계산.
        """
        if position.get("pyramiding_done", False):
            return

        df = await self._get_ohlcv(code)
        if df is None or len(df) < 5:
            return

        entry_price  = float(position.get("entry_price") or 0)
        avg_price    = float(position.get("avg_price") or entry_price)
        entry_volume = int(position.get("entry_volume") or 0)
        entry_tick   = float(position.get("entry_tick_speed") or 0)
        current_price = float(df["close"].iloc[-1])
        current_vol   = int(df["volume"].iloc[-1])
        current_tick  = float((get_state("tick_speeds") or {}).get(code, 0))

        should_pyramid = check_pyramid_signal(
            code=code,
            entry_price=entry_price,
            avg_price=avg_price,
            current_price=current_price,
            entry_volume=entry_volume,
            current_volume=current_vol,
            entry_tick_speed=entry_tick,
            current_tick_speed=current_tick,
            df_recent=df.tail(5),
            pyramiding_done=False,
            no_pyramid_after=NO_PYRAMID_AFTER,
        )

        if not should_pyramid:
            return

        qty     = int(position.get("qty") or 0)
        add_qty = max(1, int(qty * PYRAMID_ADD_RATIO))

        self._logger.info(
            f"[{code}] ▶ 피라미딩! +{add_qty}주 @ {current_price:,.0f}"
        )

        if self.dry_run:
            self._logger.info(f"[DRY RUN] {code} 피라미딩 스킵")
            return

        await self._rate_limiter.acquire()
        result = await self._run_sync(buy_ioc, code, add_qty, int(current_price))
        if result.get("success"):
            new_qty  = qty + add_qty
            new_avg  = int((avg_price * qty + current_price * add_qty) / new_qty)
            new_stop = int(calc_pyramid_stop(new_avg))

            updated = dict(position)
            updated.update({
                "qty":            new_qty,
                "avg_price":      new_avg,
                "stop_loss":      new_stop,
                "pyramiding_done": True,
            })
            add_position(code, updated)
            self._logger.info(
                f"[{code}] 피라미딩 완료: 평단 {new_avg:,} 신규손절 {new_stop:,}"
            )

    # ── 강제 청산 ─────────────────────────────────────────────

    async def force_close_all(self, reason: str = "15:20 장마감"):
        """
        모든 보유 포지션을 시장가 청산한다.
        수익률 +OVERNIGHT_THRESHOLD 이상 종목은 오버나이트 트랙으로 보존.
        """
        positions = get_positions()
        if not positions:
            self._logger.info(f"[{reason}] 청산 대상 없음")
            return

        self._logger.info(f"[{reason}] 전 포지션 청산: {len(positions)}종목")

        for code, pos in list(positions.items()):
            entry_price   = float(pos.get("entry_price") or 0)
            current_price = float(pos.get("current_price") or entry_price)
            qty           = int(pos.get("qty") or 0)

            # 장 마감 강제청산 + 오버나이트 조건 충족
            if (reason.endswith("장마감")
                    and is_overnight_candidate(entry_price, current_price)):
                overnight_stop = int(current_price * (1 + OVERNIGHT_STOP_PCT))
                self._logger.info(
                    f"[{code}] 오버나이트 전환 "
                    f"({(current_price / entry_price - 1) * 100:+.1f}%, "
                    f"익일손절={overnight_stop:,})"
                )
                updated = dict(pos)
                updated["overnight"]      = True
                updated["overnight_stop"] = overnight_stop
                add_position(code, updated)
                continue

            if self.dry_run:
                self._logger.info(f"[DRY RUN] {code} 강제청산 스킵")
                continue

            await self._rate_limiter.acquire()
            result = await self._run_sync(sell_market, code, qty)
            if result.get("success"):
                await self._on_sell_done(code, pos, int(current_price), reason)
            else:
                self._logger.error(f"[{code}] 강제청산 실패")
                try:
                    notify_error("force_close_all", f"{code} 청산 실패", MODE_LABEL)
                except Exception:
                    pass

    # ── 대기열 처리 ───────────────────────────────────────────

    async def process_queue(self):
        """
        포지션 여유가 있을 때 대기열 상위 종목을 자동 진입한다.
        """
        positions = get_positions()
        if len(positions) >= MAX_POSITIONS:
            return

        queue = get_state("queue") or []
        if not queue:
            return

        blacklist  = set(get_state("blacklist") or [])
        slots_left = MAX_POSITIONS - len(positions)

        for item in queue[:slots_left]:
            code = item.get("code") if isinstance(item, dict) else str(item)
            if not code:
                continue
            if code in positions or code in blacklist:
                continue
            self._logger.info(f"[큐] {code} 자동 진입 시도")
            await self.check_buy_signal(code)

    # ── 헬퍼 ──────────────────────────────────────────────────

    async def _get_ohlcv(self, code: str):
        """캐시된 OHLCV를 반환하거나 KIS API에서 새로 조회한다."""
        import time
        now = time.time()
        if code in self._ohlcv_cache:
            df, cached_at = self._ohlcv_cache[code]
            if now - cached_at < self._ohlcv_ttl:
                return df

        df = await self._run_sync(_fetch_ohlcv_sync, code)
        if df is not None:
            self._ohlcv_cache[code] = (df, now)
        return df

    async def _run_sync(self, func, *args, **kwargs):
        """동기 함수를 executor에서 비동기로 실행한다."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, lambda: func(*args, **kwargs)
        )

    async def _execute_sell(self, code: str, position: dict, reason: str):
        """손절 매도 실행 (�ry_run 체크 포함)."""
        qty           = int(position.get("qty") or 0)
        current_price = int(position.get("current_price") or 0)

        if self.dry_run:
            self._logger.info(f"[DRY RUN] {code} {reason} 매도 스킵")
            return

        await self._rate_limiter.acquire()
        result = await self._run_sync(sell_market, code, qty)
        if result.get("success"):
            await self._on_sell_done(code, position, current_price, reason)

    async def _on_buy_done(
        self, code: str, qty: int, price: int, atr: float,
        stop_loss: int, score: int, entry_volume: int, entry_tick: float,
    ):
        """매수 체결 후 shared_state 업데이트 및 알림."""
        name = code  # 실제로는 종목명 필요 (추후 KIS 종목 정보 API 연동)
        add_position(code, {
            "entry_price":      price,
            "avg_price":        price,
            "qty":              qty,
            "atr":              atr,
            "stop_loss":        stop_loss,
            "high_price":       price,
            "current_price":    price,
            "pyramiding_done":  False,
            "entry_volume":     entry_volume,
            "entry_tick_speed": entry_tick,
            "overnight":        False,
            "score":            score,
            "name":             name,
            "entry_time":       datetime.now().isoformat(),
        })
        self._logger.info(
            f"[{code}] ✅ 매수 완료: {qty}주 @ {price:,} 손절={stop_loss:,}"
        )
        try:
            notify_buy(code, name, qty, price, score, stop_loss, MODE_LABEL)
        except Exception:
            pass

    async def _on_sell_done(
        self, code: str, position: dict, price: int, reason: str,
    ):
        """매도 체결 후 shared_state 업데이트 및 알림."""
        entry_price = int(position.get("entry_price") or price)
        qty         = int(position.get("qty") or 0)
        name        = position.get("name", code)
        pnl_pct     = (
            (price - entry_price) / entry_price * 100
            if entry_price > 0 else 0.0
        )

        # 일 손실 누적 (pnl_pct는 이미 % 단위: -5.2 = -5.2%)
        if pnl_pct < 0:
            daily_loss = float(get_state("daily_loss") or 0.0)
            set_state("daily_loss", daily_loss + pnl_pct)

        remove_position(code)
        add_to_blacklist(code)

        self._logger.info(
            f"[{code}] ✅ 매도 완료: {qty}주 @ {price:,} "
            f"수익률={pnl_pct:+.2f}% 사유={reason}"
        )
        try:
            notify_sell(code, name, qty, price, entry_price, reason, MODE_LABEL)
        except Exception:
            pass


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — HeadStrategist 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 60)

    async def test():
        strat = HeadStrategist(dry_run=True, tick_interval=1.0)

        print("\n[1] DRY RUN 단일 틱 테스트...")
        try:
            await strat._tick()
            print("  ✅ 틱 실행 완료 (포지션 없음)")
        except Exception as e:
            print(f"  ❌ 오류: {e}")

        print("\n[2] 강제 청산 테스트 (빈 포지션)...")
        await strat.force_close_all(reason="TEST 장마감")
        print("  ✅ 청산 완료 (대상 없음)")

        print("\n[3] 시간 설정 확인...")
        now = datetime.now().strftime("%H:%M")
        in_session = MARKET_OPEN_HOLD <= now < FORCE_CLOSE_TIME
        print(f"  현재: {now}  장중여부: {in_session}")
        print(f"  장시작: {MARKET_OPEN_HOLD}  강제청산: {FORCE_CLOSE_TIME}")

        print("\n" + "=" * 60)
        print("  ✅ HeadStrategist 테스트 완료!")
        print("  💡 실제 매수 테스트: dry_run=False + .env 키 설정 후 실행")
        print("=" * 60)

    asyncio.run(test())
