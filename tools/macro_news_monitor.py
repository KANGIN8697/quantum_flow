# tools/macro_news_monitor.py — 24시간 롤링 뉴스 모니터링 시스템
# 2026-03-01 구현
#
# 핵심 클래스:
#   RollingNewsBuffer  — 24시간 롤링 윈도우 뉴스 저장소
#   TrendAnalyzer      — 시간대별 키워드 빈도 트렌드 분석
#   NewsCollector      — 다중 소스(연합/Reuters/Google/네이버) 수집 + 긴급도 판정
#
# 데이터 흐름:
#   APScheduler(매시간) → NewsCollector.collect_all() → RollingNewsBuffer
#   → TrendAnalyzer → shared_state 업데이트 → 텔레그램(변화 시만)

import os
import time
import threading
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote
from collections import defaultdict, Counter
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

try:
    import requests
    from requests.adapters import HTTPAdapter, Retry
except ImportError:
    requests = None

try:
    import feedparser
except ImportError:
    feedparser = None

# ── HTTP 세션 (뉴스 수집 공용, TCP 재사용 + 자동 재시도) ──────────
_RETRY = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503])
_SESSION = requests.Session() if requests else None
if _SESSION:
    _SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=_RETRY))
    _SESSION.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=_RETRY))

# ── 환경변수 ──────────────────────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# ── 키워드 (macro_data_tools.py에서 가져옴 — 임포트 순환 방지용 복사) ──
# 실제 긴급도 판정은 macro_data_tools.URGENT_KEYWORDS를 사용하되,
# 이 모듈 내에서도 빠른 pre-screening 용으로 간이 키워드 유지
from tools.macro_data_tools import URGENT_KEYWORDS, URGENT_KEYWORDS_EN

# 긴급도 레벨 순서 (숫자가 클수록 긴급)
URGENCY_LEVELS = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
URGENCY_NAMES = {v: k for k, v in URGENCY_LEVELS.items()}

# 분석 윈도우 (시간 단위)
TREND_WINDOWS = [1, 3, 6, 12, 24]

# 버퍼 최대 보관 시간 (24시간)
BUFFER_MAX_HOURS = 24

# ── 공통 유틸 ─────────────────────────────────────────────────

_USER_AGENT = "Mozilla/5.0 (compatible; QUANTUM_FLOW/2.1 NewsMonitor)"


def _article_hash(title: str) -> str:
    """기사 제목의 해시 (중복 제거용)"""
    # 공백/특수문자 정규화 후 MD5
    normalized = title.strip().lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]


def _score_article(title: str, description: str = "") -> tuple:
    """
    기사의 긴급도 점수 + 매칭된 키워드를 반환한다.
    Returns: (score: int, matched_keywords: list[str])
    """
    text = f"{title} {description}"
    text_lower = text.lower()
    matched = {}

    # 한글 키워드 매칭
    for kw, weight in URGENT_KEYWORDS.items():
        if kw in text:
            matched[kw] = weight

    # 영문 키워드 매칭
    for kw, weight in URGENT_KEYWORDS_EN.items():
        if kw in text_lower:
            matched[kw] = weight

    return sum(matched.values()), list(matched.keys())


# ══════════════════════════════════════════════════════════════
#  RollingNewsBuffer — 24시간 롤링 뉴스 저장소
# ══════════════════════════════════════════════════════════════

