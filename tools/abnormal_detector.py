# tools/abnormal_detector.py
# Individual stock abnormal trading pattern detection
# Monitors held positions for unusual volume, price gaps, and spread anomalies

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("abnormal_detector")
KST = timezone(timedelta(hours=9))

_lock = threading.Lock()

# Alert history to prevent duplicate notifications
_alert_history = {}

# Configuration
DETECTION_CONFIG = {
    "volume_spike_ratio": 3.0,        # Current volume vs 20-day avg
    "price_gap_threshold_pct": 3.0,   # Intraday gap from previous close
    "spread_anomaly_ratio": 2.0,      # Current spread vs avg spread
    "sudden_drop_pct": -2.5,          # Rapid price drop threshold
    "sudden_surge_pct": 5.0,          # Rapid price surge threshold
    "alert_cooldown_minutes": 30,     # Min interval between same alerts
    "vi_trigger_pct": 2.0,            # Volatility Interruption threshold
}


class AnomalyAlert:
    """Represents a detected anomaly."""

    def __init__(self, code: str, alert_type: str, severity: str,
                 description: str, data: dict = None):
        self.code = code
        self.alert_type = alert_type
        self.severity = severity  # "info", "warning", "critical"
        self.description = description
        self.data = data or {}
        self.timestamp = datetime.now(KST).isoformat()

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "description": self.description,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    def __repr__(self):
        return f"AnomalyAlert({self.code}, {self.alert_type}, {self.severity})"


def _check_cooldown(code: str, alert_type: str) -> bool:
    """Check if we should suppress this alert due to cooldown."""
    key = f"{code}_{alert_type}"
    now = datetime.now(KST)
    with _lock:
        last_alert = _alert_history.get(key)
        if last_alert:
            elapsed = (now - last_alert).total_seconds() / 60
            if elapsed < DETECTION_CONFIG["alert_cooldown_minutes"]:
                return False  # Still in cooldown
        _alert_history[key] = now
    return True


def detect_volume_spike(code: str, current_volume: int,
                        avg_volume_20d: float) -> Optional[AnomalyAlert]:
    """
    Detect abnormal volume spike for a stock.

    Parameters:
        code: Stock code
        current_volume: Current trading volume
        avg_volume_20d: 20-day average volume

    Returns:
        AnomalyAlert if spike detected, None otherwise
    """
    if avg_volume_20d <= 0:
        return None

    ratio = current_volume / avg_volume_20d

    if ratio >= DETECTION_CONFIG["volume_spike_ratio"]:
        if not _check_cooldown(code, "volume_spike"):
            return None

        severity = "critical" if ratio >= 5.0 else "warning"
        return AnomalyAlert(
            code=code,
            alert_type="VOLUME_SPIKE",
            severity=severity,
            description=f"{code}: Volume spike {ratio:.1f}x vs 20-day avg",
            data={
                "current_volume": current_volume,
                "avg_volume_20d": avg_volume_20d,
                "ratio": round(ratio, 2),
            },
        )
    return None


def detect_price_gap(code: str, current_price: float,
                     prev_close: float) -> Optional[AnomalyAlert]:
    """
    Detect abnormal price gap from previous close.

    Parameters:
        code: Stock code
        current_price: Current trading price
        prev_close: Previous day closing price

    Returns:
        AnomalyAlert if gap detected, None otherwise
    """
    if prev_close <= 0:
        return None

    gap_pct = ((current_price - prev_close) / prev_close) * 100

    threshold = DETECTION_CONFIG["price_gap_threshold_pct"]
    if abs(gap_pct) >= threshold:
        if not _check_cooldown(code, "price_gap"):
            return None

        direction = "UP" if gap_pct > 0 else "DOWN"
        severity = "critical" if abs(gap_pct) >= threshold * 2 else "warning"

        return AnomalyAlert(
            code=code,
            alert_type=f"PRICE_GAP_{direction}",
            severity=severity,
            description=f"{code}: Price gap {direction} {gap_pct:+.2f}% from prev close",
            data={
                "current_price": current_price,
                "prev_close": prev_close,
                "gap_pct": round(gap_pct, 2),
            },
        )
    return None


