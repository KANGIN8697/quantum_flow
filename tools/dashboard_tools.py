# tools/dashboard_tools.py — 일별/주별 대시보드 이미지 생성
# PIL(Pillow)로 깔끔한 텍스트 기반 대시보드 이미지를 생성하고
# 텔레그램으로 전송한다.

import os
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

KST = timezone(timedelta(hours=9))

# ── 경로 설정 ─────────────────────────────────────────────
DASHBOARD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "dashboards",
)
os.makedirs(DASHBOARD_DIR, exist_ok=True)

# ── 폰트 설정 ─────────────────────────────────────────────
_FONT_PATHS = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/unifont/unifont.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_FONT_CACHE = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """한글 지원 폰트를 캐시하여 반환."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for path in _FONT_PATHS:
        try:
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[size] = font
            return font
        except Exception:
            continue
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ── 색상 팔레트 ───────────────────────────────────────────
BG_COLOR = (18, 18, 24)           # 어두운 배경
HEADER_BG = (30, 36, 50)          # 헤더 배경
ROW_EVEN = (24, 28, 38)           # 짝수 행
ROW_ODD = (30, 34, 44)            # 홀수 행
ACCENT = (80, 140, 255)           # 강조색 (파란색)
GREEN = (50, 205, 100)            # 수익
RED = (240, 70, 70)               # 손실
YELLOW = (255, 200, 50)           # 경고/중립
WHITE = (230, 230, 240)           # 일반 텍스트
GRAY = (140, 145, 160)            # 보조 텍스트
DIVIDER = (50, 55, 70)            # 구분선


def _pnl_color(value):
    """손익 값에 따른 색상 반환."""
    if value > 0:
        return GREEN
    elif value < 0:
        return RED
    return GRAY


# ── 일별 대시보드 생성 ────────────────────────────────────

def generate_daily_dashboard(performance: dict, trades: list,
                             positions: dict = None,
                             macro_summary: str = "") -> str:
    """
    일별 대시보드 이미지를 생성한다.

    Parameters
    ----------
    performance : trade_logger.calculate_performance()의 결과
    trades      : 당일 매매 기록 리스트
    positions   : 장 마감 시 잔존 포지션 {code: {...}}
    macro_summary : 매크로 요약 문자열

    Returns
    -------
    str : 저장된 이미지 파일 경로
    """
    width = 800
    row_h = 36
    padding = 20
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    # ── 내용 준비 ──
    total_trades = performance.get("total_trades", 0)
    buy_count = performance.get("buy_count", 0)
    sell_count = performance.get("sell_count", 0)
    pyramid_count = performance.get("pyramid_count", 0)
    win_count = performance.get("win_count", 0)
    loss_count = performance.get("loss_count", 0)
    win_rate = performance.get("win_rate", 0)
    realized_pnl = performance.get("realized_pnl", 0)
    daily_loss = performance.get("daily_loss", 0)
    best_trade = performance.get("best_trade", 0)
    worst_trade = performance.get("worst_trade", 0)
    remaining = performance.get("remaining_positions", 0)
    risk_off = performance.get("risk_off_triggered", False)

    # 거래 내역 (최대 10건)
    trade_rows = []
    for t in trades[:10]:
        action = t.get("action", "?")
        code = t.get("code", "?")
        grade = t.get("eval_grade", "")
        pnl = t.get("profit_pct", None)
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "-"
        time_str = t.get("timestamp", "")[-8:]  # HH:MM:SS
        trade_rows.append((time_str, action, code, grade, pnl_str, pnl))

    # 높이 계산
    sections = [
        80,                                     # 타이틀
        40 + row_h * 4,                         # 성과 요약 (4행)
        40 + row_h * max(len(trade_rows), 1),   # 거래 내역
    ]
    if positions:
        sections.append(40 + row_h * min(len(positions), 5))  # 보유 포지션
    sections.append(40)  # 푸터

    height = sum(sections) + padding * 2

    # ── 이미지 생성 ──
    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_title = _get_font(24)
    font_section = _get_font(18)
    font_body = _get_font(15)
    font_small = _get_font(13)

    y = padding

    # ── 타이틀 ──
    draw.rectangle([(0, 0), (width, y + 70)], fill=HEADER_BG)
    draw.text((padding, y), "QUANTUM FLOW", fill=ACCENT, font=font_title)
    draw.text((padding, y + 30), f"일별 매매 리포트  |  {today_str}", fill=GRAY, font=font_section)
    if risk_off:
        draw.text((width - 180, y + 10), "RISK-OFF", fill=RED, font=font_title)
    y += 80

    # ── 성과 요약 ──
    draw.text((padding, y), "성과 요약", fill=ACCENT, font=font_section)
    y += 32

    summary_items = [
        [("총 거래", f"{total_trades}건", WHITE),
         ("매수", f"{buy_count}건", GREEN),
         ("매도", f"{sell_count}건", RED),
         ("추가매수", f"{pyramid_count}건", YELLOW)],
        [("승률", f"{win_rate:.0%}", _pnl_color(win_rate - 0.5)),
         ("익절", f"{win_count}건", GREEN),
         ("손절", f"{loss_count}건", RED),
         ("보유중", f"{remaining}종목", WHITE)],
        [("실현 손익", f"{realized_pnl:+.2f}%", _pnl_color(realized_pnl)),
         ("일일 손실", f"{daily_loss:+.2f}%", _pnl_color(-abs(daily_loss))),
         ("최고 수익", f"{best_trade:+.2f}%", GREEN if best_trade > 0 else GRAY),
         ("최대 손실", f"{worst_trade:+.2f}%", RED if worst_trade < 0 else GRAY)],
    ]

    for row_idx, row_items in enumerate(summary_items):
        bg = ROW_EVEN if row_idx % 2 == 0 else ROW_ODD
        draw.rectangle([(padding, y), (width - padding, y + row_h)], fill=bg)
        col_w = (width - padding * 2) // len(row_items)
        for col_idx, (label, value, color) in enumerate(row_items):
            x = padding + col_w * col_idx + 8
            draw.text((x, y + 2), label, fill=GRAY, font=font_small)
            draw.text((x, y + 17), value, fill=color, font=font_body)
        y += row_h

    y += row_h  # 간격

    # ── 거래 내역 ──
    draw.text((padding, y), "거래 내역", fill=ACCENT, font=font_section)
    y += 32

    if not trade_rows:
        draw.text((padding + 8, y + 8), "당일 거래 없음", fill=GRAY, font=font_body)
        y += row_h
    else:
        # 헤더
        draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=HEADER_BG)
        headers = ["시각", "유형", "종목코드", "등급", "수익률"]
        cols = [padding + 8, padding + 100, padding + 200, padding + 360, padding + 460]
        for hdr, cx in zip(headers, cols):
            draw.text((cx, y + 8), hdr, fill=GRAY, font=font_small)
        y += row_h

        for idx, (time_str, action, code, grade, pnl_str, pnl_val) in enumerate(trade_rows):
            bg = ROW_EVEN if idx % 2 == 0 else ROW_ODD
            draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=bg)

            # 유형별 색상
            act_color = GREEN if action == "BUY" else RED if action in ("SELL", "STOP_LOSS", "FORCE_CLOSE") else YELLOW
            pnl_color = _pnl_color(pnl_val) if pnl_val is not None else GRAY

            draw.text((cols[0], y + 8), time_str, fill=WHITE, font=font_body)
            draw.text((cols[1], y + 8), action, fill=act_color, font=font_body)
            draw.text((cols[2], y + 8), code, fill=WHITE, font=font_body)
            draw.text((cols[3], y + 8), grade or "-", fill=YELLOW, font=font_body)
            draw.text((cols[4], y + 8), pnl_str, fill=pnl_color, font=font_body)
            y += row_h

    # ── 보유 포지션 ──
    if positions:
        y += 8
        draw.text((padding, y), "잔존 포지션", fill=ACCENT, font=font_section)
        y += 32

        draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=HEADER_BG)
        pos_headers = ["종목코드", "등급", "섹터", "진입비중", "피라미딩"]
        pos_cols = [padding + 8, padding + 140, padding + 240, padding + 400, padding + 540]
        for hdr, cx in zip(pos_headers, pos_cols):
            draw.text((cx, y + 8), hdr, fill=GRAY, font=font_small)
        y += row_h

        for idx, (code, data) in enumerate(list(positions.items())[:5]):
            bg = ROW_EVEN if idx % 2 == 0 else ROW_ODD
            draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=bg)
            draw.text((pos_cols[0], y + 8), code, fill=WHITE, font=font_body)
            draw.text((pos_cols[1], y + 8), data.get("eval_grade", "?"), fill=YELLOW, font=font_body)
            draw.text((pos_cols[2], y + 8), data.get("sector", "-"), fill=GRAY, font=font_body)
            draw.text((pos_cols[3], y + 8), f"{data.get('entry_pct', 0):.1%}", fill=WHITE, font=font_body)
            pyr = data.get("pyramid_count", 0)
            draw.text((pos_cols[4], y + 8), f"{pyr}회", fill=YELLOW if pyr > 0 else GRAY, font=font_body)
            y += row_h

    # ── 푸터 ──
    y += 12
    draw.line([(padding, y), (width - padding, y)], fill=DIVIDER, width=1)
    y += 8
    mode = os.getenv("USE_PAPER", "true")
    mode_label = "모의투자" if mode.lower() == "true" else "실전투자"
    footer = f"QUANTUM FLOW v2.1  |  {mode_label}  |  {datetime.now(KST).strftime('%H:%M:%S')} KST"
    draw.text((padding, y), footer, fill=GRAY, font=font_small)

    # ── 저장 ──
    filepath = os.path.join(DASHBOARD_DIR, f"daily_{today_str}.png")
    img.save(filepath, "PNG")
    print(f"  📊 일별 대시보드 저장: {filepath}")
    return filepath


# ── 주별 대시보드 생성 ────────────────────────────────────

def generate_weekly_dashboard(weekly_stats: dict, daily_summaries: list) -> str:
    """
    주별 대시보드 이미지를 생성한다.

    Parameters
    ----------
    weekly_stats   : get_cumulative_stats()의 결과 (최근 5일)
    daily_summaries: 일별 성과 리스트 [{date, total_trades, win_rate, realized_pnl, ...}]

    Returns
    -------
    str : 저장된 이미지 파일 경로
    """
    width = 800
    row_h = 36
    padding = 20
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    days_count = len(daily_summaries)

    # 높이 계산
    height = (
        80                                      # 타이틀
        + 40 + row_h * 3                        # 주간 요약
        + 40 + row_h * (max(days_count, 1) + 1) # 일별 내역
        + 40 + row_h * 2                        # 등급/스킵 분석
        + 60                                    # 푸터
        + padding * 2
    )

    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_title = _get_font(24)
    font_section = _get_font(18)
    font_body = _get_font(15)
    font_small = _get_font(13)

    y = padding

    # ── 타이틀 ──
    draw.rectangle([(0, 0), (width, y + 70)], fill=HEADER_BG)
    draw.text((padding, y), "QUANTUM FLOW", fill=ACCENT, font=font_title)
    draw.text((padding, y + 30), f"주간 성과 리포트  |  {today_str}  ({days_count}일)", fill=GRAY, font=font_section)
    y += 80

    # ── 주간 요약 ──
    draw.text((padding, y), "주간 누적 성과", fill=ACCENT, font=font_section)
    y += 32

    cum_pnl = weekly_stats.get("cumulative_pnl", 0)
    total_trades = weekly_stats.get("total_trades", 0)
    win_rate = weekly_stats.get("win_rate", 0)
    profit_factor = weekly_stats.get("profit_factor", 0)
    avg_win = weekly_stats.get("avg_win", 0)
    avg_loss = weekly_stats.get("avg_loss", 0)
    risk_off_days = weekly_stats.get("risk_off_days", 0)
    trading_days = weekly_stats.get("trading_days", 0)

    summary_rows = [
        [("누적 손익", f"{cum_pnl:+.2%}", _pnl_color(cum_pnl)),
         ("총 거래", f"{total_trades}건", WHITE),
         ("거래일수", f"{trading_days}일", WHITE),
         ("Risk-Off", f"{risk_off_days}일", RED if risk_off_days > 0 else GREEN)],
        [("승률", f"{win_rate:.0%}", _pnl_color(win_rate - 0.5)),
         ("Profit Factor", f"{profit_factor:.2f}", _pnl_color(profit_factor - 1)),
         ("평균 익절", f"{avg_win:+.2%}", GREEN if avg_win > 0 else GRAY),
         ("평균 손절", f"{avg_loss:+.2%}", RED if avg_loss < 0 else GRAY)],
    ]

    for row_idx, row_items in enumerate(summary_rows):
        bg = ROW_EVEN if row_idx % 2 == 0 else ROW_ODD
        draw.rectangle([(padding, y), (width - padding, y + row_h)], fill=bg)
        col_w = (width - padding * 2) // len(row_items)
        for col_idx, (label, value, color) in enumerate(row_items):
            x = padding + col_w * col_idx + 8
            draw.text((x, y + 2), label, fill=GRAY, font=font_small)
            draw.text((x, y + 17), value, fill=color, font=font_body)
        y += row_h

    y += row_h

    # ── 일별 내역 ──
    draw.text((padding, y), "일별 상세", fill=ACCENT, font=font_section)
    y += 32

    # 헤더
    draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=HEADER_BG)
    day_headers = ["날짜", "거래수", "승률", "실현손익", "매수", "매도", "Risk-Off"]
    day_cols = [padding + 8, padding + 120, padding + 210, padding + 310,
                padding + 430, padding + 520, padding + 620]
    for hdr, cx in zip(day_headers, day_cols):
        draw.text((cx, y + 8), hdr, fill=GRAY, font=font_small)
    y += row_h

    if not daily_summaries:
        draw.text((padding + 8, y + 8), "데이터 없음", fill=GRAY, font=font_body)
        y += row_h
    else:
        for idx, ds in enumerate(daily_summaries):
            bg = ROW_EVEN if idx % 2 == 0 else ROW_ODD
            draw.rectangle([(padding, y), (width - padding, y + row_h - 2)], fill=bg)

            d_date = ds.get("date", "?")
            d_trades = ds.get("total_trades", 0)
            d_wr = ds.get("win_rate", 0)
            d_pnl = ds.get("realized_pnl", 0)
            d_buy = ds.get("buy_count", 0)
            d_sell = ds.get("sell_count", 0)
            d_risk = "Y" if ds.get("risk_off_triggered") else "-"

            draw.text((day_cols[0], y + 8), str(d_date), fill=WHITE, font=font_body)
            draw.text((day_cols[1], y + 8), f"{d_trades}건", fill=WHITE, font=font_body)
            draw.text((day_cols[2], y + 8), f"{d_wr:.0%}", fill=_pnl_color(d_wr - 0.5), font=font_body)
            draw.text((day_cols[3], y + 8), f"{d_pnl:+.2%}", fill=_pnl_color(d_pnl), font=font_body)
            draw.text((day_cols[4], y + 8), f"{d_buy}", fill=GREEN, font=font_body)
            draw.text((day_cols[5], y + 8), f"{d_sell}", fill=RED, font=font_body)
            draw.text((day_cols[6], y + 8), d_risk, fill=RED if d_risk == "Y" else GRAY, font=font_body)
            y += row_h

    y += 8

    # ── 등급 분포 & 스킵 사유 ──
    draw.text((padding, y), "분석", fill=ACCENT, font=font_section)
    y += 32

    # 등급 분포
    grade_dist = weekly_stats.get("grade_distribution", {})
    grade_str = "  ".join(f"{g}:{c}" for g, c in sorted(grade_dist.items())) or "데이터 없음"
    bg = ROW_EVEN
    draw.rectangle([(padding, y), (width - padding, y + row_h)], fill=bg)
    draw.text((padding + 8, y + 2), "등급 분포", fill=GRAY, font=font_small)
    draw.text((padding + 8, y + 17), grade_str, fill=YELLOW, font=font_body)
    y += row_h

    # 스킵 사유
    skip_reasons = weekly_stats.get("skip_reasons", {})
    skip_str = "  ".join(f"{r}:{c}" for r, c in sorted(skip_reasons.items())) or "없음"
    bg = ROW_ODD
    draw.rectangle([(padding, y), (width - padding, y + row_h)], fill=bg)
    draw.text((padding + 8, y + 2), "매수 스킵 사유", fill=GRAY, font=font_small)
    draw.text((padding + 8, y + 17), skip_str, fill=GRAY, font=font_body)
    y += row_h

    # ── 푸터 ──
    y += 12
    draw.line([(padding, y), (width - padding, y)], fill=DIVIDER, width=1)
    y += 8
    mode = os.getenv("USE_PAPER", "true")
    mode_label = "모의투자" if mode.lower() == "true" else "실전투자"
    footer = f"QUANTUM FLOW v2.1  |  {mode_label}  |  {datetime.now(KST).strftime('%H:%M:%S')} KST"
    draw.text((padding, y), footer, fill=GRAY, font=font_small)

    # ── 저장 ──
    filepath = os.path.join(DASHBOARD_DIR, f"weekly_{today_str}.png")
    img.save(filepath, "PNG")
    print(f"  📊 주별 대시보드 저장: {filepath}")
    return filepath


# ── 통합 호출 함수 ────────────────────────────────────────

def create_and_send_daily_dashboard(performance: dict, trades: list,
                                     positions: dict = None):
    """일별 대시보드를 생성하고 텔레그램으로 전송한다."""
    try:
        filepath = generate_daily_dashboard(performance, trades, positions)
        from tools.notifier_tools import notify_dashboard
        notify_dashboard(filepath, "일별")
        return filepath
    except Exception as e:
        print(f"  ❌ 일별 대시보드 생성/전송 실패: {e}")
        return None


def create_and_send_weekly_dashboard():
    """주별 대시보드를 생성하고 텔레그램으로 전송한다."""
    try:
        from tools.trade_logger import get_cumulative_stats, load_recent_reports
        weekly_stats = get_cumulative_stats(days=5)
        reports = load_recent_reports(days=5)

        daily_summaries = []
        for r in reports:
            s = r.get("summary", {})
            if s:
                daily_summaries.append(s)

        filepath = generate_weekly_dashboard(weekly_stats, daily_summaries)
        from tools.notifier_tools import notify_dashboard
        notify_dashboard(filepath, "주별")
        return filepath
    except Exception as e:
        print(f"  ❌ 주별 대시보드 생성/전송 실패: {e}")
        return None


# ── 테스트 블록 ───────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW — 대시보드 이미지 테스트")
    print("=" * 55)

    # 테스트 데이터
    test_perf = {
        "date": "2026-02-22",
        "total_trades": 7,
        "buy_count": 4,
        "sell_count": 2,
        "pyramid_count": 1,
        "win_count": 2,
        "loss_count": 0,
        "win_rate": 1.0,
        "realized_pnl": 3.45,
        "daily_loss": 0.0,
        "best_trade": 5.2,
        "worst_trade": 1.7,
        "remaining_positions": 3,
        "risk_off_triggered": False,
        "grade_distribution": {"A": 2, "B": 3, "C": 2},
        "skip_reasons": {"max_positions": 3, "blacklisted": 1},
    }

    test_trades = [
        {"timestamp": "2026-02-22T09:15:30", "action": "BUY", "code": "005930",
         "eval_grade": "A", "profit_pct": None},
        {"timestamp": "2026-02-22T09:20:00", "action": "BUY", "code": "000660",
         "eval_grade": "A+", "profit_pct": None},
        {"timestamp": "2026-02-22T10:30:15", "action": "PYRAMID", "code": "005930",
         "eval_grade": "A", "profit_pct": None},
        {"timestamp": "2026-02-22T11:45:00", "action": "BUY", "code": "035420",
         "eval_grade": "B", "profit_pct": None},
        {"timestamp": "2026-02-22T13:20:00", "action": "SELL", "code": "005930",
         "eval_grade": "A", "profit_pct": 5.2},
        {"timestamp": "2026-02-22T14:10:00", "action": "SELL", "code": "000660",
         "eval_grade": "A+", "profit_pct": 1.7},
        {"timestamp": "2026-02-22T14:30:00", "action": "BUY", "code": "051910",
         "eval_grade": "B", "profit_pct": None},
    ]

    test_positions = {
        "035420": {"eval_grade": "B", "sector": "IT", "entry_pct": 0.12, "pyramid_count": 0},
        "051910": {"eval_grade": "B", "sector": "화학", "entry_pct": 0.10, "pyramid_count": 0},
    }

    # 일별 대시보드
    print("\n[1] 일별 대시보드 생성...")
    path1 = generate_daily_dashboard(test_perf, test_trades, test_positions)
    print(f"  저장: {path1}")

    # 주별 대시보드 (테스트 데이터)
    print("\n[2] 주별 대시보드 생성...")
    test_weekly = {
        "trading_days": 5,
        "total_trades": 28,
        "total_buys": 15,
        "total_sells": 10,
        "cumulative_pnl": 0.0542,
        "win_count": 7,
        "loss_count": 3,
        "win_rate": 0.7,
        "avg_win": 0.045,
        "avg_loss": -0.025,
        "profit_factor": 2.1,
        "risk_off_days": 1,
        "grade_distribution": {"A+": 3, "A": 8, "B": 12, "C": 5},
        "skip_reasons": {"max_positions": 12, "position_too_small": 5, "blacklisted": 2},
    }

    test_daily_summaries = [
        {"date": "2026-02-18", "total_trades": 6, "win_rate": 0.67, "realized_pnl": 0.021,
         "buy_count": 3, "sell_count": 2, "risk_off_triggered": False},
        {"date": "2026-02-19", "total_trades": 5, "win_rate": 0.50, "realized_pnl": -0.008,
         "buy_count": 3, "sell_count": 2, "risk_off_triggered": False},
        {"date": "2026-02-20", "total_trades": 8, "win_rate": 0.75, "realized_pnl": 0.032,
         "buy_count": 4, "sell_count": 3, "risk_off_triggered": False},
        {"date": "2026-02-21", "total_trades": 2, "win_rate": 0.0, "realized_pnl": -0.015,
         "buy_count": 1, "sell_count": 1, "risk_off_triggered": True},
        {"date": "2026-02-22", "total_trades": 7, "win_rate": 1.0, "realized_pnl": 0.035,
         "buy_count": 4, "sell_count": 2, "risk_off_triggered": False},
    ]

    path2 = generate_weekly_dashboard(test_weekly, test_daily_summaries)
    print(f"  저장: {path2}")

    print("\n" + "=" * 55)
    print("  ✅ 대시보드 테스트 완료!")
    print("=" * 55)
