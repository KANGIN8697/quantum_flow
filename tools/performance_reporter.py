# tools/performance_reporter.py
# Weekly/Monthly cumulative performance report generator
# Reads daily trade logs and aggregates into periodic summaries

import json
import os
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from tools.trade_logger import load_daily_report, load_recent_reports, REPORTS_DIR

logger = logging.getLogger("performance_reporter")
KST = timezone(timedelta(hours=9))


def _get_all_report_dates() -> list:
    """Scan REPORTS_DIR and return sorted list of date strings (YYYY-MM-DD)."""
    dates = []
    if not os.path.exists(REPORTS_DIR):
        return dates
    for fname in os.listdir(REPORTS_DIR):
        if fname.startswith("daily_") and fname.endswith(".json"):
            date_str = fname.replace("daily_", "").replace(".json", "")
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                dates.append(date_str)
            except ValueError:
                continue
    return sorted(dates)


def _aggregate_reports(reports: list) -> dict:
    """Aggregate multiple daily reports into a summary."""
    if not reports:
        return {"error": "No reports to aggregate"}

    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_profit_pct = 0.0
    total_realized_pnl = 0.0
    max_daily_profit = float("-inf")
    max_daily_loss = float("inf")
    risk_events_count = 0
    trading_days = 0
    daily_returns = []

    for report in reports:
        perf = report.get("performance", {})
        trades = report.get("trades", [])
        risk_events = report.get("risk_events", [])

        day_pnl = perf.get("total_realized_pnl", 0.0)
        day_return = perf.get("daily_return_pct", 0.0)

        total_trades += len(trades)
        total_realized_pnl += day_pnl
        total_profit_pct += day_return
        risk_events_count += len(risk_events)
        trading_days += 1
        daily_returns.append(day_return)

        if day_return > 0:
            winning_trades += 1
        elif day_return < 0:
            losing_trades += 1

        max_daily_profit = max(max_daily_profit, day_return)
        max_daily_loss = min(max_daily_loss, day_return)

        for trade in trades:
            pnl = trade.get("pnl_pct", 0.0)
            if pnl > 0:
                winning_trades += 1
            elif pnl < 0:
                losing_trades += 1

    # Calculate statistics
    avg_daily_return = total_profit_pct / trading_days if trading_days > 0 else 0.0
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    # Sharpe-like ratio (simplified)
    if len(daily_returns) > 1:
        import statistics
        mean_ret = statistics.mean(daily_returns)
        std_ret = statistics.stdev(daily_returns)
        sharpe_ratio = (mean_ret / std_ret) if std_ret > 0 else 0.0
    else:
        sharpe_ratio = 0.0

    # Max drawdown calculation
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for ret in daily_returns:
        cumulative += ret
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "period_days": trading_days,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": round(win_rate, 2),
        "cumulative_return_pct": round(total_profit_pct, 4),
        "total_realized_pnl": round(total_realized_pnl, 0),
        "avg_daily_return_pct": round(avg_daily_return, 4),
        "max_daily_profit_pct": round(max_daily_profit, 4) if max_daily_profit != float("-inf") else 0.0,
        "max_daily_loss_pct": round(max_daily_loss, 4) if max_daily_loss != float("inf") else 0.0,
        "max_drawdown_pct": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "risk_events_count": risk_events_count,
    }


def get_weekly_report(weeks_ago: int = 0) -> dict:
    """
    Generate weekly performance report.

    Parameters:
        weeks_ago: 0 = current week, 1 = last week, etc.

    Returns:
        dict with period info and aggregated stats
    """
    now = datetime.now(KST)
    # Monday of the target week
    current_monday = now - timedelta(days=now.weekday())
    target_monday = current_monday - timedelta(weeks=weeks_ago)
    target_sunday = target_monday + timedelta(days=6)

    start_date = target_monday.strftime("%Y-%m-%d")
    end_date = target_sunday.strftime("%Y-%m-%d")

    all_dates = _get_all_report_dates()
    week_dates = [d for d in all_dates if start_date <= d <= end_date]

    reports = []
    for date_str in week_dates:
        report = load_daily_report(date_str)
        if report:
            reports.append(report)

    summary = _aggregate_reports(reports)
    summary["period_type"] = "weekly"
    summary["period_start"] = start_date
    summary["period_end"] = end_date
    summary["report_generated"] = now.strftime("%Y-%m-%d %H:%M:%S KST")

    return summary


