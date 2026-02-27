# shared_state.py — 에이전트 간 공유 상태 관리
# threading.Lock을 사용하여 thread-safe 접근 보장

import threading
from datetime import datetime
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

    # 기능3: V자 반등 Re-entry
    "recovery_state": "NONE",    # NONE / WATCHING / RECOVERED
    "risk_off_time": None,       # Risk-Off 선언 시각 (ISO 문자열)
    "reentry_count": 0,          # 당일 재진입 횟수

    # 기능2: 섹터 Momentum Delta
    "sector_scores_morning": {},  # 09:20 섹터별 점수 캐시
    "sector_scores_midday": {},   # 11:30 섹터별 점수 캐시

    # 기능6: 섹터 멀티플라이어
    "sector_multipliers": {},     # {"반도체": 1.2, "내수": 0.8, ...}

    # Agent 1 → 전체: 거시 리스크 상태
    "macro_risk": "ON",              # ON / OFF / CAUTION
    "macro_confidence": 50,          # 거시 분석 신뢰도 (0~100)
    "macro_urgent": "NONE",          # 긴급 플래그: NONE / HIGH / CRITICAL
    "force_exit": False,             # 긴급 전량 청산 시그널
    "strategist_result": {},         # Agent 3 → 전체: 전략 실행 결과

    # 에이전트 간 데이터 전달 키
    "macro_result": {},           # Agent 1 → Agent 3: 거시 분석 결과
    "macro_sectors": [],          # Agent 1 → Agent 2: 추천 섹터
    "macro_avoid_sectors": [],    # Agent 1 → Agent 2: 회피 섹터
    "preferred_sectors": [],      # Agent 2 내부: 선호 섹터
    "scanner_result": {},         # Agent 2 → Agent 3: 스캔 결과
    "vix_level": "NORMAL",        # Agent 4: VIX 레벨 (NORMAL/CAUTION/HIGH/EXTREME)
    "vix_value": None,            # Agent 4: VIX 현재값
    "vix_trail_adjustment": 1.0,  # Agent 4: 트레일링 스탑 조정 배수

    # ========== 2트랙 전략 관련 (2026-02-25) ==========
    # 15분봉 추세 정보 (Agent 4 → Agent 2,3)
    "tf15_trends": {},            # {종목코드: {"trend":"UP","aligned":True,"ma3":..}}
    "tf5_trends": {},             # {종목코드: {"trend":"UP","aligned":True,...}}

    # Track 분류 (Agent 3 관리)
    "track_info": {},             # {종목코드: {"track": 1 or 2, "entry_time": str,
                                  #            "entry_price": float, "max_pnl_pct": float}}

    # Track 2 오버나이트 후보 (14:30 평가)
    "overnight_candidates": [],   # [{"code":str, "pnl_pct":float, "catalyst":str}, ...]

    # 체결강도 실시간 (Agent 4 웹소켓 → Agent 3)
    "realtime_chg_strength": {},  # {종목코드: float} — 최근 체결강도
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


def update_position(code: str, updates: dict):
    """포지션 부분 업데이트 (thread-safe)"""
    with _lock:
        if code in shared_state["positions"]:
            shared_state["positions"][code].update(updates)


def get_positions() -> dict:
    """현재 보유 포지션 전체 반환 (thread-safe)"""
    with _lock:
        return dict(shared_state["positions"])


# ── 2트랙 전략 헬퍼 ─────────────────────────────────────────────

def set_tf15_trend(code: str, trend_data: dict):
    """15분봉 추세 정보 갱신 (Agent 4 → Agent 3)"""
    with _lock:
        shared_state["tf15_trends"][code] = trend_data


def get_tf15_trend(code: str) -> dict:
    """15분봉 추세 조회"""
    with _lock:
        return shared_state["tf15_trends"].get(code, {})


def set_track_info(code: str, track: int, entry_price: float = 0,
                   entry_time: str = ""):
    """포지션 Track 분류 설정 (1=장중, 2=오버나이트)"""
    with _lock:
        shared_state["track_info"][code] = {
            "track": track,
            "entry_price": entry_price,
            "entry_time": entry_time or datetime.now().strftime("%H:%M:%S"),
            "max_pnl_pct": 0.0,
        }


def get_track_info(code: str) -> dict:
    """포지션 Track 정보 조회"""
    with _lock:
        return shared_state["track_info"].get(code, {})


def update_track_pnl(code: str, current_pnl_pct: float):
    """Track 포지션 최대 수익률 갱신"""
    with _lock:
        info = shared_state["track_info"].get(code)
        if info and current_pnl_pct > info.get("max_pnl_pct", 0):
            info["max_pnl_pct"] = current_pnl_pct


def set_chg_strength(code: str, value: float):
    """실시간 체결강도 갱신 (Agent 4 웹소켓)"""
    with _lock:
        shared_state["realtime_chg_strength"][code] = value


def get_chg_strength(code: str) -> float:
    """실시간 체결강도 조회"""
    with _lock:
        return shared_state["realtime_chg_strength"].get(code, 0.0)
