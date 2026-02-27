# tools/trade_logger.py — 일일 매매 기록 로거
# 장 중 모든 매매 이벤트를 수집하고, 장 마감 후 분석용 JSON 파일로 저장
# 매일 Claude와 함께 복기/개선점 분석에 활용

import json
import os
import threading
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# ── 저장 경로 ─────────────────────────────────────────
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "outputs", "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ── 글로벌 일일 로그 ──────────────────────────────────
_lock = threading.Lock()
_daily_log = {
    "date": None,
    "trades": [],           # 개별 매매 이벤트 목록
    "signals": [],          # 발생한 신호 (매수 미실행 포함)
    "risk_events": [],      # 리스크 이벤트 (risk-off, 뉴스 경보 등)
    "macro_snapshot": {},   # 장 시작 시 매크로 데이터 스냅샷
    "performance": {},      # 장 마감 시 성과 요약
}


def _today_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")


def _ensure_date():
    """날짜가 바뀌면 로그 초기화"""
    today = _today_str()
    if _daily_log["date"] != today:
        _daily_log["date"] = today
        _daily_log["trades"] = []
        _daily_log["signals"] = []
        _daily_log["risk_events"] = []
        _daily_log["macro_snapshot"] = {}
        _daily_log["performance"] = {}


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  매매 기록 함수들
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def log_trade(action: str, code: str, **kwargs):
    """
    매매 이벤트 기록

    Parameters:
        action: "BUY" | "SELL" | "STOP_LOSS" | "FORCE_CLOSE" | "PYRAMID"
        code: 종목코드 (예: "005930")
        **kwargs: 추가 정보
            - price: 체결가
            - quantity: ìë
            - position_pct: í¬ì§ì ë¹ì¨
            - eval_grade: íê° ë±ê¸ (A+, A, B, C, D, F)
            - eval_score: íê° ì ì
            - reason: 매매 사유
            - profit_pct: 실현 수익률 (매도 시)
            - sector: ì¹í°
            - strategy: 전략 (공격적/중립/방어적)
            - entry_price: 진입가 (매도 시 참조)
    """
    with _lock:
        _ensure_date()
        record = {
            "timestamp": _now_str(),
            "action": action,
            "code": code,
            **kwargs,
        }
        _daily_log["trades"].append(record)
    return record


def log_signal(code: str, signal_type: str, **kwargs):
    """
    매수/매도 신호 기록 (실행 여부와 무관하게 모든 신호를 기록)

    Parameters:
        code: 종목코드
        signal_type: "BUY_SIGNAL" | "SELL_SIGNAL" | "PYRAMID_SIGNAL" | "STOP_SIGNAL"
        **kwargs:
            - executed: True/False (ì¤ì  ì¤í ì¬ë¶)
            - skip_reason: ë¯¸ì¤í ì¬ì  (ì: "max_positions", "daily_loss_limit", "blacklisted")
            - eval_grade: íê° ë±ê¸
            - eval_score: íê° ì ì
            - score_breakdown: ì¸ë¶ ì ì ëìëë¦¬
    """
    with _lock:
        _ensure_date()
        record = {
            "timestamp": _now_str(),
            "code": code,
            "signal_type": signal_type,
            **kwargs,
        }
        _daily_log["signals"].append(record)
    return record


def log_risk_event(event_type: str, **kwargs):
    """
    리스크 이벤트 기록

    Parameters:
        event_type: "RISK_OFF" | "RISK_LEVEL_CHANGE" | "NEWS_ALERT" |
                    "DAILY_LOSS_LIMIT" | "EMERGENCY_LIQUIDATE" | "FORCE_CLOSE"
        **kwargs:
            - level: "NORMAL" | "HIGH" | "CRITICAL"
            - trigger: í¸ë¦¬ê±° ìì¸
            - message: ì¤ëª
    """
    with _lock:
        _ensure_date()
        record = {
            "timestamp": _now_str(),
            "event_type": event_type,
            **kwargs,
        }
        _daily_log["risk_events"].append(record)
    return record


