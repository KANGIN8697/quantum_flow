# shared_state.py — 에이전트 간 공유 상태 관리
# threading.Lock을 사용하여 thread-safe 접근 보장

import threading

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

    # 기능3: V자 반등 Re-entry
    "recovery_state": "NONE",    # NONE / WATCHING / RECOVERED
    "risk_off_time": None,       # Risk-Off 선언 시각 (ISO 문자열)
    "reentry_count": 0,          # 당일 재진입 횟수

    # 기능2: 섹터 Momentum Delta
    "sector_scores_morning": {},  # 09:20 섹터별 점수 캐시
    "sector_scores_midday": {},   # 11:30 섹터별 점수 캐시

    # 기능6: 섹터 멀티플라이어
    "sector_multipliers": {},     # {"반도체": 1.2, "내수": 0.8, ...}
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
    """포지션 추가 (thread-safe)"""
    with _lock:
        shared_state["positions"][code] = data


def remove_position(code: str):
    """포지션 제거 (thread-safe)"""
    with _lock:
        shared_state["positions"].pop(code, None)


def add_to_blacklist(code: str):
    """당일 매매 금지 종목 추가 (thread-safe)"""
    with _lock:
        if code not in shared_state["blacklist"]:
            shared_state["blacklist"].append(code)


def update_position(code: str, updates: dict):
    """포지션 부분 업데이트 (thread-safe)"""
    with _lock:
        if code in shared_state["positions"]:
            shared_state["positions"][code].update(updates)


def get_positions() -> dict:
    """현재 보유 포지션 전체 반환 (thread-safe)"""
    with _lock:
        return dict(shared_state["positions"])
