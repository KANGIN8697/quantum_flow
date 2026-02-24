# tools/position_scaler.py
# Gradual position size reduction on consecutive losses
# Integrates with HeadStrategist for dynamic risk management

import logging
import threading
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("position_scaler")
KST = timezone(timedelta(hours=9))

_lock = threading.Lock()

# Position scaling state
_state = {
    "consecutive_losses": 0,
    "last_trade_result": None,  # "win" or "loss"
    "current_scale": 1.0,       # 1.0 = 100%, 0.5 = 50%
    "total_losses_today": 0,
    "recovery_mode": False,
    "last_updated": None,
}

# Configuration
SCALE_CONFIG = {
    "loss_thresholds": [
        # (consecutive_losses, scale_factor, description)
        (1, 1.0, "Normal: no reduction"),
        (2, 0.75, "Caution: 25% reduction after 2 consecutive losses"),
        (3, 0.50, "Warning: 50% reduction after 3 consecutive losses"),
        (4, 0.25, "Critical: 75% reduction after 4 consecutive losses"),
        (5, 0.0, "Stop: trading halted after 5 consecutive losses"),
    ],
    "recovery_wins_needed": 2,      # Wins needed to step up one level
    "max_daily_losses": 5,          # Max individual losing trades per day
    "reset_on_new_day": True,       # Reset consecutive count each morning
}


def get_current_scale() -> float:
    """Get the current position scale factor (0.0 ~ 1.0)."""
    with _lock:
        return _state["current_scale"]


def get_state() -> dict:
    """Get full position scaler state (read-only copy)."""
    with _lock:
        return dict(_state)


def _calculate_scale(consecutive_losses: int) -> float:
    """Determine scale factor based on consecutive loss count."""
    scale = 1.0
    for threshold, factor, _ in SCALE_CONFIG["loss_thresholds"]:
        if consecutive_losses >= threshold:
            scale = factor
    return scale


def record_trade_result(pnl_pct: float) -> dict:
    """
    Record a trade result and update position scaling.

    Parameters:
        pnl_pct: Profit/loss percentage of the trade

    Returns:
        dict with updated state info and any triggered actions
    """
    with _lock:
        prev_scale = _state["current_scale"]
        prev_losses = _state["consecutive_losses"]

        if pnl_pct < 0:
            # Loss
            _state["consecutive_losses"] += 1
            _state["total_losses_today"] += 1
            _state["last_trade_result"] = "loss"
            _state["recovery_mode"] = False
        elif pnl_pct > 0:
            # Win
            _state["last_trade_result"] = "win"
            if _state["consecutive_losses"] > 0:
                _state["recovery_mode"] = True
                # Gradual recovery: reduce loss count by 1 for each win
                _state["consecutive_losses"] = max(0, _state["consecutive_losses"] - 1)
        else:
            # Breakeven - no change
            _state["last_trade_result"] = "even"

        # Calculate new scale
        new_scale = _calculate_scale(_state["consecutive_losses"])
        _state["current_scale"] = new_scale
        _state["last_updated"] = datetime.now(KST).isoformat()

        # Determine action
        action = None
        if new_scale == 0.0:
            action = "HALT_TRADING"
        elif new_scale < prev_scale:
            action = "SCALE_DOWN"
        elif new_scale > prev_scale:
            action = "SCALE_UP"
        elif _state["total_losses_today"] >= SCALE_CONFIG["max_daily_losses"]:
            action = "DAILY_LOSS_LIMIT"
            _state["current_scale"] = 0.0

        result = {
            "action": action,
            "prev_scale": prev_scale,
            "new_scale": _state["current_scale"],
            "consecutive_losses": _state["consecutive_losses"],
            "total_losses_today": _state["total_losses_today"],
            "recovery_mode": _state["recovery_mode"],
        }

        if action:
            logger.warning(
                f"Position scale changed: {prev_scale:.0%} -> {_state['current_scale']:.0%} "
                f"(consecutive_losses={_state['consecutive_losses']}, action={action})"
            )

        return result


def apply_scale_to_quantity(base_quantity: int) -> int:
    """
    Apply current scale factor to a base order quantity.

    Parameters:
        base_quantity: Original intended quantity

    Returns:
        Scaled quantity (minimum 0)
    """
    scale = get_current_scale()
    scaled = int(base_quantity * scale)
    if scaled <= 0 and scale > 0:
        scaled = 1  # Minimum 1 share if not fully halted
    return max(0, scaled)


def apply_scale_to_positions(max_positions: int) -> int:
    """
    Apply current scale factor to max concurrent positions.

    Parameters:
        max_positions: Original max positions setting

    Returns:
        Scaled max positions count
    """
    scale = get_current_scale()
    scaled = max(1, int(max_positions * scale))
    if scale == 0.0:
        return 0
    return scaled


def reset_daily():
    """Reset daily counters (called at start of trading day)."""
    with _lock:
        if SCALE_CONFIG["reset_on_new_day"]:
            _state["total_losses_today"] = 0
            # Keep consecutive losses across days for safety
            # but cap recovery at one level per day
            logger.info(
                f"Daily reset: consecutive_losses={_state['consecutive_losses']}, "
                f"scale={_state['current_scale']:.0%}"
            )


def force_reset():
    """Force reset all state (manual override)."""
    with _lock:
        _state["consecutive_losses"] = 0
        _state["last_trade_result"] = None
        _state["current_scale"] = 1.0
        _state["total_losses_today"] = 0
        _state["recovery_mode"] = False
        _state["last_updated"] = datetime.now(KST).isoformat()
        logger.info("Position scaler force reset to defaults")


def get_scale_description() -> str:
    """Get human-readable description of current scaling status."""
    with _lock:
        losses = _state["consecutive_losses"]
        scale = _state["current_scale"]
        recovery = _state["recovery_mode"]

        if scale == 0.0:
            return f"TRADING HALTED ({losses} consecutive losses)"
        elif scale < 1.0:
            status = "RECOVERING" if recovery else "REDUCED"
            return f"{status}: {scale:.0%} capacity ({losses} consecutive losses)"
        else:
            return "NORMAL: 100% capacity"
