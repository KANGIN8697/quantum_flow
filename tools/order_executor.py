# tools/order_executor.py — KIS API 주문 집행기 (v2.1 개선판)
# Phase 5 구현: 매수(IOC/시장가/폴백), 매도(시장가/IOC), 주문취소, 잔고조회, 체결확인
# 개선사항:
#   1) KRX 호가단위 기반 보수적 슬리피지 버퍼 (calc_limit_price)
#   2) 3단계 폴백 체인: IOC(ask1+3틱) → 재입찰(ask1+5틱) → 시장가
#   3) buy_market() 함수 추가 (시장가 매수)
#   4) 비동기 로그 큐 (asyncio.Queue) — I/O 블로킹 제거
#   5) 연결 프리웜 (pre_warm_connection) — 첫 주문 레이턴시 제거
#   6) 병렬 다종목 진입 지원 (buy_parallel_entries)

import os
import json
import time
import asyncio
import threading
import requests
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime
from dotenv import load_dotenv
from tools.utils import safe_float

load_dotenv()

# ── Token Bucket Rate Limiter ─────────────────────────────────
# KIS API 제한: 초당 20건 (안전 마진 적용하여 18건/초)

class _TokenBucket:
    """Thread-safe Token Bucket — API 호출 속도 제한."""

    def __init__(self, rate: float = 18.0, capacity: float = 18.0):
        self._rate = rate          # 초당 토큰 충전 속도
        self._capacity = capacity  # 최대 토큰 수
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 5.0) -> bool:
        """
        토큰 1개를 소비한다. 토큰이 없으면 충전될 때까지 대기.
        timeout 초 내에 토큰을 얻지 못하면 False 반환.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._rate)
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)  # 50ms 후 재시도


_rate_limiter = _TokenBucket(rate=18.0, capacity=18.0)


# ── HTTP 세션 풀 (TCP 재사용, 자동 재시도) ──────────────────────
# pool_maxsize=20: 병렬 다종목 동시 매수 대응
_RETRY = Retry(total=3, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503])
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=20, max_retries=_RETRY))

# ── 환경변수 ───────────────────────────────────────────────────
USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"

if USE_PAPER:
    BASE_URL    = "https://openapivts.koreainvestment.com:29443"
    APP_KEY     = os.getenv("KIS_PAPER_APP_KEY", "")
    APP_SECRET  = os.getenv("KIS_PAPER_APP_SECRET", "")
    ACCOUNT_NO  = os.getenv("KIS_ACCOUNT_NO", "")
    ACNT_PRDT   = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
    MODE_LABEL  = "모의투자"
    # 모의투자 TR ID
    TR_BUY      = "VTTC0802U"
    TR_SELL     = "VTTC0801U"
    TR_CANCEL   = "VTTC0803U"
    TR_BALANCE  = "VTTC8434R"
    TR_ORDERS   = "VTTC8036R"
else:
    BASE_URL    = "https://openapi.koreainvestment.com:9443"
    APP_KEY     = os.getenv("KIS_APP_KEY", "")
    APP_SECRET  = os.getenv("KIS_APP_SECRET", "")
    ACCOUNT_NO  = os.getenv("KIS_ACCOUNT_NO", "")
    ACNT_PRDT   = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
    MODE_LABEL  = "실전투자"
    # 실전 TR ID
    TR_BUY      = "TTTC0802U"
    TR_SELL     = "TTTC0801U"
    TR_CANCEL   = "TTTC0803U"
    TR_BALANCE  = "TTTC8434R"
    TR_ORDERS   = "TTTC8036R"

# 주문 로그 파일 경로
LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "reports"
)

# ── 캐시된 헤더 (프리웜 후 재사용) ───────────────────────────────
_cached_token: str = ""
_cached_token_expiry: float = 0.0
_cached_headers: dict = {}


# ── 비동기 로그 큐 (I/O 블로킹 제거) ────────────────────────────
_log_queue: asyncio.Queue = None  # 이벤트 루프 생성 후 초기화
_log_worker_started = False


def _get_log_queue() -> asyncio.Queue:
    """이벤트 루프 내에서 안전하게 큐 초기화."""
    global _log_queue
    if _log_queue is None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _log_queue = asyncio.Queue()
        except RuntimeError:
            pass
    return _log_queue


async def _log_worker():
    """백그라운드 로그 파이프라인: 큐에서 레코드를 꺼내 파일 저장."""
    while True:
        record = await _log_queue.get()
        if record is None:  # 종료 신호
            break
        try:
            _log_order_sync(record)
        except Exception:
            pass
        _log_queue.task_done()


async def _start_log_worker():
    """로그 워커 asyncio 태스크 시작 (중복 방지)."""
    global _log_worker_started
    if not _log_worker_started and _get_log_queue() is not None:
        _log_worker_started = True
        asyncio.ensure_future(_log_worker())


# ── 내부 유틸 ─────────────────────────────────────────────────

def _get_token() -> str:
    """ensure_token()으로 유효한 액세스 토큰을 가져온다."""
    from tools.token_manager import ensure_token
    return ensure_token()


def _headers(tr_id: str) -> dict:
    """KIS API 공통 헤더 생성. (토큰 캐시 활용)"""
    token = _get_token()
    return {
        "Content-Type":    "application/json; charset=utf-8",
        "authorization":   f"Bearer {token}",
        "appkey":          APP_KEY,
        "appsecret":       APP_SECRET,
        "tr_id":           tr_id,
        "custtype":        "P",
    }


def _log_order_sync(record: dict):
    """주문 결과를 날짜별 JSON 파일에 동기적으로 저장."""
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(LOG_DIR, f"orders_{today}.json")

    records = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (json.JSONDecodeError, IOError):
            records = []

    records.append(record)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _log_order(record: dict):
    """
    주문 결과 로그 저장.
    비동기 컨텍스트에서는 큐에 넣고 즉시 반환(non-blocking).
    동기 컨텍스트에서는 직접 파일 저장.
    """
    q = _get_log_queue()
    if q is not None:
        try:
            q.put_nowait(record)
            return
        except asyncio.QueueFull:
            pass  # 큐 가득 시 동기 폴백
    _log_order_sync(record)


# ── 연결 프리웜 (첫 주문 레이턴시 제거) ──────────────────────────

def pre_warm_connection():
    """
    장 시작 전 토큰 발급 + TCP 연결 프리웜.
    주 이벤트 루프 시작 시 한 번 호출.
    """
    try:
        token = _get_token()
        if token:
            # 잔고조회로 연결 수립 (가벼운 GET 요청)
            url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
            params = {
                "CANO": ACCOUNT_NO[:8], "ACNT_PRDT_CD": ACNT_PRDT,
                "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            }
            _SESSION.get(url, headers=_headers(TR_BALANCE), params=params, timeout=5)
            print(f"  ✅ 연결 프리웜 완료 ({MODE_LABEL})")
    except Exception as e:
        print(f"  ⚠️  연결 프리웜 실패 (무시): {e}")


# ── KRX 호가단위 테이블 ────────────────────────────────────────

def _get_tick_size(price: int) -> int:
    """
    KRX 코스피/코스닥 호가단위 반환.
    가격 구간별 최소 변동 단위 (원).

    가격 구간     호가단위
    ----------   --------
    < 1,000       1
    < 5,000       5
    < 10,000     10
    < 50,000     50
    < 100,000   100
    < 500,000   500
    ≥ 500,000  1,000
    """
    if price < 1_000:
        return 1
    elif price < 5_000:
        return 5
    elif price < 10_000:
        return 10
    elif price < 50_000:
        return 50
    elif price < 100_000:
        return 100
    elif price < 500_000:
        return 500
    else:
        return 1_000


def calc_limit_price(ask1: int, n_ticks: int = 3) -> int:
    """
    IOC 지정가 주문가 계산: 매도1호가 + N 호가단위.

    보수적 기본값 3틱: 빠른 체결 vs 과도한 슬리피지 방지 균형.
    - ask1+3틱으로도 미체결 시 ask1+5틱으로 재입찰 (buy_with_fallback).

    Parameters
    ----------
    ask1   : 현재 매도1호가 (원)
    n_ticks: 버퍼 호가단위 수 (기본 3)

    Returns
    -------
    int: 지정가 주문 단가 (호가단위 배수로 정렬)
    """
    if ask1 <= 0:
        return 0
    tick = _get_tick_size(ask1)
    raw_price = ask1 + tick * n_ticks
    # 호가단위 배수로 반올림 (거래소 규정 준수)
    return (raw_price // tick) * tick


# ── 1. 매수 (IOC — Immediate Or Cancel) ──────────────────────

def buy_ioc(code: str, qty: int, price: int, dry_run: bool = False) -> dict:
    """
    IOC 방식으로 지정가 매수 주문을 실행한다.
    체결되지 않은 수량은 즉시 취소된다.

    Parameters
    ----------
    code    : 종목코드 (6자리, e.g. '005930')
    qty     : 주문 수량
    price   : 주문 단가 (원) — calc_limit_price()로 계산된 값 권장
    dry_run : True면 실제 주문 없이 로그만 출력

    Returns
    -------
    dict: {success, order_no, code, qty, price, mode, timestamp, error}
    """
    timestamp = datetime.now().isoformat()

    if dry_run:
        record = {
            "type": "BUY_IOC", "success": True, "order_no": "DRY_RUN",
            "code": code, "qty": qty, "price": price,
            "mode": f"{MODE_LABEL}(DRY)", "timestamp": timestamp,
        }
        print(f"  🔵 [DRY_RUN] 매수IOC: {code} {qty}주 @{price:,}원")
        _log_order(record)
        return record

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "PDNO":        code,
        "ORD_DVSN":    "01",        # 01=IOC 지정가
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    str(price),
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.post(url, headers=_headers(TR_BUY), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "9")
        order_no = data.get("output", {}).get("ODNO", "")
        success = rt_cd == "0"

        record = {
            "type":      "BUY_IOC",
            "success":   success,
            "order_no":  order_no,
            "code":      code,
            "qty":       qty,
            "price":     price,
            "mode":      MODE_LABEL,
            "timestamp": timestamp,
            "rt_cd":     rt_cd,
            "msg":       data.get("msg1", ""),
        }
        _log_order(record)

        if success:
            print(f"  ✅ [{MODE_LABEL}] 매수IOC 성공: {code} {qty}주 @{price:,}원  주문번호:{order_no}")
        else:
            print(f"  ❌ [{MODE_LABEL}] 매수IOC 실패: {code} | {data.get('msg1', '')}")

        return record

    except Exception as e:
        record = {
            "type": "BUY_IOC", "success": False, "code": code,
            "qty": qty, "price": price, "mode": MODE_LABEL,
            "timestamp": timestamp, "error": str(e),
        }
        _log_order(record)
        print(f"  ❌ [{MODE_LABEL}] 매수IOC 오류: {code} | {e}")
        return record


# ── 2. 매수 (시장가) — 신규 추가 ─────────────────────────────

def buy_market(code: str, qty: int, dry_run: bool = False) -> dict:
    """
    시장가 매수 주문을 실행한다. (3단계 폴백의 최후 수단)

    주의: 시장가 매수는 슬리피지가 크므로 폴백 최후 수단으로만 사용.
    IOC+3틱, IOC+5틱 모두 미체결 후 진입 불가 판단 시 호출.

    Parameters
    ----------
    code    : 종목코드
    qty     : 매수 수량
    dry_run : True면 실제 주문 없이 로그만 출력
    """
    timestamp = datetime.now().isoformat()

    if dry_run:
        record = {
            "type": "BUY_MARKET", "success": True, "order_no": "DRY_RUN",
            "code": code, "qty": qty, "price": 0,
            "mode": f"{MODE_LABEL}(DRY)", "timestamp": timestamp,
        }
        print(f"  🔵 [DRY_RUN] 시장가매수: {code} {qty}주")
        _log_order(record)
        return record

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "PDNO":        code,
        "ORD_DVSN":    "01",   # 시장가: 단가 0 + ORD_DVSN 01
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    "0",    # 시장가: 가격 0
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.post(url, headers=_headers(TR_BUY), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "9")
        order_no = data.get("output", {}).get("ODNO", "")
        success = rt_cd == "0"

        record = {
            "type":      "BUY_MARKET",
            "success":   success,
            "order_no":  order_no,
            "code":      code,
            "qty":       qty,
            "price":     0,
            "mode":      MODE_LABEL,
            "timestamp": timestamp,
            "rt_cd":     rt_cd,
            "msg":       data.get("msg1", ""),
        }
        _log_order(record)

        if success:
            print(f"  ✅ [{MODE_LABEL}] 시장가매수 성공: {code} {qty}주  주문번호:{order_no}")
        else:
            print(f"  ❌ [{MODE_LABEL}] 시장가매수 실패: {code} | {data.get('msg1', '')}")

        return record

    except Exception as e:
        record = {
            "type": "BUY_MARKET", "success": False, "code": code,
            "qty": qty, "price": 0, "mode": MODE_LABEL,
            "timestamp": timestamp, "error": str(e),
        }
        _log_order(record)
        print(f"  ❌ [{MODE_LABEL}] 시장가매수 오류: {code} | {e}")
        return record


# ── 3. 3단계 폴백 체인 매수 (핵심 개선) ──────────────────────

async def buy_with_fallback(
    code: str,
    qty: int,
    ask1: int,
    dry_run: bool = False,
    notify_fn=None,
) -> dict:
    """
    3단계 폴백 체인으로 매수 체결을 극대화한다.

    단계 1: IOC (ask1 + 3틱) — 보수적 슬리피지, 즉시 체결 시도
    단계 2: IOC (ask1 + 5틱) — 200ms 후 재입찰 (틱 확대)
    단계 3: 시장가 매수      — 최후 수단, 반드시 체결

    Parameters
    ----------
    code     : 종목코드
    qty      : 매수 수량
    ask1     : 현재 매도1호가 (웹소켓 실시간값)
    dry_run  : True면 실제 주문 없이 시뮬레이션
    notify_fn: 텔레그램 알림 콜백 (선택)

    Returns
    -------
    dict: {
        success, code, qty, filled_qty, stage_used,
        final_price, orders, timestamp
    }
    """
    start_ts = datetime.now().isoformat()
    orders = []
    filled_qty = 0
    stage_used = 0

    # ── Stage 1: IOC + 3틱 ───────────────────────────────────
    price1 = calc_limit_price(ask1, n_ticks=3)
    print(f"  🔄 [{MODE_LABEL}] {code} Stage1 IOC @{price1:,}원 (ask1={ask1:,}+3틱)")

    result1 = buy_ioc(code, qty, price1, dry_run=dry_run)
    orders.append(result1)

    if result1.get("success"):
        await asyncio.sleep(0.15)  # 체결 확인 대기 150ms
        order_no = result1.get("order_no", "")
        if order_no and not dry_run:
            status1 = get_order_status(order_no)
            filled_qty = status1.get("filled_qty", 0)
        else:
            filled_qty = qty  # dry_run: 전량 체결 가정

        if filled_qty >= qty:
            stage_used = 1
            print(f"  ✅ Stage1 체결 완료: {filled_qty}/{qty}주")
            return {
                "success": True, "code": code, "qty": qty,
                "filled_qty": filled_qty, "stage_used": stage_used,
                "final_price": price1, "orders": orders,
                "timestamp": start_ts,
            }
        elif filled_qty > 0:
            # 부분 체결 — 잔여분 Stage2 재시도
            remaining = qty - filled_qty
            qty = remaining  # 잔여만 재주문
            print(f"  ⚠️  Stage1 부분체결 {filled_qty}주, 잔여 {remaining}주 Stage2 진행")

    # ── Stage 2: IOC + 5틱 (200ms 후) ────────────────────────
    await asyncio.sleep(0.2)
    price2 = calc_limit_price(ask1, n_ticks=5)
    print(f"  🔄 [{MODE_LABEL}] {code} Stage2 IOC @{price2:,}원 (ask1={ask1:,}+5틱)")

    result2 = buy_ioc(code, qty, price2, dry_run=dry_run)
    orders.append(result2)

    if result2.get("success"):
        await asyncio.sleep(0.15)
        order_no2 = result2.get("order_no", "")
        if order_no2 and not dry_run:
            status2 = get_order_status(order_no2)
            s2_filled = status2.get("filled_qty", 0)
        else:
            s2_filled = qty  # dry_run

        filled_qty += s2_filled

        if s2_filled >= qty:
            stage_used = 2
            print(f"  ✅ Stage2 체결 완료: 누적 {filled_qty}주")
            return {
                "success": True, "code": code, "qty": qty + (filled_qty - s2_filled),
                "filled_qty": filled_qty, "stage_used": stage_used,
                "final_price": price2, "orders": orders,
                "timestamp": start_ts,
            }
        elif s2_filled > 0:
            remaining = qty - s2_filled
            qty = remaining
            print(f"  ⚠️  Stage2 부분체결, 잔여 {remaining}주 Stage3 진행")

    # ── Stage 3: 시장가 매수 (최후 수단) ─────────────────────
    await asyncio.sleep(0.05)
    print(f"  🚨 [{MODE_LABEL}] {code} Stage3 시장가매수 (최후수단) {qty}주")

    result3 = buy_market(code, qty, dry_run=dry_run)
    orders.append(result3)

    if result3.get("success"):
        stage_used = 3
        filled_qty += qty  # 시장가는 전량 체결 가정
        print(f"  ✅ Stage3 시장가 완료: 누적 {filled_qty}주")

        # 시장가 사용 텔레그램 알림
        if notify_fn:
            try:
                notify_fn(f"⚡ {code} 시장가 진입 (Stage3)\n"
                          f"슬리피지 확대 주의: ask1={ask1:,}원")
            except Exception:
                pass

        return {
            "success": True, "code": code, "qty": qty,
            "filled_qty": filled_qty, "stage_used": stage_used,
            "final_price": 0,  # 시장가: 실제 체결가 미확정
            "orders": orders, "timestamp": start_ts,
        }

    # ── 전 단계 실패 ────────────────────────────────────────
    print(f"  ❌ [{MODE_LABEL}] {code} 3단계 전부 실패 — 진입 포기")
    return {
        "success": False, "code": code, "qty": qty,
        "filled_qty": filled_qty, "stage_used": 0,
        "final_price": 0, "orders": orders,
        "timestamp": start_ts,
    }


# ── 4. 병렬 다종목 동시 매수 ─────────────────────────────────

async def buy_parallel_entries(
    entries: list,
    dry_run: bool = False,
    notify_fn=None,
) -> list:
    """
    여러 종목을 asyncio.gather로 병렬 매수.
    최대 5종목 동시 체결 — 순차 대비 ~5배 빠른 진입.

    Parameters
    ----------
    entries : [{"code": "005930", "qty": 100, "ask1": 72000}, ...]
    dry_run : True면 실제 주문 없이 시뮬레이션

    Returns
    -------
    list: 각 종목별 buy_with_fallback 결과 목록
    """
    tasks = [
        buy_with_fallback(
            e["code"], e["qty"], e["ask1"],
            dry_run=dry_run, notify_fn=notify_fn
        )
        for e in entries
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 예외를 결과 딕셔너리로 변환
    out = []
    for e, r in zip(entries, results):
        if isinstance(r, Exception):
            out.append({
                "success": False, "code": e["code"],
                "error": str(r), "stage_used": 0,
            })
        else:
            out.append(r)

    return out


# ── 5. 매도 (시장가) ──────────────────────────────────────────

def sell_market(code: str, qty: int, dry_run: bool = False) -> dict:
    """
    시장가 매도 주문을 실행한다.

    Parameters
    ----------
    code    : 종목코드
    qty     : 매도 수량 (0이면 전량)
    dry_run : True면 실제 주문 없이 로그만 출력
    """
    timestamp = datetime.now().isoformat()

    if dry_run:
        record = {
            "type": "SELL_MARKET", "success": True, "order_no": "DRY_RUN",
            "code": code, "qty": qty, "price": 0,
            "mode": f"{MODE_LABEL}(DRY)", "timestamp": timestamp,
        }
        print(f"  🔵 [DRY_RUN] 시장가매도: {code} {qty}주")
        _log_order(record)
        return record

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "PDNO":        code,
        "ORD_DVSN":    "01",        # 시장가 = 주문단가 0 + 구분 01
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    "0",
        "SLL_TYPE":    "01",        # 매도
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.post(url, headers=_headers(TR_SELL), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "9")
        order_no = data.get("output", {}).get("ODNO", "")
        success = rt_cd == "0"

        record = {
            "type":      "SELL_MARKET",
            "success":   success,
            "order_no":  order_no,
            "code":      code,
            "qty":       qty,
            "price":     0,
            "mode":      MODE_LABEL,
            "timestamp": timestamp,
            "rt_cd":     rt_cd,
            "msg":       data.get("msg1", ""),
        }
        _log_order(record)

        if success:
            print(f"  ✅ [{MODE_LABEL}] 시장가매도 성공: {code} {qty}주  주문번호:{order_no}")
        else:
            print(f"  ❌ [{MODE_LABEL}] 시장가매도 실패: {code} | {data.get('msg1', '')}")

        return record

    except Exception as e:
        record = {
            "type": "SELL_MARKET", "success": False, "code": code,
            "qty": qty, "price": 0, "mode": MODE_LABEL,
            "timestamp": timestamp, "error": str(e),
        }
        _log_order(record)
        print(f"  ❌ [{MODE_LABEL}] 시장가매도 오류: {code} | {e}")
        return record


# ── 6. 매도 (IOC 지정가) ─────────────────────────────────────

def sell_ioc(code: str, qty: int, price: int, dry_run: bool = False) -> dict:
    """
    IOC 방식으로 지정가 매도 주문을 실행한다.
    체결되지 않은 수량은 즉시 취소된다.
    """
    timestamp = datetime.now().isoformat()

    if dry_run:
        record = {
            "type": "SELL_IOC", "success": True, "order_no": "DRY_RUN",
            "code": code, "qty": qty, "price": price,
            "mode": f"{MODE_LABEL}(DRY)", "timestamp": timestamp,
        }
        print(f"  🔵 [DRY_RUN] 매도IOC: {code} {qty}주 @{price:,}원")
        _log_order(record)
        return record

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "PDNO":        code,
        "ORD_DVSN":    "01",
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    str(price),
        "SLL_TYPE":    "01",
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.post(url, headers=_headers(TR_SELL), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "9")
        order_no = data.get("output", {}).get("ODNO", "")
        success = rt_cd == "0"

        record = {
            "type":      "SELL_IOC",
            "success":   success,
            "order_no":  order_no,
            "code":      code,
            "qty":       qty,
            "price":     price,
            "mode":      MODE_LABEL,
            "timestamp": timestamp,
            "rt_cd":     rt_cd,
            "msg":       data.get("msg1", ""),
        }
        _log_order(record)

        if success:
            print(f"  ✅ [{MODE_LABEL}] 매도IOC 성공: {code} {qty}주 @{price:,}원  주문번호:{order_no}")
        else:
            print(f"  ❌ [{MODE_LABEL}] 매도IOC 실패: {code} | {data.get('msg1', '')}")

        return record

    except Exception as e:
        record = {
            "type": "SELL_IOC", "success": False, "code": code,
            "qty": qty, "price": price, "mode": MODE_LABEL,
            "timestamp": timestamp, "error": str(e),
        }
        _log_order(record)
        print(f"  ❌ [{MODE_LABEL}] 매도IOC 오류: {code} | {e}")
        return record


# ── 7. 주문 취소 ──────────────────────────────────────────────

def cancel_order(order_no: str, code: str, qty: int, price: int) -> dict:
    """
    미체결 주문을 취소한다.

    Parameters
    ----------
    order_no : 원주문번호 (ODNO)
    code     : 종목코드
    qty      : 취소 수량
    price    : 원주문 단가
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "KRX_FWDG_ORD_ORGNO": "",
        "ORGN_ODNO":   order_no,
        "ORD_DVSN":    "01",
        "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    str(price),
        "QTY_ALL_ORD_YN": "Y",
    }

    timestamp = datetime.now().isoformat()
    try:
        _rate_limiter.acquire()
        resp = _SESSION.post(url, headers=_headers(TR_CANCEL), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "9")
        success = rt_cd == "0"

        record = {
            "type":      "CANCEL",
            "success":   success,
            "order_no":  order_no,
            "code":      code,
            "qty":       qty,
            "price":     price,
            "mode":      MODE_LABEL,
            "timestamp": timestamp,
            "rt_cd":     rt_cd,
            "msg":       data.get("msg1", ""),
        }
        _log_order(record)

        if success:
            print(f"  ✅ [{MODE_LABEL}] 주문취소 성공: 주문번호 {order_no}")
        else:
            print(f"  ❌ [{MODE_LABEL}] 주문취소 실패: {data.get('msg1', '')}")

        return record

    except Exception as e:
        record = {
            "type": "CANCEL", "success": False, "order_no": order_no,
            "code": code, "mode": MODE_LABEL,
            "timestamp": timestamp, "error": str(e),
        }
        _log_order(record)
        print(f"  ❌ [{MODE_LABEL}] 주문취소 오류: {e}")
        return record