class RollingNewsBuffer:
    """
    24시간 롤링 윈도우로 뉴스 기사를 보관한다.
    메모리 기반, thread-safe.

    각 기사 구조:
    {
        "hash": str,          # 제목 기반 해시 (중복 제거)
        "title": str,
        "source": str,        # YONHAP / REUTERS / GOOGLE / NAVER
        "published": str,     # ISO 형식 또는 원본 날짜
        "collected_at": float, # time.time() 수집 시각
        "link": str,
        "score": int,         # 긴급도 점수
        "keywords": list,     # 매칭된 키워드
    }
    """

    def __init__(self, max_hours: int = BUFFER_MAX_HOURS):
        self._articles: list[dict] = []
        self._hashes: set = set()  # 빠른 중복 체크
        self._lock = threading.Lock()
        self._max_seconds = max_hours * 3600

    def add_articles(self, articles: list[dict]) -> int:
        """
        기사 목록을 버퍼에 추가한다.
        중복(해시 기반)은 자동 스킵.
        Returns: 실제 추가된 건수
        """
        added = 0
        now = time.time()

        with self._lock:
            for art in articles:
                h = art.get("hash") or _article_hash(art.get("title", ""))
                if h in self._hashes:
                    continue  # 중복 스킵

                # 점수 계산 (아직 없으면)
                if "score" not in art:
                    score, keywords = _score_article(
                        art.get("title", ""), art.get("description", "")
                    )
                    art["score"] = score
                    art["keywords"] = keywords

                art["hash"] = h
                art.setdefault("collected_at", now)
                self._articles.append(art)
                self._hashes.add(h)
                added += 1

        return added

    def cleanup_expired(self):
        """24시간 초과 기사를 삭제한다."""
        cutoff = time.time() - self._max_seconds
        with self._lock:
            before = len(self._articles)
            self._articles = [a for a in self._articles if a["collected_at"] >= cutoff]
            # 해시 세트도 동기화
            self._hashes = {a["hash"] for a in self._articles}
            removed = before - len(self._articles)
            if removed > 0:
                logger.debug(f"RollingNewsBuffer: {removed}건 만료 삭제 (남은 {len(self._articles)}건)")

    def get_articles_in_window(self, hours: int) -> list[dict]:
        """최근 N시간 이내 기사를 반환한다 (최신순 정렬)."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            result = [a for a in self._articles if a["collected_at"] >= cutoff]
        result.sort(key=lambda x: x["collected_at"], reverse=True)
        return result

    def get_urgent_articles(self, min_score: int = 5, limit: int = 10) -> list[dict]:
        """긴급도 점수가 min_score 이상인 기사를 점수 내림차순으로 반환."""
        with self._lock:
            urgent = [a for a in self._articles if a.get("score", 0) >= min_score]
        urgent.sort(key=lambda x: x.get("score", 0), reverse=True)
        return urgent[:limit]

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._articles)

    def get_state_for_persist(self) -> list[dict]:
        """재시작 복원용 전체 상태 반환."""
        with self._lock:
            return list(self._articles)

    def restore_from_state(self, articles: list[dict]):
        """저장된 상태에서 복원."""
        with self._lock:
            self._articles = articles
            self._hashes = {a.get("hash", "") for a in articles}
        self.cleanup_expired()


# ══════════════════════════════════════════════════════════════
#  TrendAnalyzer — 키워드 빈도 트렌드 분석
# ══════════════════════════════════════════════════════════════

class TrendAnalyzer:
    """
    RollingNewsBuffer의 기사들을 시간 윈도우별로 분석하여
    키워드 빈도 변화(트렌드)를 감지한다.
    LLM 없이 룰 기반.
    """

    def __init__(self, buffer: RollingNewsBuffer):
        self._buffer = buffer
        self._prev_urgency = "NONE"  # 직전 스캔 긴급도

    def analyze_windows(self) -> dict:
        """
        각 윈도우(1h/3h/6h/12h/24h)별 키워드 빈도를 분석한다.

        Returns:
        {
            "1h": {"count": int, "top_keywords": [...], "avg_score": float},
            "3h": {...},
            ...
        }
        """
        result = {}
        for hours in TREND_WINDOWS:
            articles = self._buffer.get_articles_in_window(hours)
            kw_counter = Counter()
            total_score = 0

            for art in articles:
                for kw in art.get("keywords", []):
                    kw_counter[kw] += 1
                total_score += art.get("score", 0)

            result[f"{hours}h"] = {
                "count": len(articles),
                "total_score": total_score,
                "avg_score": round(total_score / max(len(articles), 1), 2),
                "top_keywords": kw_counter.most_common(5),
            }

        return result

    def detect_urgency(self) -> tuple:
        """
        현재 긴급도 레벨을 판정하고, 이전 대비 변화 여부를 반환한다.

        판정 기준 (최근 1시간 기사 기준):
          - total_score ≥ 20  → CRITICAL
          - total_score ≥ 10  → HIGH
          - total_score ≥ 5   → MEDIUM
          - total_score ≥ 1   → LOW
          - 0                 → NONE

        Returns: (urgency_level: str, changed: bool)
        """
        articles_1h = self._buffer.get_articles_in_window(1)
        total_score = sum(a.get("score", 0) for a in articles_1h)

        if total_score >= 20:
            current = "CRITICAL"
        elif total_score >= 10:
            current = "HIGH"
        elif total_score >= 5:
            current = "MEDIUM"
        elif total_score >= 1:
            current = "LOW"
        else:
            current = "NONE"

        changed = current != self._prev_urgency
        self._prev_urgency = current
        return current, changed

    def detect_increasing_trends(self) -> list[dict]:
        """
        키워드 빈도가 급증하는 트렌드를 감지한다.
        3시간 대비 1시간 기사 수가 2배 이상이면 급증으로 판단.

        Returns:
        [{"keyword": str, "count_1h": int, "count_3h": int, "ratio": float}, ...]
        """
        articles_1h = self._buffer.get_articles_in_window(1)
        articles_3h = self._buffer.get_articles_in_window(3)

        kw_1h = Counter()
        kw_3h = Counter()
        for a in articles_1h:
            for kw in a.get("keywords", []):
                kw_1h[kw] += 1
        for a in articles_3h:
            for kw in a.get("keywords", []):
                kw_3h[kw] += 1

        increasing = []
        for kw, count_1h in kw_1h.items():
            count_3h = kw_3h.get(kw, 0)
            # 3시간 중 1시간 비중이 과도하게 높으면 급증
            # 3시간에 3건, 1시간에 3건이면 → 최근에 몰린 것
            if count_3h >= 2 and count_1h >= 2:
                expected_1h = count_3h / 3  # 균등 분포 기대값
                ratio = count_1h / max(expected_1h, 0.5)
                if ratio >= 2.0:
                    increasing.append({
                        "keyword": kw,
                        "count_1h": count_1h,
                        "count_3h": count_3h,
                        "ratio": round(ratio, 1),
                    })

        increasing.sort(key=lambda x: x["ratio"], reverse=True)
        return increasing

    def get_trend_narrative(self) -> str:
        """
        자연어 트렌드 요약을 생성한다 (LLM 없이 룰 기반).
        예: "최근 1시간 '전쟁' 관련 뉴스 5건 급증 (3시간 대비 2.5배)"
        """
        windows = self.analyze_windows()
        increasing = self.detect_increasing_trends()

        parts = []

        # 1시간 요약
        w1h = windows.get("1h", {})
        if w1h.get("count", 0) > 0:
            top_kws = [kw for kw, _ in w1h.get("top_keywords", [])[:3]]
            if top_kws:
                parts.append(
                    f"최근 1시간: {w1h['count']}건 (키워드: {', '.join(top_kws)})"
                )

        # 급증 트렌드
        for trend in increasing[:3]:
            parts.append(
                f"'{trend['keyword']}' 급증 — 1시간 {trend['count_1h']}건 "
                f"(3시간 {trend['count_3h']}건 대비 {trend['ratio']}배)"
            )

        # 24시간 전체 요약
        w24h = windows.get("24h", {})
        if w24h.get("count", 0) > 0:
            parts.append(f"24시간 누적: {w24h['count']}건, 평균 긴급도 {w24h['avg_score']}")

        return " | ".join(parts) if parts else "특이 사항 없음"


# ══════════════════════════════════════════════════════════════
#  NewsCollector — 다중 소스 뉴스 수집기 (싱글턴)
# ══════════════════════════════════════════════════════════════

class NewsCollector:
    """
    6개 소스에서 뉴스를 수집하여 RollingNewsBuffer에 적재한다.
    - 연합뉴스 RSS (한국 속보, 최속)
    - Reuters RSS (글로벌 breaking news)
    - AP통신 RSS (글로벌 속보, Reuters 대안)
    - MarketWatch RSS (미국 시장 특화)
    - Google News RSS (다양성)
    - 네이버 뉴스 API (키워드 확장)
    """

    # Google News RSS (한국 경제)
    GOOGLE_NEWS_QUERIES = ["한국 증시", "코스피 전망", "미국 경제 금리", "전쟁 국제정세"]

    def __init__(self, buffer: RollingNewsBuffer):
        self._buffer = buffer
        self.analyzer = TrendAnalyzer(buffer)
        self._last_collect_time = None

    def collect_all(self) -> dict:
        """
        모든 소스에서 뉴스를 수집하고 버퍼에 적재한다.
        병렬 수집으로 속도 최적화.

        Returns:
        {
            "total_new": int,       # 신규 추가된 기사 수
            "by_source": dict,      # 소스별 수집 건수
            "urgency": str,         # 현재 긴급도
            "urgency_changed": bool,# 변화 여부
            "trend_narrative": str, # 트렌드 요약
            "duration_sec": float,  # 수집 소요 시간
        }
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        start = time.time()
        all_articles = []
        by_source = {}

        # 병렬 수집 (6개 소스)
        collectors = {
            "YONHAP": self._fetch_yonhap,
            "REUTERS": self._fetch_reuters,
            "AP": self._fetch_ap_news,
            "MARKETWATCH": self._fetch_marketwatch,
            "GOOGLE": self._fetch_google_news,
            "NAVER": self._fetch_naver_news,
        }

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in collectors.items()
            }
            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    articles = future.result()
                    by_source[source_name] = len(articles)
                    all_articles.extend(articles)
                except Exception as e:
                    logger.warning(f"[{source_name}] 수집 실패: {e}")
                    by_source[source_name] = 0

        # 버퍼에 적재 (중복 자동 제거)
        total_new = self._buffer.add_articles(all_articles)

        # 만료 기사 정리
        self._buffer.cleanup_expired()

        # 트렌드 분석 + 긴급도 판정
        urgency, urgency_changed = self.analyzer.detect_urgency()
        trend_narrative = self.analyzer.get_trend_narrative()
        trend_windows = self.analyzer.analyze_windows()
        urgent_items = self._buffer.get_urgent_articles(min_score=5, limit=5)

        duration = time.time() - start
        self._last_collect_time = datetime.now()

        logger.info(
            f"뉴스 수집 완료: 신규 {total_new}건 / 총 {self._buffer.total_count}건 "
            f"/ 긴급도 {urgency} / {duration:.1f}초"
        )

        return {
            "total_new": total_new,
            "total_in_buffer": self._buffer.total_count,
            "by_source": by_source,
            "urgency": urgency,
            "urgency_changed": urgency_changed,
            "trend_narrative": trend_narrative,
            "trend_windows": trend_windows,
            "urgent_items": [
                {"title": a.get("title", ""), "score": a.get("score", 0),
                 "keywords": a.get("keywords", []), "source": a.get("source", "")}
                for a in urgent_items
            ],
            "duration_sec": round(duration, 2),
        }

    # ── 개별 소스 수집 함수들 ─────────────────────────────────

    def _fetch_yonhap(self) -> list[dict]:
        """연합뉴스 RSS 수집"""
        if not _SESSION:
            return []
        articles = []
        try:
            # 연합뉴스 주요 RSS 피드들
            feeds = [
                "https://www.yna.co.kr/rss/economy.xml",       # 경제
                "https://www.yna.co.kr/rss/international.xml",  # 국제
                "https://www.yna.co.kr/rss/headline.xml",       # 주요뉴스
            ]
            for feed_url in feeds:
                try:
                    resp = _SESSION.get(
                        feed_url,
                        headers={"User-Agent": _USER_AGENT},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue
                    root = ET.fromstring(resp.content)
                    for item in root.findall(".//item")[:10]:
                        title = item.findtext("title", "").strip()
                        if not title:
                            continue
                        articles.append({
                            "title": title,
                            "source": "YONHAP",
                            "published": item.findtext("pubDate", ""),
                            "link": item.findtext("link", ""),
                            "description": item.findtext("description", "")[:200],
                        })
                except Exception as e:
                    logger.debug(f"연합뉴스 RSS 파싱 실패 ({feed_url}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[YONHAP] 수집 오류: {e}")

        return articles

    def _fetch_reuters(self) -> list[dict]:
        """Reuters RSS 수집 (영문 글로벌 뉴스)"""
        if not _SESSION:
            return []
        articles = []
        try:
            # Reuters 비즈니스/금융 RSS
            feeds = [
                "https://www.reutersagency.com/feed/?best-topics=business-finance",
                "https://www.reutersagency.com/feed/?best-topics=political-general",
            ]
            for feed_url in feeds:
                try:
                    resp = _SESSION.get(
                        feed_url,
                        headers={"User-Agent": _USER_AGENT},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue

                    # feedparser가 있으면 사용, 없으면 XML 파싱
                    if feedparser:
                        feed = feedparser.parse(resp.content)
                        for entry in feed.entries[:10]:
                            title = entry.get("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "REUTERS",
                                "published": entry.get("published", ""),
                                "link": entry.get("link", ""),
                                "description": entry.get("summary", "")[:200],
                            })
                    else:
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item")[:10]:
                            title = item.findtext("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "REUTERS",
                                "published": item.findtext("pubDate", ""),
                                "link": item.findtext("link", ""),
                                "description": item.findtext("description", "")[:200],
                            })
                except Exception as e:
                    logger.debug(f"Reuters RSS 파싱 실패 ({feed_url}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[REUTERS] 수집 오류: {e}")

        return articles

    def _fetch_google_news(self) -> list[dict]:
        """Google News RSS 수집 (한국 경제 키워드)"""
        if not _SESSION:
            return []
        articles = []
        try:
            for query in self.GOOGLE_NEWS_QUERIES:
                encoded = quote(query)
                url = f"https://news.google.com/rss/search?q={encoded}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
                try:
                    if feedparser:
                        feed = feedparser.parse(url)
                        for entry in feed.entries[:8]:
                            title = entry.get("title", "").strip()
                            if not title:
                                continue
                            source = ""
                            if hasattr(entry.get("source", {}), "get"):
                                source = entry.get("source", {}).get("title", "")
                            articles.append({
                                "title": title,
                                "source": f"GOOGLE/{source}" if source else "GOOGLE",
                                "published": entry.get("published", ""),
                                "link": entry.get("link", ""),
                            })
                    else:
                        resp = _SESSION.get(url, timeout=10,
                                           headers={"User-Agent": _USER_AGENT})
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item")[:8]:
                            title = item.findtext("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "GOOGLE",
                                "published": item.findtext("pubDate", ""),
                                "link": item.findtext("link", ""),
                            })
                except Exception as e:
                    logger.debug(f"Google News 파싱 실패 ({query}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[GOOGLE] 수집 오류: {e}")

        return articles

    def _fetch_naver_news(self) -> list[dict]:
        """네이버 뉴스 API 수집 (다양한 경제 키워드)"""
        import re as _re
        if not _SESSION or not NAVER_CLIENT_ID:
            return []

        articles = []
        queries = ["증시 전망", "경제 금리", "국제 정세 전쟁", "코스피 급등 급락"]
        headers = {
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        }

        try:
            for query in queries:
                try:
                    resp = _SESSION.get(
                        "https://openapi.naver.com/v1/search/news.json",
                        headers=headers,
                        params={"query": query, "display": 8, "sort": "date"},
                        timeout=10,
                    )
                    data = resp.json()
                    for item in data.get("items", []):
                        title = _re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        desc = _re.sub(r"<[^>]+>", "", item.get("description", ""))[:200]
                        if not title:
                            continue
                        articles.append({
                            "title": title,
                            "source": "NAVER",
                            "published": item.get("pubDate", ""),
                            "link": item.get("link", ""),
                            "description": desc,
                        })
                except Exception as e:
                    logger.debug(f"네이버 뉴스 검색 실패 ({query}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[NAVER] 수집 오류: {e}")

        return articles

    def _fetch_ap_news(self) -> list[dict]:
        """AP통신 RSS 수집 (글로벌 속보, Reuters 대안)"""
        if not _SESSION:
            return []
        articles = []
        try:
            feeds = [
                "https://rsshub.app/apnews/topics/business",       # 경제/비즈니스
                "https://rsshub.app/apnews/topics/world-news",     # 국제
            ]
            for feed_url in feeds:
                try:
                    resp = _SESSION.get(
                        feed_url,
                        headers={"User-Agent": _USER_AGENT},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue

                    if feedparser:
                        feed = feedparser.parse(resp.content)
                        for entry in feed.entries[:10]:
                            title = entry.get("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "AP",
                                "published": entry.get("published", ""),
                                "link": entry.get("link", ""),
                                "description": entry.get("summary", "")[:200],
                            })
                    else:
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item")[:10]:
                            title = item.findtext("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "AP",
                                "published": item.findtext("pubDate", ""),
                                "link": item.findtext("link", ""),
                                "description": item.findtext("description", "")[:200],
                            })
                except Exception as e:
                    logger.debug(f"AP통신 RSS 파싱 실패 ({feed_url}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[AP] 수집 오류: {e}")

        return articles

    def _fetch_marketwatch(self) -> list[dict]:
        """MarketWatch RSS 수집 (미국 시장 뉴스 특화)"""
        if not _SESSION:
            return []
        articles = []
        try:
            feeds = [
                "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # Top Stories
                "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",  # Market Pulse
                "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",  # Real-time
            ]
            for feed_url in feeds:
                try:
                    resp = _SESSION.get(
                        feed_url,
                        headers={"User-Agent": _USER_AGENT},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue

                    if feedparser:
                        feed = feedparser.parse(resp.content)
                        for entry in feed.entries[:10]:
                            title = entry.get("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "MARKETWATCH",
                                "published": entry.get("published", ""),
                                "link": entry.get("link", ""),
                                "description": entry.get("summary", "")[:200],
                            })
                    else:
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item")[:10]:
                            title = item.findtext("title", "").strip()
                            if not title:
                                continue
                            articles.append({
                                "title": title,
                                "source": "MARKETWATCH",
                                "published": item.findtext("pubDate", ""),
                                "link": item.findtext("link", ""),
                                "description": item.findtext("description", "")[:200],
                            })
                except Exception as e:
                    logger.debug(f"MarketWatch RSS 파싱 실패 ({feed_url}): {e}")
                    continue

        except Exception as e:
            logger.warning(f"[MARKETWATCH] 수집 오류: {e}")

        return articles


