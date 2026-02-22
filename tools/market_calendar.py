# tools/market_calendar.py — 한국 주식시장 개장일 판별
# 공휴일 + 주말을 고려하여 장이 열리는 날인지 판단한다.
# 매년 초 HOLIDAYS 딕셔너리를 업데이트하면 된다.

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# ── 한국 공휴일 (연도별) ──────────────────────────────────────
# 대체공휴일, 임시공휴일은 정부 발표 후 수동 추가
HOLIDAYS = {
    2025: [
        date(2025, 1, 1),    # 신정
        date(2025, 1, 28),   # 설날 연휴
        date(2025, 1, 29),   # 설날
        date(2025, 1, 30),   # 설날 연휴
        date(2025, 3, 1),    # 삼일절
        date(2025, 5, 5),    # 어린이날
        date(2025, 5, 6),    # 대체공휴일 (석가탄신일)
        date(2025, 6, 6),    # 현충일
        date(2025, 8, 15),   # 광복절
        date(2025, 10, 3),   # 개천절
        date(2025, 10, 5),   # 추석 연휴
        date(2025, 10, 6),   # 추석
        date(2025, 10, 7),   # 추석 연휴
        date(2025, 10, 8),   # 대체공휴일
        date(2025, 10, 9),   # 한글날
        date(2025, 12, 25),  # 크리스마스
        date(2025, 12, 31),  # 폐장일 (KRX 지정)
    ],
    2026: [
        date(2026, 1, 1),    # 신정
        date(2026, 2, 16),   # 설날 연휴
        date(2026, 2, 17),   # 설날
        date(2026, 2, 18),   # 설날 연휴
        date(2026, 3, 1),    # 삼일절
        date(2026, 3, 2),    # 대체공휴일
        date(2026, 5, 5),    # 어린이날
        date(2026, 5, 24),   # 석가탄신일
        date(2026, 5, 25),   # 대체공휴일
        date(2026, 6, 6),    # 현충일
        date(2026, 8, 15),   # 광복절
        date(2026, 8, 17),   # 대체공휴일
        date(2026, 9, 24),   # 추석 연휴
        date(2026, 9, 25),   # 추석
        date(2026, 9, 26),   # 추석 연휴
        date(2026, 10, 3),   # 개천절
        date(2026, 10, 5),   # 대체공휴일
        date(2026, 10, 9),   # 한글날
        date(2026, 12, 25),  # 크리스마스
        date(2026, 12, 31),  # 폐장일 (KRX 지정)
    ],
}


def _holiday_set(year: int) -> set:
    """해당 연도의 공휴일 set 반환."""
    return set(HOLIDAYS.get(year, []))


def is_market_open_day(d: date = None) -> bool:
    """
    주어진 날짜가 KRX 개장일인지 판별한다.
    - 주말(토·일) → False
    - 공휴일 → False
    - 나머지 → True

    Parameters
    ----------
    d : 확인할 날짜. None이면 오늘(KST).
    """
    if d is None:
        d = datetime.now(KST).date()
    # 주말 체크
    if d.weekday() >= 5:  # 5=토, 6=일
        return False
    # 공휴일 체크
    return d not in _holiday_set(d.year)


def next_market_day(d: date = None) -> date:
    """다음 개장일을 반환한다."""
    if d is None:
        d = datetime.now(KST).date()
    d = d + timedelta(days=1)
    while not is_market_open_day(d):
        d += timedelta(days=1)
    return d


def is_market_hours(now: datetime = None) -> bool:
    """
    현재 시각이 장중 시간대(09:00~15:30 KST)인지 판별한다.
    """
    if now is None:
        now = datetime.now(KST)
    if not is_market_open_day(now.date()):
        return False
    t = now.time()
    from datetime import time as dtime
    return dtime(9, 0) <= t <= dtime(15, 30)


def market_time_label(now: datetime = None) -> str:
    """
    현재 시간대 라벨을 반환한다.
    - 'PRE_MARKET'  : 개장일 00:00~08:59
    - 'MARKET_OPEN' : 09:00~15:30
    - 'POST_MARKET' : 15:31~23:59
    - 'CLOSED'      : 주말/공휴일
    """
    if now is None:
        now = datetime.now(KST)
    if not is_market_open_day(now.date()):
        return "CLOSED"
    from datetime import time as dtime
    t = now.time()
    if t < dtime(9, 0):
        return "PRE_MARKET"
    elif t <= dtime(15, 30):
        return "MARKET_OPEN"
    else:
        return "POST_MARKET"


# ── 테스트 블록 ───────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW — 시장 캘린더 테스트")
    print("=" * 55)

    today = datetime.now(KST).date()
    print(f"\n  오늘: {today} ({['월','화','수','목','금','토','일'][today.weekday()]})")
    print(f"  개장일: {'Y' if is_market_open_day(today) else 'N'}")
    print(f"  장중: {'Y' if is_market_hours() else 'N'}")
    print(f"  시간대: {market_time_label()}")
    print(f"  다음 개장일: {next_market_day(today)}")

    # 2026년 공휴일 테스트
    print(f"\n  2026년 공휴일 수: {len(HOLIDAYS.get(2026, []))}일")
    holidays_2026 = sorted(HOLIDAYS.get(2026, []))
    for h in holidays_2026:
        wd = ['월','화','수','목','금','토','일'][h.weekday()]
        print(f"    {h} ({wd})")

    # 특정 날짜 테스트
    tests = [
        (date(2026, 2, 17), False, "설날"),
        (date(2026, 2, 22), False, "일요일"),
        (date(2026, 2, 23), True,  "월요일(평일)"),
        (date(2026, 3, 2),  False, "대체공휴일"),
        (date(2026, 5, 5),  False, "어린이날"),
    ]
    print("\n  개장일 판별 테스트:")
    for d, expected, desc in tests:
        result = is_market_open_day(d)
        ok = "OK" if result == expected else "FAIL"
        print(f"    {d} ({desc}): {result} [{ok}]")

    print("\n" + "=" * 55)
    print("  테스트 완료!")
    print("=" * 55)
