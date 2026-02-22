# data_collector/text/news_collector.py — RSS 뉴스 헤드라인 수집
#
# 수집 소스: 한국경제, 매일경제, 조선비즈, 연합인포맥스
# 매일 06:00 자동 실행
# feedparser 라이브러리 사용 (requirements.txt에 이미 포함)

import os
import sys
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("news_collector")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import init_db, insert_rows_ignore, log_collection

try:
    import feedparser
except ImportError:
    feedparser = None
    logger.warning("feedparser 미설치 — pip install feedparser")


# ── RSS 소스 정의 ─────────────────────────────────────────

RSS_SOURCES = [
    {
        "name": "한국경제",
        "url": "https://www.hankyung.com/feed/all-news",
        "category": "종합경제",
    },
    {
        "name": "매일경제",
        "url": "https://www.mk.co.kr/rss/30000001/",
        "category": "종합경제",
    },
    {
        "name": "조선비즈",
        "url": "https://biz.chosun.com/arc/outboundfeeds/rss/",
        "category": "비즈니스",
    },
    {
        "name": "연합인포맥스",
        "url": "https://news.einfomax.co.kr/rss/allArticle.xml",
        "category": "금융시장",
    },
]


# ── 개별 RSS 피드 수집 ────────────────────────────────────

def _fetch_rss_feed(source: dict) -> list:
    """
    단일 RSS 피드에서 뉴스 헤드라인 수집.

    Returns
    -------
    list of dict: [{date, source, title, summary, category, url}, ...]
    """
    if feedparser is None:
        return []

    name = source["name"]
    url = source["url"]
    category = source["category"]

    try:
        feed = feedparser.parse(url)

        if feed.bozo and not feed.entries:
            logger.warning(f"  {name}: RSS 파싱 실패 — {feed.bozo_exception}")
            return []

        items = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title or len(title) < 5:
                continue

            # 날짜 파싱
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                try:
                    dt = datetime(*published[:6])
                    date_str = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            else:
                date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            # 요약 추출 (있으면)
            summary = entry.get("summary", "")
            if summary:
                # HTML 태그 제거
                import re
                summary = re.sub(r'<[^>]+>', '', summary).strip()
                if len(summary) > 300:
                    summary = summary[:300] + "..."

            link = entry.get("link", "")

            items.append({
                "date": date_str,
                "source": name,
                "title": title,
                "summary": summary or None,
                "category": category,
                "url": link,
            })

        logger.info(f"  {name}: {len(items)}건 수집")
        return items

    except Exception as e:
        logger.error(f"  {name}: 수집 실패 — {e}")
        return []


# ── 전체 수집 (병렬) ──────────────────────────────────────

def collect_all() -> int:
    """전체 RSS 소스에서 뉴스 수집 (병렬)."""
    if feedparser is None:
        logger.error("feedparser 미설치")
        return 0

    init_db()
    all_items = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_rss_feed, source): source["name"]
            for source in RSS_SOURCES
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                all_items.extend(items)
            except Exception as e:
                logger.error(f"  {name}: 병렬 수집 오류 — {e}")

    if all_items:
        inserted = insert_rows_ignore("news_headlines", all_items)
        log_collection("rss_news", "daily_collect", rows_added=inserted)
        logger.info(f"RSS 뉴스 수집 완료: {inserted}건 (총 {len(all_items)}건 중)")
        return inserted

    logger.info("RSS 뉴스 수집: 0건")
    return 0


def run_daily():
    """일일 자동 실행 (매일 06:00)."""
    logger.info("일일 RSS 뉴스 수집 시작")
    return collect_all()


# ── FOMC/한은 성명서 수집 (Phase 3) ──────────────────────

def fetch_fomc_statements() -> int:
    """
    연준 FOMC 성명서 수집 (Phase 3에서 구현).
    현재는 placeholder.
    """
    # TODO: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
    # PDF/HTML에서 텍스트 추출 → fomc_statements 테이블 저장
    logger.info("FOMC 성명서 수집: Phase 3에서 구현 예정")
    return 0


def fetch_bok_statements() -> int:
    """
    한국은행 통화정책방향 수집 (Phase 3에서 구현).
    현재는 placeholder.
    """
    # TODO: https://www.bok.or.kr 보도자료 섹션
    logger.info("한은 성명서 수집: Phase 3에서 구현 예정")
    return 0


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — RSS 뉴스 수집")
    print("=" * 55)
    result = collect_all()
    print(f"\n  {result}건 저장 완료")
