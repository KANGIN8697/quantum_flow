# data_collector/text/news_collector.py — 뉴스 데이터 수집
# 네이버 뉴스, Google News 등 다양한 뉴스 소스 수집

import os
import time
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("news_collector")

# ── 환경변수 ───────────────────────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# ── 뉴스 소스 설정 ─────────────────────────────────────────────
NEWS_SOURCES = {
    "naver": {
        "base_url": "https://openapi.naver.com/v1/search/news.json",
        "headers": lambda: {
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        } if NAVER_CLIENT_ID else {},
    },
    "google": {
        "rss_url": "https://news.google.com/rss/search?q={query}+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    },
}

# 캐시
_cache = {}
CACHE_TTL = 600  # 10분


def fetch_naver_news(query: str = "증시 전망", max_items: int = 10) -> list:
    """네이버 뉴스 검색 API로 뉴스 수집"""
    if not NAVER_CLIENT_ID:
        return []

    cache_key = f"naver_news_{query}_{max_items}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        url = NEWS_SOURCES["naver"]["base_url"]
        headers = NEWS_SOURCES["naver"]["headers"]()
        params = {
            "query": query,
            "display": max_items,
            "sort": "date",
            "start": 1,
        }

        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        articles = []
        for item in data.get("items", []):
            title = item.get("title", "").replace("<b>", "").replace("</b>", "")
            description = item.get("description", "").replace("<b>", "").replace("</b>", "")

            articles.append({
                "title": title,
                "description": description[:200],
                "link": item.get("link", ""),
                "pubDate": item.get("pubDate", ""),
                "source": "네이버 뉴스",
                "collected_at": datetime.now().isoformat(),
            })

        _cache[cache_key] = {"data": articles, "ts": time.time()}
        return articles

    except Exception as e:
        logger.error(f"네이버 뉴스 조회 실패: {e}", exc_info=True)
        return []


def fetch_google_news_rss(query: str = "한국 경제 증시", max_items: int = 15) -> list:
    """Google News RSS로 뉴스 수집"""
    cache_key = f"google_news_{query}_{max_items}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        encoded_query = quote(query)
        rss_url = NEWS_SOURCES["google"]["rss_url"].format(query=encoded_query)

        resp = requests.get(rss_url, timeout=10)
        root = ET.fromstring(resp.content)

        articles = []
        for item in root.findall(".//item")[:max_items]:
            articles.append({
                "title": item.findtext("title", ""),
                "description": item.findtext("description", ""),
                "link": item.findtext("link", ""),
                "pubDate": item.findtext("pubDate", ""),
                "source": "Google News",
                "collected_at": datetime.now().isoformat(),
            })

        _cache[cache_key] = {"data": articles, "ts": time.time()}
        return articles

    except Exception as e:
        logger.error(f"Google News RSS 조회 실패: {e}", exc_info=True)
        return []


def collect_economic_news(max_total: int = 20) -> list:
    """경제/증시 뉴스를 여러 소스에서 종합 수집"""
    all_news = []

    # 여러 키워드로 수집
    queries = ["한국 증시", "코스피 전망", "미국 경제 금리", "환율 전망"]

    for query in queries:
        # 네이버 뉴스
        if NAVER_CLIENT_ID:
            naver_articles = fetch_naver_news(query, max_items=5)
            all_news.extend(naver_articles)

        # Google News RSS
        google_articles = fetch_google_news_rss(query, max_items=8)
        all_news.extend(google_articles)

    # 중복 제거 (제목 기준)
    seen_titles = set()
    unique_news = []

    for article in all_news:
        title = article.get("title", "").strip()
        if title and title not in seen_titles:
            seen_titles.add(title)
            unique_news.append(article)

    # 최신순 정렬
    unique_news.sort(key=lambda x: x.get("pubDate", ""), reverse=True)

    return unique_news[:max_total]


def collect_stock_news(stock_code: str, max_items: int = 10) -> list:
    """특정 종목 관련 뉴스 수집"""
    # 종목명 조회 (임시로 코드 사용)
    query = f"{stock_code} 주식"

    news_list = []

    # 네이버 뉴스
    if NAVER_CLIENT_ID:
        news_list.extend(fetch_naver_news(query, max_items))

    # Google News
    news_list.extend(fetch_google_news_rss(query, max_items))

    # 중복 제거
    seen_titles = set()
    unique_news = []

    for article in news_list:
        title = article.get("title", "").strip()
        if title and title not in seen_titles:
            seen_titles.add(title)
            unique_news.append(article)

    return unique_news[:max_items]