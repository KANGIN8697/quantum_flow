# data_collector/macro/fred_release_scheduler.py
# FRED 발표 일정 기반 이벤트 드리븐 데이터 수집
#
# 동작 방식:
#   1. 매일 06:00 — FRED releases/dates API로 오늘 발표 예정 지표 목록 조회
#   2. 발표 예정 항목이 있으면 → 해당 시간 + 10분 후 APScheduler에 동적 Job 등록
#   3. 발표 없는 날은 수집 안 함 → 불필요한 API 호출 제로
#   4. 미국 시간 기준 발표 → KST 변환 후 야간 수집, 다음날 08:30 Agent 1이 최신값 활용

import os
import logging
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("fred_release_scheduler")

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
KST = ZoneInfo("Asia/Seoul")
ET  = ZoneInfo("America/New_York")

# ── 추적할 FRED Release ID ──────────────────────────────────────
# FRED의 release_id → (친숙한 이름, 관련 series_id 목록)
TRACKED_RELEASES: Dict[int, Dict] = {
    10:  {"name": "Employment Situation (NFP)",     "series": ["UNRATE", "PAYEMS", "ICSA"]},
    46:  {"name": "CPI",                            "series": ["CPIAUCSL", "CPILFESL"]},
    49:  {"name": "PPI",                            "series": ["PPIACO", "PPIFIS"]},
    53:  {"name": "GDP",                            "series": ["GDP", "GDPC1"]},
    82:  {"name": "PCE / Personal Income",          "series": ["PCEPI", "PCE", "PSAVERT"]},
    103: {"name": "ISM Manufacturing PMI",          "series": ["MANEMP"]},
    168: {"name": "Federal Reserve Interest Rate",  "series": ["FEDFUNDS", "DFF"]},
    21:  {"name": "Consumer Confidence",            "series": ["UMCSENT"]},
    144: {"name": "Retail Sales",                   "series": ["RSAFS"]},
    175: {"name": "Industrial Production",          "series": ["INDPRO"]},
}


# ══════════════════════════════════════════════════════════════
# 1. FRED Release Calendar 조회
# ══════════════════════════════════════════════════════════════

