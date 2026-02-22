# tools/trade_logger.py â ì¼ì¼ ë§¤ë§¤ ê¸°ë¡ ë¡ê±°
# ì¥ ì¤ ëª¨ë  ë§¤ë§¤ ì´ë²¤í¸ë¥¼ ìì§íê³ , ì¥ ë§ê° í ë¶ìì© JSON íì¼ë¡ ì ì¥
# ë§¤ì¼ Claudeì í¨ê» ë³µê¸°/ê°ì ì  ë¶ìì íì©

import json
import os
import threading
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# ââ ì ì¥ ê²½ë¡ âââââââââââââââââââââââââââââââââââââââââ
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "outputs", "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ââ ê¸ë¡ë² ì¼ì¼ ë¡ê·¸ ââââââââââââââââââââââââââââââââââ
_lock = threading.Lock()
_daily_log = {
    "date": None,
    "trades": [],           # ê°ë³ ë§¤ë§¤ ì´ë²¤í¸ ëª©ë¡
    "signals": [],          # ë°ìí ì í¸ (ë§¤ì ë¯¸ì¤í í¬í¨)
    "risk_events": [],      # ë¦¬ì¤í¬ ì´ë²¤í¸ (risk-off, ë´ì¤ ê²½ë³´ ë±)
    "macro_snapshot": {},   # ì¥ ìì ì ë§¤í¬ë¡ ë°ì´í° ì¤ëì·
    "performance": {},      # ì¥ ë§ê° ì ì±ê³¼ ìì½
}


def _today_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")


def _ensure_date():
    """ë ì§ê° ë°ëë©´ ë¡ê·¸ ì´ê¸°í"""
    today = _today_str()
    if _daily_log["date"] != today:
        _daily_log["date"] = today
        _daily_log["trades"] = []
        _daily_log["signals"] = []
        _daily_log["risk_events"] = []
        _daily_log["macro_snapshot"] = {}
        _daily_log["performance"] = {}


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ë§¤ë§¤ ê¸°ë¡ í¨ìë¤
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def log_trade(action: str, code: str, **kwargs):
    """
    ë§¤ë§¤ ì´ë²¤í¸ ê¸°ë¡

    Parameters:
        action: "BUY" | "SELL" | "STOP_LOSS" | "FORCE_CLOSE" | "PYRAMID"
        code: ì¢ëª©ì½ë (ì: "005930")
        **kwargs: ì¶ê° ì ë³´
            - price: ì²´ê²°ê°
            - quantity: ìë
            - position_pct: í¬ì§ì ë¹ì¨
            - eval_grade: íê° ë±ê¸ (A+, A, B, C, D, F)
            - eval_score: íê° ì ì
            - reason: ë§¤ë§¤ ì¬ì 
            - profit_pct: ì¤í ììµë¥  (ë§¤ë ì)
            - sector: ì¹í°
            - strategy: ì ëµ (ê³µê²©ì /ì¤ë¦½/ë°©ì´ì )
            - entry_price: ì§ìê° (ë§¤ë ì ì°¸ì¡°)
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
    ë§¤ì/ë§¤ë ì í¸ ê¸°ë¡ (ì¤í ì¬ë¶ì ë¬´ê´íê² ëª¨ë  ì í¸ë¥¼ ê¸°ë¡)

    Parameters:
        code: ì¢ëª©ì½ë
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
    ë¦¬ì¤í¬ ì´ë²¤í¸ ê¸°ë¡

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
    """ì¥ ìì ì ë§¤í¬ë¡ ë°ì´í° ì¤ëì· ì ì¥"""
    with _lock:
        _ensure_date()
        _daily_log["macro_snapshot"] = macro_data


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ì¼ì¼ ì±ê³¼ ê³ì° & ë¦¬í¬í¸ ìì±
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def calculate_performance(final_positions: dict = None, daily_loss: float = 0.0):
    """
    ì¥ ë§ê° ì ì¼ì¼ ì±ê³¼ ìì½ ê³ì°

    Parameters:
        final_positions: ì¥ ë§ê° ì ìì¡´ í¬ì§ì {code: {...}}
        daily_loss: ë¹ì¼ ì¤í ìì¤ë¥ 

    Returns:
        performance dict
    """
    with _lock:
        _ensure_date()
        trades = _daily_log["trades"]

        # ë§¤ë§¤ íµê³
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

        # ë±ê¸ë³ ë§¤ë§¤ ë¶í¬
        grade_distribution = {}
        for t in buys:
            grade = t.get("eval_grade", "N/A")
            grade_distribution[grade] = grade_distribution.get(grade, 0) + 1

        perf = {
            "date": _daily_log["date"],
            # ë§¤ë§¤ ê±´ì
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
    ì¼ì¼ ë§¤ë§¤ ë¦¬í¬í¸ë¥¼ JSON íì¼ë¡ ì ì¥

    Returns:
        ì ì¥ë íì¼ ê²½ë¡
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
    í¹ì  ë ì§ì ë¦¬í¬í¸ë¥¼ ë¡ë

    Parameters:
        date_str: "2026-02-21" íì

    Returns:
        ë¦¬í¬í¸ ëìëë¦¬ (ìì¼ë©´ ë¹ ëìëë¦¬)
    """
    filepath = os.path.join(REPORTS_DIR, f"trade_log_{date_str}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_recent_reports(days: int = 5) -> list:
    """
    ìµê·¼ Nì¼ê° ë¦¬í¬í¸ë¥¼ ë¡ë

    Parameters:
        days: ë¡ëí  ì¼ì (ê¸°ë³¸ 5ì¼)

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
    ìµê·¼ Nì¼ ëì  íµê³ â ì¥ê¸° ì¶ì¸ ë¶ìì©

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
        "win_rate": round(total_wins / total_closed, 3) if total_closed else 0,
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
#  ì¥ ë§ê° ë£¨í´ (main.pyìì í¸ì¶)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_daily_trades() -> list:
    """당일 매매 기록 리스트를 반환한다."""
    with _lock:
        _ensure_date()
        return list(_daily_log["trades"])


def end_of_day_routine(positions: dict = None, daily_loss: float = 0.0):
    """
    ì¥ ë§ê° ì í¸ì¶ëë íµí© ë£¨í´
    1) ì±ê³¼ ê³ì°
    2) JSON ë¦¬í¬í¸ ì ì¥
    3) íì¼ ê²½ë¡ ë°í

    Parameters:
        positions: ì¥ ë§ê° ì ìì¡´ í¬ì§ì
        daily_loss: ë¹ì¼ ëì  ì¤í ìì¤

    Returns:
        {"filepath": str, "performance": dict}
    """
    perf = calculate_performance(positions, daily_loss)
    filepath = export_daily_report()
    return {"filepath": filepath, "performance": perf}