def get_monthly_report(months_ago: int = 0) -> dict:
    """
    Generate monthly performance report.

    Parameters:
        months_ago: 0 = current month, 1 = last month, etc.

    Returns:
        dict with period info and aggregated stats
    """
    now = datetime.now(KST)
    target_year = now.year
    target_month = now.month - months_ago

    while target_month <= 0:
        target_month += 12
        target_year -= 1

    start_date = f"{target_year}-{target_month:02d}-01"

    # End of month
    if target_month == 12:
        end_date = f"{target_year + 1}-01-01"
    else:
        end_date = f"{target_year}-{target_month + 1:02d}-01"
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)
    end_date = end_dt.strftime("%Y-%m-%d")

    all_dates = _get_all_report_dates()
    month_dates = [d for d in all_dates if start_date <= d <= end_date]

    reports = []
    for date_str in month_dates:
        report = load_daily_report(date_str)
        if report:
            reports.append(report)

    summary = _aggregate_reports(reports)
    summary["period_type"] = "monthly"
    summary["period_start"] = start_date
    summary["period_end"] = end_date
    summary["period_label"] = f"{target_year}-{target_month:02d}"
    summary["report_generated"] = now.strftime("%Y-%m-%d %H:%M:%S KST")

    return summary


def get_cumulative_summary(days: int = 30) -> dict:
    """
    Generate cumulative performance summary for the last N days.

    Parameters:
        days: Number of days to look back

    Returns:
        dict with comprehensive performance metrics
    """
    now = datetime.now(KST)
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    all_dates = _get_all_report_dates()
    target_dates = [d for d in all_dates if start_date <= d <= end_date]

    reports = []
    for date_str in target_dates:
        report = load_daily_report(date_str)
        if report:
            reports.append(report)

    summary = _aggregate_reports(reports)
    summary["period_type"] = "cumulative"
    summary["lookback_days"] = days
    summary["period_start"] = start_date
    summary["period_end"] = end_date
    summary["report_generated"] = now.strftime("%Y-%m-%d %H:%M:%S KST")

    return summary


def format_report_text(report: dict) -> str:
    """Format report dict into readable text for Telegram notification."""
    period_type = report.get("period_type", "unknown")
    period_labels = {
        "weekly": "Weekly Report",
        "monthly": "Monthly Report",
        "cumulative": "Cumulative Report",
    }

    lines = []
    lines.append(f"=== {period_labels.get(period_type, period_type.upper())} ===")
    lines.append(f"Period: {report.get('period_start', 'N/A')} ~ {report.get('period_end', 'N/A')}")
    lines.append(f"Trading Days: {report.get('period_days', 0)}")
    lines.append("")
    lines.append(f"Cumulative Return: {report.get('cumulative_return_pct', 0):.2f}%")
    lines.append(f"Total Realized PnL: {report.get('total_realized_pnl', 0):,.0f} KRW")
    lines.append(f"Avg Daily Return: {report.get('avg_daily_return_pct', 0):.4f}%")
    lines.append("")
    lines.append(f"Total Trades: {report.get('total_trades', 0)}")
    lines.append(f"Win Rate: {report.get('win_rate_pct', 0):.1f}%")
    lines.append(f"Best Day: {report.get('max_daily_profit_pct', 0):.2f}%")
    lines.append(f"Worst Day: {report.get('max_daily_loss_pct', 0):.2f}%")
    lines.append(f"Max Drawdown: {report.get('max_drawdown_pct', 0):.2f}%")
    lines.append(f"Sharpe Ratio: {report.get('sharpe_ratio', 0):.4f}")
    lines.append("")
    lines.append(f"Risk Events: {report.get('risk_events_count', 0)}")
    lines.append(f"Generated: {report.get('report_generated', 'N/A')}")

    return "\n".join(lines)


def export_periodic_report(period_type: str = "weekly", **kwargs) -> str:
    """
    Export a periodic report to JSON file.

    Parameters:
        period_type: "weekly", "monthly", or "cumulative"
        **kwargs: Additional arguments passed to the respective generator

    Returns:
        filepath of the exported report
    """
    if period_type == "weekly":
        report = get_weekly_report(**kwargs)
    elif period_type == "monthly":
        report = get_monthly_report(**kwargs)
    elif period_type == "cumulative":
        report = get_cumulative_summary(**kwargs)
    else:
        raise ValueError(f"Unknown period_type: {period_type}")

    filename = f"{period_type}_{report.get('period_start', 'unknown')}_{report.get('period_end', 'unknown')}.json"
    filepath = os.path.join(REPORTS_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info(f"Periodic report exported: {filepath}")
    return filepath