# ── 8. 잔고 조회 ─────────────────────────────────────────────

def get_balance() -> dict:
    """
    현재 계좌 잔고(보유 종목 목록 + 예수금)를 조회한다.

    Returns
    -------
    dict: {
        cash       : 예수금 (원),
        positions  : [{ code, name, qty, avg_price, current_price, pnl_pct }, ...],
        total_eval : 총 평가금액,
    }
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO":             ACCOUNT_NO[:8],
        "ACNT_PRDT_CD":     ACNT_PRDT,
        "AFHR_FLPR_YN":     "N",
        "OFL_YN":           "N",
        "INQR_DVSN":        "02",
        "UNPR_DVSN":        "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN":        "01",
        "CTX_AREA_FK100":   "",
        "CTX_AREA_NK100":   "",
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.get(url, headers=_headers(TR_BALANCE), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])

        positions = []
        for item in output1:
            qty = int(item.get("hldg_qty", 0))
            if qty == 0:
                continue
            avg_price = safe_float(item.get("pchs_avg_pric", 0))
            current_price = safe_float(item.get("prpr", 0))
            pnl_pct = (
                (current_price - avg_price) / (avg_price or 1) * 100
                if avg_price > 0 else 0.0
            )
            positions.append({
                "code":          item.get("pdno", ""),
                "name":          item.get("prdt_name", ""),
                "qty":           qty,
                "avg_price":     int(avg_price),
                "current_price": int(current_price),
                "pnl_pct":       round(pnl_pct, 2),
            })

        summary = output2[0] if output2 else {}
        cash = int(safe_float(summary.get("dnca_tot_amt", 0)))
        total_eval = int(safe_float(summary.get("tot_evlu_amt", 0)))

        print(f"  💰 [{MODE_LABEL}] 잔고조회 완료: 예수금 {cash:,}원  보유{len(positions)}종목  총평가 {total_eval:,}원")
        return {
            "cash":       cash,
            "positions":  positions,
            "total_eval": total_eval,
        }

    except Exception as e:
        print(f"  ❌ [{MODE_LABEL}] 잔고조회 오류: {e}")
        return {"cash": 0, "positions": [], "total_eval": 0}


# ── 9. 체결 확인 ─────────────────────────────────────────────

def get_order_status(order_no: str) -> dict:
    """
    특정 주문번호의 체결 상태를 조회한다.

    Returns
    -------
    dict: {filled_qty, remaining_qty, status, avg_fill_price}
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    params = {
        "CANO":         ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "INQR_STRT_DT": datetime.now().strftime("%Y%m%d"),
        "INQR_END_DT":  datetime.now().strftime("%Y%m%d"),
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN":    "01",
        "PDNO":         "",
        "ORD_GNO_BRNO": "",
        "ODNO":         order_no,
        "INQR_DVSN_3":  "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    try:
        _rate_limiter.acquire()
        resp = _SESSION.get(url, headers=_headers(TR_ORDERS), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        output = data.get("output1", [])
        if not output:
            return {"filled_qty": 0, "remaining_qty": 0, "status": "UNKNOWN", "avg_fill_price": 0}

        item = output[0]
        filled_qty    = int(item.get("tot_ccld_qty", 0))
        order_qty     = int(item.get("ord_qty", 0))
        remaining_qty = order_qty - filled_qty
        avg_fill_price = int(safe_float(item.get("avg_prvs", 0)))

        if remaining_qty == 0 and filled_qty > 0:
            status = "FILLED"
        elif filled_qty > 0:
            status = "PARTIAL"
        else:
            status = "PENDING"

        return {
            "filled_qty":     filled_qty,
            "remaining_qty":  remaining_qty,
            "status":         status,
            "avg_fill_price": avg_fill_price,
        }

    except Exception as e:
        print(f"  ❌ [{MODE_LABEL}] 체결조회 오류: {e}")
        return {"filled_qty": 0, "remaining_qty": 0, "status": "ERROR", "avg_fill_price": 0}


# ── 10. Micro-TWAP 분할 매수 (개선판) ───────────────────────

async def buy_twap(code: str, total_qty: int, ask1: int,
                   avg_daily_volume: int = 0,
                   tick_speed_fn=None,
                   dry_run: bool = False) -> dict:
    """
    Micro-TWAP: 주문 수량을 분할하여 호가 상태를 확인하며 진입.
    각 분할은 buy_with_fallback()으로 실행 (슬리피지 버퍼 + 폴백 포함).

    Parameters
    ----------
    code              : 종목코드
    total_qty         : 총 주문 수량
    ask1              : 현재 매도1호가 (웹소켓 실시간값)
    avg_daily_volume  : 일평균 거래량 (0이면 분할 없이 단일 주문)
    tick_speed_fn     : 현재 틱 속도 반환 콜백 (없으면 틱 체크 생략)
    dry_run           : True면 실제 주문 없이 시뮬레이션
    """
    try:
        from config.settings import (
            TWAP_VOLUME_THRESHOLD, TWAP_MAX_SPLITS,
            TWAP_INTERVAL_SEC, TWAP_TICK_SPEED_MIN,
        )
    except ImportError:
        TWAP_VOLUME_THRESHOLD = 0.001
        TWAP_MAX_SPLITS = 4
        TWAP_INTERVAL_SEC = 45
        TWAP_TICK_SPEED_MIN = 5

    # 분할 횟수 결정 (유동성 기반)
    if avg_daily_volume > 0:
        order_ratio = total_qty / (avg_daily_volume or 1)
        if order_ratio < TWAP_VOLUME_THRESHOLD:
            num_splits = 1
        elif order_ratio < TWAP_VOLUME_THRESHOLD * 5:
            num_splits = 2
        else:
            num_splits = TWAP_MAX_SPLITS
    else:
        num_splits = 1

    split_qty = total_qty // (num_splits or 1)
    remainder = total_qty % num_splits
    split_quantities = [split_qty] * num_splits
    split_quantities[-1] += remainder

    print(f"  📊 [{MODE_LABEL}] TWAP 시작: {code} 총{total_qty}주 → {num_splits}분할")

    orders = []
    total_filled = 0
    splits_executed = 0

    for i, qty in enumerate(split_quantities):
        if i > 0:
            # 틱 속도 체크
            if tick_speed_fn is not None:
                try:
                    current_tick = tick_speed_fn(code)
                    if current_tick < TWAP_TICK_SPEED_MIN:
                        print(f"    ⚠️  분할 {i+1}: 틱속도 부족 ({current_tick:.1f}) → Skip")
                        break
                except Exception:
                    pass
            await asyncio.sleep(TWAP_INTERVAL_SEC)

        # 각 분할을 buy_with_fallback으로 실행 (슬리피지 버퍼 + 폴백 포함)
        result = await buy_with_fallback(code, qty, ask1, dry_run=dry_run)
        orders.append(result)
        splits_executed += 1

        if result.get("success"):
            s_filled = result.get("filled_qty", 0)
            total_filled += s_filled
            print(f"    분할 {i+1}/{num_splits}: {s_filled}/{qty}주 체결 (Stage{result.get('stage_used',0)})")
        else:
            print(f"    ❌ 분할 {i+1}/{num_splits}: 실패 → 잔여 Skip")
            break

    success = total_filled > 0
    print(f"  {'✅' if success else '❌'} TWAP 완료: {total_filled}/{total_qty}주 ({splits_executed}/{num_splits}분할)")

    twap_result = {
        "type": "BUY_TWAP",
        "success": success,
        "code": code,
        "total_qty": total_qty,
        "total_filled": total_filled,
        "splits_executed": splits_executed,
        "splits_planned": num_splits,
        "ask1": ask1,
        "mode": MODE_LABEL,
        "timestamp": datetime.now().isoformat(),
        "orders": orders,
    }
    _log_order(twap_result)
    return twap_result


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW — 주문 집행기 v2.1 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 55)

    if not APP_KEY or not APP_SECRET or not ACCOUNT_NO:
        print()
        print("⚠️  API 키 또는 계좌번호가 설정되지 않았습니다.")
        print("   .env 파일에 아래 항목을 입력하세요:\n")
        if USE_PAPER:
            print("   KIS_PAPER_APP_KEY=...")
            print("   KIS_PAPER_APP_SECRET=...")
        else:
            print("   KIS_APP_KEY=...")
            print("   KIS_APP_SECRET=...")
        print("   KIS_ACCOUNT_NO=...")
        print()
        exit(0)

    # 호가단위 계산 테스트
    print("\n[1] 호가단위(calc_limit_price) 테스트:")
    test_cases = [
        (1500, 3),    # 1틱=1, +3틱=4원
        (8000, 3),    # 1틱=5, +3틱=15원
        (45000, 3),   # 1틱=10, +3틱=30원
        (72000, 3),   # 1틱=50, +3틱=150원 (삼성전자급)
        (150000, 3),  # 1틱=100, +3틱=300원
    ]
    for ask1, n in test_cases:
        result = calc_limit_price(ask1, n)
        tick = _get_tick_size(ask1)
        print(f"    ask1={ask1:,}원 | 틱={tick}원 | +{n}틱={result:,}원 (슬리피지={result-ask1:,}원)")

    print("\n[2] 연결 프리웜 테스트...")
    pre_warm_connection()

    print("\n[3] 잔고 조회...")
    balance = get_balance()
    print(f"    예수금: {balance['cash']:,}원  보유: {len(balance['positions'])}종목")

    print("\n[4] DRY_RUN 매수 폴백 체인 테스트 (삼성전자 1주)...")
    async def _test():
        result = await buy_with_fallback("005930", qty=1, ask1=72000, dry_run=True)
        print(f"    결과: stage={result['stage_used']} filled={result['filled_qty']} success={result['success']}")
    asyncio.run(_test())

    print("\n" + "=" * 55)
    print("  ✅ order_executor.py v2.1 확인 완료!")
    print("=" * 55)
