# tools/order_executor.py — KIS API 주문 집행기
# Phase 5 구현: 매수(IOC), 매도(시장가/IOC), 주문취소, 잔고조회, 체결확인
# 모든 주문 결과는 outputs/reports/orders_YYYYMMDD.json 에 로깅

import os
import json
import time
import threading
import requests
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime
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
_RETRY = Retry(total=3, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503])
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=10, max_retries=_RETRY))

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


# ── 내부 유틸 ─────────────────────────────────────────────────

def _get_token() -> str:
    """ensure_token()으로 유효한 액세스 토큰을 가져온다."""
    from tools.token_manager import ensure_token
    return ensure_token()


def _headers(tr_id: str) -> dict:
    """KIS API 공통 헤더 생성."""
    token = _get_token()
    return {
        "Content-Type":    "application/json; charset=utf-8",
        "authorization":   f"Bearer {token}",
        "appkey":          APP_KEY,
        "appsecret":       APP_SECRET,
        "tr_id":           tr_id,
        "custtype":        "P",
    }


def _log_order(record: dict):
    """주문 결과를 날짜별 JSON 파일에 누적 저장한다."""
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


# ── 1. 매수 (IOC — Immediate Or Cancel) ──────────────────────

def buy_ioc(code: str, qty: int, price: int) -> dict:
    """
    IOC 방식으로 지정가 매수 주문을 실행한다.
    체결되지 않은 수량은 즉시 취소된다.

    Parameters
    ----------
    code  : 종목코드 (6자리, e.g. '005930')
    qty   : 주문 수량
    price : 주문 단가 (원)

    Returns
    -------
    dict: {success, order_no, code, qty, price, mode, timestamp, error}
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":        ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACNT_PRDT,
        "PDNO":        code,
        "ORD_DVSN":    "01",        # 01=IOC 지정가
        "ORD_QTY":     str(qty),
        "ORD_UNPR":    str(price),
    }

    timestamp = datetime.now().isoformat()
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


# ── 2. 매도 (시장가) ──────────────────────────────────────────

def sell_market(code: str, qty: int) -> dict:
    """
    시장가 매도 주문을 실행한다.

    Parameters
    ----------
    code : 종목코드
    qty  : 매도 수량 (0이면 전량)
    """
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

    timestamp = datetime.now().isoformat()
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


# ── 3. 매도 (IOC 지정가) ─────────────────────────────────────

def sell_ioc(code: str, qty: int, price: int) -> dict:
    """
    IOC 방식으로 지정가 매도 주문을 실행한다.
    체결되지 않은 수량은 즉시 취소된다.
    """
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

    timestamp = datetime.now().isoformat()
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


# ── 4. 주문 취소 ──────────────────────────────────────────────

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


# ── 5. 잔고 조회 ─────────────────────────────────────────────

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


# ── 6. 체결 확인 ─────────────────────────────────────────────

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


# ── 7. [기능1] Micro-TWAP 분할 매수 ──────────────────────

async def buy_twap(code: str, total_qty: int, price: int,
                   avg_daily_volume: int = 0,
                   tick_speed_fn=None) -> dict:
    """
    Micro-TWAP: 주문 수량을 분할하여 호가 상태를 확인하며 진입.
    일평균 거래량 대비 주문 비율에 따라 분할 횟수를 자동 결정.

    Parameters
    ----------
    code              : 종목코드
    total_qty         : 총 주문 수량
    price             : 주문 단가
    avg_daily_volume  : 일평균 거래량 (0이면 분할 없이 단일 주문)
    tick_speed_fn     : 현재 틱 속도를 반환하는 콜백 (없으면 틱 체크 생략)

    Returns
    -------
    dict: {success, total_filled, splits_executed, splits_planned, orders}
    """
    import asyncio

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

    # 분할 횟수 결정
    if avg_daily_volume > 0:
        order_ratio = total_qty / (avg_daily_volume or 1)
        if order_ratio < TWAP_VOLUME_THRESHOLD:
            num_splits = 1  # 유동성 충분 → 분할 불필요
        elif order_ratio < TWAP_VOLUME_THRESHOLD * 5:
            num_splits = 2
        else:
            num_splits = TWAP_MAX_SPLITS
    else:
        num_splits = 1  # 거래량 정보 없으면 단일 주문

    # 분할 수량 계산
    split_qty = total_qty // (num_splits or 1)
    remainder = total_qty % num_splits
    split_quantities = [split_qty] * num_splits
    split_quantities[-1] += remainder  # 나머지를 마지막 분할에 추가

    print(f"  📊 [{MODE_LABEL}] TWAP 시작: {code} 총{total_qty}주 → {num_splits}분할")

    orders = []
    total_filled = 0
    splits_executed = 0

    for i, qty in enumerate(split_quantities):
        # 분할 간 대기 (첫 주문은 즉시)
        if i > 0:
            # 틱 속도 체크 (콜백 제공 시)
            if tick_speed_fn is not None:
                try:
                    current_tick = tick_speed_fn(code)
                    if current_tick < TWAP_TICK_SPEED_MIN:
                        print(f"    ⚠️  분할 {i+1}: 틱속도 부족 ({current_tick:.1f} < {TWAP_TICK_SPEED_MIN}) → 잔여 물량 Skip")
                        break
                except Exception as e:
                    pass  # 틱 체크 실패 시 계속 진행

            await asyncio.sleep(TWAP_INTERVAL_SEC)

        # IOC 주문 실행
        result = buy_ioc(code, qty, price)
        orders.append(result)
        splits_executed += 1

        if result.get("success"):
            # 체결 수량 확인
            order_no = result.get("order_no", "")
            if order_no:
                status = get_order_status(order_no)
                filled = status.get("filled_qty", 0)
                total_filled += filled
                print(f"    분할 {i+1}/{num_splits}: {filled}/{qty}주 체결")
            else:
                total_filled += qty  # 주문번호 없으면 전량 체결 가정
                print(f"    분할 {i+1}/{num_splits}: {qty}주 주문 완료")
        else:
            print(f"    ❌ 분할 {i+1}/{num_splits}: 주문 실패 → 잔여 물량 Skip")
            break

    success = total_filled > 0
    print(f"  {'✅' if success else '❌'} TWAP 완료: {total_filled}/{total_qty}주 체결 ({splits_executed}/{num_splits}분할)")

    twap_result = {
        "type": "BUY_TWAP",
        "success": success,
        "code": code,
        "total_qty": total_qty,
        "total_filled": total_filled,
        "splits_executed": splits_executed,
        "splits_planned": num_splits,
        "price": price,
        "mode": MODE_LABEL,
        "timestamp": datetime.now().isoformat(),
        "orders": orders,
    }
    _log_order(twap_result)
    return twap_result


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW — 주문 집행기 테스트")
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
        print("📁 order_executor.py 구조 확인 완료 — API 키 입력 후 재실행하세요.")
        exit(0)

    print("\n[1] 잔고 조회...")
    balance = get_balance()
    print(f"    {balance['cash']:,}원")
    for pos in balance['positions']:
        print(f"    {pos['name']}({pos['code']}): {pos['qty']}주  평단 {pos['avg_price']:,}원  수익 {pos['pnl_pct']:+.2f}%")

    print("\n[2] 매수 IOC 테스트 (삼성전자 1주 / 실제 실행됩니다!)")
    print("    ⚠️  실제 주문이 발생합니다. 테스트 시 주의하세요.")
    # result = buy_ioc("005930", qty=1, price=70000)
    # print(f"    결과: {result}")
    print("    (주석 해제 후 실행)")

    print("\n[3] 주문 로그 경로:", LOG_DIR)
    print("\n" + "=" * 55)
    print("  ✅ order_executor.py 구조 확인 완료!")
    print("=" * 55)