def set_macro_snapshot(macro_data: dict):
    """장 시작 시 매크로 데이터 스냅샷 저장"""
    with _lock:
        _ensure_date()
        _daily_log["macro_snapshot"] = macro_data


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ì¼ì¼ ì±ê³¼ ê³ì° & ë¦¬í¬í¸ ìì±
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def calculate_performance(final_positions: dict = None, daily_loss: float = 0.0):
    """
    장 마감 시 일일 성과 요약 계산

    Parameters:
        final_positions: 장 마감 시 잔존 포지션 {code: {...}}
        daily_loss: ë¹ì¼ ì¤í ìì¤ë¥ 

    Returns:
        performance dict
    """
    with _lock:
        _ensure_date()
        trades = _daily_log["trades"]

        # 매매 통계
        buys = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] in ("SELL", "STOP_LOSS", "FORCE_CLOSE")]
        pyramids = [t for t in trades if t["action"] == "PYRAMID"]

        # ììµë¥  ë¶ì
        realized_profits = [t.get("profit_pct", 0) for t in sells if "profit_pct" in t]
        winning = [p for p in realized_profits if p > 0]
        losing = [p for p in realized_profits if p < 0]

        # ì í¸ ë¶ì
        signals = _daily_log["signals"]
        buy_signals = [s for s in signals if s["signal_type"] == "BUY_SIGNAL"]
        executed_signals = [s for s in buy_signals if s.get("executed")]
        skipped_signals = [s for s in buy_signals if not s.get("executed")]

        # ì¤íµ ì¬ì  ë¶ë¥
        skip_reasons = {}
        for s in skipped_signals:
            reason = s.get("skip_reason", "unknown")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        # 등급별 매매 분포
        grade_distribution = {}
        for t in buys:
            grade = t.get("eval_grade", "N/A")
            grade_distribution[grade] = grade_distribution.get(grade, 0) + 1

        perf = {
            "date": _daily_log["date"],
            # 매매 건수
            "total_trades": len(trades),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "pyramid_count": len(pyramids),
            # ììµë¥ 
            "realized_pnl": sum(realized_profits) if realized_profits else 0,
            "daily_loss": daily_loss,
            "win_count": len(winning),
            "loss_count": len(losing),
            "win_rate": len(winning) / len(realized_profits) if realized_profits else 0,
            "avg_win": sum(winning) / len(winning) if winning else 0,
            "avg_loss": sum(losing) / len(losing) if losing else 0,
            "best_trade": max(realized_profits) if realized_profits else 0,
            "worst_trade": min(realized_profits) if realized_profits else 0,
            # ì í¸ ë¶ì
            "total_signals": len(buy_signals),
            "executed_signals": len(executed_signals),
            "skipped_signals": len(skipped_signals),
            "skip_reasons": skip_reasons,
            # ë±ê¸ ë¶í¬
            "grade_distribution": grade_distribution,
            # ë¦¬ì¤í¬ ì´ë²¤í¸
            "risk_event_count": len(_daily_log["risk_events"]),
            "risk_off_triggered": any(
                e["event_type"] == "RISK_OFF" for e in _daily_log["risk_events"]
            ),
            # ìì¡´ í¬ì§ì
            "remaining_positions": len(final_positions) if final_positions else 0,
            "remaining_codes": list(final_positions.keys()) if final_positions else [],
        }

        _daily_log["performance"] = perf
        return perf