# ══════════════════════════════════════════════════════════════
#  싱글턴 인스턴스
# ══════════════════════════════════════════════════════════════

_buffer_instance: RollingNewsBuffer | None = None
_collector_instance: NewsCollector | None = None
_singleton_lock = threading.Lock()


def get_news_buffer() -> RollingNewsBuffer:
    """RollingNewsBuffer 싱글턴 반환"""
    global _buffer_instance
    with _singleton_lock:
        if _buffer_instance is None:
            _buffer_instance = RollingNewsBuffer()
        return _buffer_instance


def get_news_collector() -> NewsCollector:
    """NewsCollector 싱글턴 반환"""
    global _collector_instance
    with _singleton_lock:
        if _collector_instance is None:
            _collector_instance = NewsCollector(get_news_buffer())
        return _collector_instance


# ══════════════════════════════════════════════════════════════
#  메인 실행 함수 (main.py의 job에서 호출)
# ══════════════════════════════════════════════════════════════

def run_hourly_news_scan() -> dict:
    """
    시간별 뉴스 스캔을 실행한다.
    main.py의 job_hourly_news_scan()에서 호출됨.

    Returns: collect_all() 결과 dict
    """
    collector = get_news_collector()
    result = collector.collect_all()
    return result


# ══════════════════════════════════════════════════════════════
#  테스트 블록
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — 24시간 롤링 뉴스 모니터링 테스트")
    print("=" * 60)

    # 1) 수집 테스트
    print("\n[1] 뉴스 수집 시작...")
    result = run_hourly_news_scan()

    print(f"\n  신규 기사: {result['total_new']}건")
    print(f"  버퍼 총량: {result['total_in_buffer']}건")
    print(f"  소스별: {result['by_source']}")
    print(f"  긴급도: {result['urgency']} (변화: {result['urgency_changed']})")
    print(f"  소요 시간: {result['duration_sec']}초")

    # 2) 트렌드 분석
    print(f"\n[2] 트렌드 내러티브:")
    print(f"  {result['trend_narrative']}")

    # 3) 긴급 기사
    if result["urgent_items"]:
        print(f"\n[3] 긴급 기사 ({len(result['urgent_items'])}건):")
        for item in result["urgent_items"]:
            print(f"  [{item['score']}점] {item['title'][:60]}")
            if item["keywords"]:
                print(f"         키워드: {', '.join(item['keywords'])}")
    else:
        print("\n[3] 긴급 기사 없음")

    # 4) 윈도우별 분석
    print(f"\n[4] 윈도우별 분석:")
    for window, data in result.get("trend_windows", {}).items():
        print(f"  {window}: {data['count']}건, 평균 긴급도 {data['avg_score']}")
        if data["top_keywords"]:
            kws = ", ".join(f"{k}({c})" for k, c in data["top_keywords"][:3])
            print(f"        주요 키워드: {kws}")

    print("\n" + "=" * 60)
    print("  ✅ macro_news_monitor.py 테스트 완료!")
    print("=" * 60)