def detect_sudden_move(code: str, price_change_pct: float,
                       time_window_minutes: int = 5) -> Optional[AnomalyAlert]:
    """
    Detect sudden price movement within a short time window.

    Parameters:
        code: Stock code
        price_change_pct: Price change percentage in the time window
        time_window_minutes: Time window for the change

    Returns:
        AnomalyAlert if sudden move detected, None otherwise
    """
    drop_threshold = DETECTION_CONFIG["sudden_drop_pct"]
    surge_threshold = DETECTION_CONFIG["sudden_surge_pct"]

    if price_change_pct <= drop_threshold:
        if not _check_cooldown(code, "sudden_drop"):
            return None

        severity = "critical" if price_change_pct <= drop_threshold * 1.5 else "warning"
        return AnomalyAlert(
            code=code,
            alert_type="SUDDEN_DROP",
            severity=severity,
            description=(
                f"{code}: Sudden drop {price_change_pct:.2f}% "
                f"in {time_window_minutes}min"
            ),
            data={
                "price_change_pct": round(price_change_pct, 2),
                "time_window_minutes": time_window_minutes,
            },
        )

    if price_change_pct >= surge_threshold:
        if not _check_cooldown(code, "sudden_surge"):
            return None

        return AnomalyAlert(
            code=code,
            alert_type="SUDDEN_SURGE",
            severity="warning",
            description=(
                f"{code}: Sudden surge +{price_change_pct:.2f}% "
                f"in {time_window_minutes}min"
            ),
            data={
                "price_change_pct": round(price_change_pct, 2),
                "time_window_minutes": time_window_minutes,
            },
        )

    return None


def detect_vi_trigger(code: str, price_change_pct: float) -> Optional[AnomalyAlert]:
    """
    Detect potential Volatility Interruption (VI) trigger.
    Korean market triggers VI when price moves more than a threshold
    from the reference price within a short period.

    Parameters:
        code: Stock code
        price_change_pct: Price change from VI reference price

    Returns:
        AnomalyAlert if VI likely, None otherwise
    """
    vi_threshold = DETECTION_CONFIG["vi_trigger_pct"]

    if abs(price_change_pct) >= vi_threshold:
        if not _check_cooldown(code, "vi_trigger"):
            return None

        direction = "UP" if price_change_pct > 0 else "DOWN"
        return AnomalyAlert(
            code=code,
            alert_type=f"VI_TRIGGER_{direction}",
            severity="critical",
            description=(
                f"{code}: Possible VI trigger - "
                f"{price_change_pct:+.2f}% from reference"
            ),
            data={
                "price_change_pct": round(price_change_pct, 2),
                "vi_threshold": vi_threshold,
            },
        )
    return None


def scan_held_positions(positions: dict, market_data: dict) -> list:
    """
    Scan all held positions for anomalies.

    Parameters:
        positions: Dict of held positions {code: position_info}
        market_data: Dict of current market data {code: market_info}

    Returns:
        List of AnomalyAlert objects
    """
    alerts = []

    for code, pos_info in positions.items():
        mdata = market_data.get(code, {})
        if not mdata:
            continue

        current_price = mdata.get("current_price", 0)
        prev_close = mdata.get("prev_close", 0)
        current_volume = mdata.get("current_volume", 0)
        avg_volume = mdata.get("avg_volume_20d", 0)
        price_change_5min = mdata.get("price_change_5min_pct", 0)

        # Volume spike check
        alert = detect_volume_spike(code, current_volume, avg_volume)
        if alert:
            alerts.append(alert)

        # Price gap check
        alert = detect_price_gap(code, current_price, prev_close)
        if alert:
            alerts.append(alert)

        # Sudden movement check
        if price_change_5min != 0:
            alert = detect_sudden_move(code, price_change_5min)
            if alert:
                alerts.append(alert)

        # VI trigger check
        ref_price = mdata.get("vi_reference_price", prev_close)
        if ref_price > 0 and current_price > 0:
            vi_change = ((current_price - ref_price) / ref_price) * 100
            alert = detect_vi_trigger(code, vi_change)
            if alert:
                alerts.append(alert)

    # Sort by severity
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: severity_order.get(a.severity, 99))

    if alerts:
        logger.warning(
            f"Anomaly scan: {len(alerts)} alerts detected for "
            f"{len(positions)} positions"
        )

    return alerts


def format_alerts_text(alerts: list) -> str:
    """Format alerts for Telegram notification."""
    if not alerts:
        return ""

    severity_emoji = {
        "critical": "[!!!]",
        "warning": "[!!]",
        "info": "[i]",
    }

    lines = ["=== ANOMALY ALERTS ==="]
    for alert in alerts:
        emoji = severity_emoji.get(alert.severity, "")
        lines.append(f"{emoji} {alert.description}")

    return "\n".join(lines)


def clear_history():
    """Clear alert history (for testing or daily reset)."""
    with _lock:
        _alert_history.clear()
        logger.info("Alert history cleared")