def export_daily_report() -> str:
    """
    일일 매매 리포트를 JSON 파일로 저장

    Returns:
        저장된 파일 경로
    """
    with _lock:
        _ensure_date()
        date_str = _daily_log["date"]
        filename = f"trade_log_{date_str}.json"
        filepath = os.path.join(REPORTS_DIR, filename)

        report = {
            "version": "1.0",
            "generated_at": _now_str(),
            "date": date_str,
            "summary": _daily_log.get("performance", {}),
            "macro_snapshot": _daily_log.get("macro_snapshot", {}),
            "trades": _daily_log["trades"],
            "signals": _daily_log["signals"],
            "risk_events": _daily_log["risk_events"],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return filepath


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ë¶ìì© ì í¸ë¦¬í°
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def load_daily_report(date_str: str) -> dict:
    """
    특정 날짜의 리포트를 로드

    Parameters:
        date_str: "2026-02-21" íì

    Returns:
        리포트 딕셔너리 (없으면 빈 딕셔너리)
    """
    filepath = os.path.join(REPORTS_DIR, f"trade_log_{date_str}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_recent_reports(days: int = 5) -> list:
    """
    최근 N일간 리포트를 로드

    Parameters:
        days: 로드할 일수 (기본 5일)

    Returns:
        ë¦¬í¬í¸ ë¦¬ì¤í¸ (ìµì ì)
    """
    reports = []
    today = datetime.now(KST).date()
    for i in range(days):
        d = today - timedelta(days=i)
        report = load_daily_report(d.strftime("%Y-%m-%d"))
        if report:
            reports.append(report)
    return reports


def get_cumulative_stats(days: int = 20) -> dict:
    """
    최근 N일 누적 통계 — 장기 추세 분석용

    Returns:
        ëì  íµê³ ëìëë¦¬
    """
    reports = load_recent_reports(days)
    if not reports:
        return {"message": "ë°ì´í° ìì", "trading_days": 0}

    total_trades = 0
    total_buys = 0
    total_sells = 0
    total_pnl = 0.0
    total_wins = 0
    total_losses = 0
    all_win_pcts = []
    all_loss_pcts = []
    grade_totals = {}
    risk_off_days = 0
    skip_reason_totals = {}

    for r in reports:
        s = r.get("summary", {})
        total_trades += s.get("total_trades", 0)
        total_buys += s.get("buy_count", 0)
        total_sells += s.get("sell_count", 0)
        total_pnl += s.get("realized_pnl", 0)
        total_wins += s.get("win_count", 0)
        total_losses += s.get("loss_count", 0)

        if s.get("avg_win"):
            all_win_pcts.append(s["avg_win"])
        if s.get("avg_loss"):
            all_loss_pcts.append(s["avg_loss"])

        if s.get("risk_off_triggered"):
            risk_off_days += 1

        for grade, count in s.get("grade_distribution", {}).items():
            grade_totals[grade] = grade_totals.get(grade, 0) + count

        for reason, count in s.get("skip_reasons", {}).items():
            skip_reason_totals[reason] = skip_reason_totals.get(reason, 0) + count

    total_closed = total_wins + total_losses
    return {
        "trading_days": len(reports),
        "total_trades": total_trades,
        "total_buys": total_buys,
        "total_sells": total_sells,
        "cumulative_pnl": round(total_pnl, 4),
        "win_count": total_wins,
        "loss_count": total_losses,
        "win_rate": round(total_wins / (total_closed or 1), 3) if total_closed else 0,
        "avg_win": round(sum(all_win_pcts) / len(all_win_pcts), 4) if all_win_pcts else 0,
        "avg_loss": round(sum(all_loss_pcts) / len(all_loss_pcts), 4) if all_loss_pcts else 0,
        "profit_factor": abs(
            (sum(all_win_pcts) * total_wins) / (sum(all_loss_pcts) * total_losses)
        ) if all_loss_pcts and total_losses else 0,
        "risk_off_days": risk_off_days,
        "grade_distribution": grade_totals,
        "skip_reasons": skip_reason_totals,
    }


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  장 마감 루틴 (main.py에서 호출)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_daily_trades() -> list:
    """당일 매매 기록 리스트를 반환한다."""
    with _lock:
        _ensure_date()
        return list(_daily_log["trades"])


def end_of_day_routine(positions: dict = None, daily_loss: float = 0.0):
    """
    장 마감 시 호출되는 통합 루틴
    1) ì±ê³¼ ê³ì°
    2) JSON ë¦¬í¬í¸ ì ì¥
    3) 파일 경로 반환

    Parameters:
        positions: 장 마감 시 잔존 포지션
        daily_loss: ë¹ì¼ ëì  ì¤í ìì¤

    Returns:
        {"filepath": str, "performance": dict}
    """
    perf = calculate_performance(positions, daily_loss)
    filepath = export_daily_report()
    return {"filepath": filepath, "performance": perf}
