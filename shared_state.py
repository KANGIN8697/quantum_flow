# shared_state.py — 에이전트 간 공유 상태 관리
# threading.Lock을 사용하여 thread-safe 접근 보장

import threading
from config.settings import INITIAL_STOP_ATR, TRAILING_STOP_ATR

shared_state = {
    "risk_off": False,           # True 시 전 매매 중단
    "positions": {},             # 종목코드: {진입가, 섹터, 수익률, pyramiding_done, ...}
    "queue": [],                 # 대기열: [{종목코드, 점수}, ...]
    "daily_loss": 0.0,           # 당일 누적 실현 손실 (%)
    "watch_list": [],            # 감시 풀 종목 리스트
    "risk_params": {
        "risk_level": "NORMAL",          # NORMAL / HIGH / CRITICAL
        "stop_loss_multiplier": 2.0,
        "pyramiding_allowed": True,
        "banned_sectors": [],
        "emergency_liquidate": False,
    },
    "blacklist": [],             # 당일 매매 금지 종목
}

_lock = threading.Lock()


def get_state(key):
    """공유 상태에서 값을 읽어옴 (thread-safe)"""
    with _lock:
        return shared_state.get(key)


def set_state(key, value):
    """공유 상태에 값을 설정 (thread-safe)"""
    with _lock:
        shared_state[key] = value


def update_risk_params(params: dict):
    """risk_params를 부분 업데이트 (thread-safe)"""
    with _lock:
        shared_state["risk_params"].update(params)


def add_position(code: str, data: dict):
    """
    포지션 추가 (thread-safe)
    필수 필드:
        entry_price, quantity, sector, eval_grade, eval_score
    ATR 손절 필드 (자동 추가):
        atr_value, stop_loss_price, stop_loss_type, highest_since_entry
    """
    with _lock:
        # ATR 손절 필드 기본값 보장
        if "atr_value" not in data:
            data["atr_value"] = 0.0
        if "stop_loss_price" not in data:
            # 초기 손절: entry_price - (ATR × INITIAL_STOP_ATR)
            entry = data.get("entry_price", 0)
            atr = data.get("atr_value", 0)
            # INITIAL_STOP_ATR는 모듈 상단에서 import 완료
            data["stop_loss_price"] = entry - (atr * INITIAL_STOP_ATR) if atr > 0 else 0
        if "stop_loss_type" not in data:
            data["stop_loss_type"] = "initial"  # "initial" | "trailing"
        if "highest_since_entry" not in data:
            data["highest_since_entry"] = data.get("entry_price", 0)
        shared_state["positions"][code] = data


def update_position_stop(code: str, current_price: float):
    """
    포지션 트레일링 스톱 업데이트 (thread-safe)
    현재가가 최고가를 경신하면 손절가도 따라 올림
    Returns: stop_triggered (bool)
    """
    # TRAILING_STOP_ATR는 모듈 상단에서 import 완료
    with _lock:
        pos = shared_state["positions"].get(code)
        if not pos:
            return False

        # 최고가 갱신
        if current_price > pos.get("highest_since_entry", 0):
            pos["highest_since_entry"] = current_price
            # 트레일링 스톱으로 전환
            atr = pos.get("atr_value", 0)
            if atr > 0:
                new_stop = current_price - (atr * TRAILING_STOP_ATR)
                # 손절가는 올라가기만 함 (내려가지 않음)
                if new_stop > pos.get("stop_loss_price", 0):
                    pos["stop_loss_price"] = new_stop
                    pos["stop_loss_type"] = "trailing"

        # 손절 트리거 체크
        return current_price <= pos.get("stop_loss_price", 0)


def remove_position(code: str):
    """포지션 제거 (thread-safe)"""
    with _lock:
        shared_state["positions"].pop(code, None)


def add_to_blacklist(code: str):
    """당일 매매 금지 종목 추가 (thread-safe)"""
    with _lock:
        if code not in shared_state["blacklist"]:
            shared_state["blacklist"].append(code)


def get_positions() -> dict:
    """현재 보유 포지션 전체 반환 (thread-safe)"""
    with _lock:
        return dict(shared_state["positions"])