async def fetch_today_releases(target_date: Optional[str] = None) -> List[Dict]:
    """
    오늘(또는 지정 날짜) 발표 예정인 FRED 지표 목록 조회.

    Parameters
    ----------
    target_date : "YYYY-MM-DD" 형식. None이면 오늘.

    Returns
    -------
    [{"release_id": 46, "name": "CPI", "release_time_kst": "2026-02-28 22:30", ...}, ...]
    """
    if not FRED_API_KEY:
        logger.warning("FRED_API_KEY 미설정 — 발표 일정 조회 불가")
        return []

    if target_date is None:
        target_date = date.today().isoformat()

    url = (
        f"https://api.stlouisfed.org/fred/releases/dates"
        f"?api_key={FRED_API_KEY}"
        f"&file_type=json"
        f"&realtime_start={target_date}"
        f"&realtime_end={target_date}"
        f"&include_release_dates_with_no_data=false"
        f"&limit=100"
    )

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()

        release_dates = data.get("release_dates", [])
        logger.info(f"FRED 발표 일정: {target_date} → {len(release_dates)}건")

        results = []
        for rd in release_dates:
            rid = rd.get("release_id")
            if rid not in TRACKED_RELEASES:
                continue

            release_info = TRACKED_RELEASES[rid]
            release_name = rd.get("release_name", release_info["name"])

            # 발표 시각 파싱 (FRED는 시각을 별도 제공하지 않으므로 별도 조회)
            release_time_et = await _fetch_release_time(rid, target_date)

            # ET → KST 변환
            if release_time_et:
                kst_dt = release_time_et.astimezone(KST)
                release_time_kst = kst_dt.strftime("%Y-%m-%d %H:%M")
                collect_time_kst = (kst_dt + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
            else:
                # 시각 불명: 해당 날짜 23:30 KST로 기본값
                release_time_kst = f"{target_date} 23:30"
                collect_time_kst = f"{target_date} 23:40"

            results.append({
                "release_id":       rid,
                "name":             release_name,
                "release_date":     target_date,
                "release_time_kst": release_time_kst,
                "collect_time_kst": collect_time_kst,
                "series":           release_info["series"],
            })

        logger.info(f"추적 대상 발표: {[r['name'] for r in results]}")
        return results

    except Exception as e:
        logger.error(f"FRED 발표 일정 조회 실패: {e}", exc_info=True)
        return []


async def _fetch_release_time(release_id: int, release_date: str) -> Optional[datetime]:
    """
    특정 Release의 정확한 발표 시각 조회 (ET 기준).
    FRED API: /fred/release → release_time 필드 활용.
    """
    if not FRED_API_KEY:
        return None

    url = (
        f"https://api.stlouisfed.org/fred/release"
        f"?release_id={release_id}"
        f"&api_key={FRED_API_KEY}"
        f"&file_type=json"
    )

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

        releases = data.get("releases", [])
        if not releases:
            return None

        # release_time은 보통 "08:30:00" 형식 (ET 기준 추정)
        rt = releases[0].get("release_time", "")
        if not rt or rt == "NA":
            return None

        # 날짜 + 시각 결합
        dt_naive = datetime.strptime(f"{release_date} {rt}", "%Y-%m-%d %H:%M:%S")
        return dt_naive.replace(tzinfo=ET)

    except Exception as e:
        logger.debug(f"Release time 조회 실패 ({release_id}): {e}")
        return None


# ══════════════════════════════════════════════════════════════
# 2. 발표 직후 데이터 수집
# ══════════════════════════════════════════════════════════════

async def collect_release_data(release_info: Dict) -> Dict:
    """
    특정 발표 이후 해당 시리즈의 최신값 수집.

    Parameters
    ----------
    release_info : fetch_today_releases()가 반환한 항목 하나

    Returns
    -------
    {"name": "CPI", "series_data": {"CPIAUCSL": {...}, ...}, "collected_at": "..."}
    """
    from data_collector.macro.fred_collector import fetch_fred_series

    name    = release_info["name"]
    series  = release_info["series"]
    results = {}

    logger.info(f"📊 FRED 발표 후 수집 시작: {name}")

    for sid in series:
        try:
            data = fetch_fred_series(sid, days_back=5)
            if "error" not in data:
                results[sid] = data
                logger.info(f"  {sid}: {data.get('value')} ({data.get('date')})")
        except Exception as e:
            logger.error(f"  {sid} 수집 실패: {e}")

    collected = {
        "name":         name,
        "release_id":   release_info["release_id"],
        "release_date": release_info["release_date"],
        "series_data":  results,
        "collected_at": datetime.now(KST).isoformat(),
    }

    # shared_state에 저장 (다음날 Agent 1이 참조)
    _save_to_state(collected)

    # 텔레그램 알림
    _notify_release(collected)

    return collected


def _save_to_state(collected: Dict):
    """수집 결과를 shared_state + 파일에 저장"""
    try:
        from shared_state import set_state
        # 기존 발표 데이터에 추가
        existing = {}
        try:
            existing = set_state.__globals__.get("_state", {}).get("fred_releases", {})
        except Exception:
            pass

        key = f"{collected['release_date']}_{collected['release_id']}"
        releases = dict(existing)
        releases[key] = collected

        set_state("fred_releases", releases)
        logger.info(f"  shared_state 저장 완료: fred_releases[{key}]")

    except Exception as e:
        logger.debug(f"shared_state 저장 실패: {e}")

    # 파일 백업
    try:
        import json
        import os
        out_dir = "outputs/fred_releases"
        os.makedirs(out_dir, exist_ok=True)
        fname = f"{out_dir}/{collected['release_date']}_{collected['name'].replace(' ', '_')}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(collected, f, ensure_ascii=False, indent=2)
        logger.info(f"  파일 저장: {fname}")
    except Exception as e:
        logger.debug(f"파일 저장 실패: {e}")


def _notify_release(collected: Dict):
    """FRED 발표 텔레그램 알림"""
    try:
        from tools.notifier_tools import _send
        name = collected["name"]
        series_data = collected["series_data"]

        lines = [f"📊 <b>FRED 발표 수집 완료</b>: {name}"]
        for sid, d in list(series_data.items())[:4]:
            val = d.get("value", "?")
            dt  = d.get("date", "?")
            lines.append(f"  • {sid}: <b>{val}</b> ({dt})")
        lines.append(f"  수집: {collected['collected_at'][:16]}")

        _send("\n".join(lines))
    except Exception as e:
        logger.debug(f"텔레그램 알림 실패: {e}")


# ══════════════════════════════════════════════════════════════
# 3. APScheduler 동적 Job 등록 (main.py에서 호출)
# ══════════════════════════════════════════════════════════════

async def setup_daily_fred_jobs(scheduler) -> int:
    """
    오늘 FRED 발표 일정을 조회하고 APScheduler에 동적 Job을 등록한다.
    main.py의 06:00 job에서 호출.

    Parameters
    ----------
    scheduler : APScheduler AsyncIOScheduler 인스턴스

    Returns
    -------
    int : 등록된 Job 수
    """
    from apscheduler.triggers.date import DateTrigger

    # 기존 fred_release_* Job 전부 제거 (재등록 방지)
    for job in scheduler.get_jobs():
        if job.id.startswith("fred_release_"):
            scheduler.remove_job(job.id)

    releases = await fetch_today_releases()

    if not releases:
        logger.info("오늘 FRED 발표 없음 — Job 등록 스킵")
        return 0

    registered = 0
    for rel in releases:
        collect_time_str = rel["collect_time_kst"]  # "YYYY-MM-DD HH:MM"
        try:
            collect_dt = datetime.strptime(collect_time_str, "%Y-%m-%d %H:%M")
            collect_dt = collect_dt.replace(tzinfo=KST)

            # 이미 지난 시간이면 스킵
            if collect_dt <= datetime.now(KST):
                logger.info(f"  {rel['name']}: 이미 지난 시각 ({collect_time_str}) — 스킵")
                continue

            job_id = f"fred_release_{rel['release_id']}_{rel['release_date']}"

            # 클로저로 release_info 캡처
            def _make_job(r):
                async def _job():
                    await collect_release_data(r)
                return _job

            scheduler.add_job(
                _make_job(rel),
                trigger=DateTrigger(run_date=collect_dt, timezone=KST),
                id=job_id,
                name=f"FRED {rel['name']}",
            )

            logger.info(f"  Job 등록: {rel['name']} → {collect_time_str} KST")
            registered += 1

        except Exception as e:
            logger.error(f"  Job 등록 실패 ({rel['name']}): {e}")

    logger.info(f"FRED 동적 Job {registered}개 등록 완료")
    return registered


# ══════════════════════════════════════════════════════════════
# 4. 다음 N일 발표 일정 미리보기 (유틸리티)
# ══════════════════════════════════════════════════════════════

async def fetch_upcoming_releases(days_ahead: int = 7) -> List[Dict]:
    """
    향후 N일간 주요 FRED 발표 일정 조회.
    Telegram 알림 또는 대시보드 표시용.
    """
    if not FRED_API_KEY:
        return []

    start = date.today().isoformat()
    end   = (date.today() + timedelta(days=days_ahead)).isoformat()

    url = (
        f"https://api.stlouisfed.org/fred/releases/dates"
        f"?api_key={FRED_API_KEY}"
        f"&file_type=json"
        f"&realtime_start={start}"
        f"&realtime_end={end}"
        f"&include_release_dates_with_no_data=false"
        f"&limit=200"
    )

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()

        upcoming = []
        for rd in data.get("release_dates", []):
            rid = rd.get("release_id")
            if rid in TRACKED_RELEASES:
                upcoming.append({
                    "date":  rd.get("date"),
                    "name":  rd.get("release_name", TRACKED_RELEASES[rid]["name"]),
                    "id":    rid,
                })

        upcoming.sort(key=lambda x: x["date"])
        return upcoming

    except Exception as e:
        logger.error(f"향후 발표 일정 조회 실패: {e}")
        return []


async def send_weekly_schedule_preview():
    """
    매주 월요일 09:00 — 이번 주 FRED 발표 일정 텔레그램 알림.
    main.py에 스케줄 추가 권장.
    """
    upcoming = await fetch_upcoming_releases(days_ahead=7)
    if not upcoming:
        return

    try:
        from tools.notifier_tools import _send
        lines = ["📅 <b>이번 주 주요 FRED 발표 일정</b>"]
        for u in upcoming:
            lines.append(f"  • {u['date']} — {u['name']}")
        _send("\n".join(lines))
    except Exception as e:
        logger.debug(f"주간 일정 알림 실패: {e}")
