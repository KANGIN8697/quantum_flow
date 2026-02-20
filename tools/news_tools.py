# tools/news_tools.py — 뉴스 수집 툴
# Phase 6 구현: DART 공시, 네이버 금융 RSS, 통합 뉴스 조회

import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

# ── 환경변수 ───────────────────────────────────────────────────
DART_API_KEY = os.getenv("DART_API_KEY", "")   # DART OpenAPI 키 (선택)

# 캐시: {key: (timestamp, data)}
_news_cache: dict = {}
CACHE_TTL = 300  # 5분


# ── 내부 유틸 ─────────────────────────────────────────────────

def _is_cache_valid(key: str) -> bool:
    """캐시 유효성 확인."""
    if key not in _news_cache:
        return False
    ts, _ = _news_cache[key]
    return time.time() - ts < CACHE_TTL


def _get_cache(key: str):
    """캐시된 데이터 반환."""
    _, data = _news_cache[key]
    return data


def _set_cache(key: str, data):
    """캐시 저장."""
    _news_cache[key] = (time.time(), data)


def _parse_naver_date(date_str: str) -> str:
    """
    RFC 2822 형식 날짜 → 'YYYY-MM-DD HH:MM' 형식으로 변환.
    예: 'Fri, 20 Feb 2026 09:00:00 +0900' → '2026-02-20 09:00'
    """
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str[:16] if date_str else ""


# ── 1. DART 공시 수집 ─────────────────────────────────────────

def fetch_dart_disclosures(
    code: str,
    days: int = 1,
    max_items: int = 5,
) -> list:
    """
    DART OpenAPI에서 특정 종목의 최근 공시 목록을 조회한다.

    Parameters
    ----------
    code      : 종목코드 (6자리)
    days      : 최근 며칠치 조회 (기본 1일)
    max_items : 최대 반환 건수

    Returns
    -------
    list of dict: [{title, corp_name, rcept_dt, link}, ...]
    """
    cache_key = f"dart_{code}_{days}"
    if _is_cache_valid(cache_key):
        return _get_cache(cache_key)

    if not DART_API_KEY:
        # API 키 없으면 빈 리스트 (시스템 동작에 영향 없음)
        return []

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key":   DART_API_KEY,
        "stock_code":  code,
        "bgn_de":      start_date,
        "end_de":      end_date,
        "sort":        "date",
        "sort_mth":    "desc",
        "page_count":  str(max_items),
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "000":
            return []

        items = []
        for item in data.get("list", [])[:max_items]:
            items.append({
                "source":    "DART",
                "title":     item.get("report_nm", ""),
                "corp_name": item.get("corp_name", ""),
                "date":      item.get("rcept_dt", ""),
                "link":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no', '')}",
            })

        _set_cache(cache_key, items)
        return items

    except Exception as e:
        print(f"  ⚠️  [DART] 공시 조회 오류: {e}")
        return []


# ── 2. 네이버 금융 뉴스 RSS 수집 ─────────────────────────────

def fetch_naver_news(
    code: str,
    max_items: int = 10,
) -> list:
    """
    네이버 금융 RSS에서 특정 종목의 최신 뉴스를 수집한다.

    Parameters
    ----------
    code      : 종목코드 (6자리)
    max_items : 최대 반환 건수 (기본 10)

    Returns
    -------
    list of dict: [{title, source, date, link}, ...]
    """
    cache_key = f"naver_{code}"
    if _is_cache_valid(cache_key):
        return _get_cache(cache_key)

    # 네이버 금융 종목 뉴스 RSS
    rss_url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1&sm=title_entity_id.basic&clusterId="

    # RSS 피드 주소 (네이버 뉴스 검색)
    search_query = quote(code)
    rss_url = f"https://finance.naver.com/item/news.naver?code={code}"

    # 실제 RSS 엔드포인트 사용
    rss_feed_url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; QUANTUM_FLOW/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
        resp = requests.get(
            f"https://search.naver.com/search.naver?where=news&query={code}+주식&sort=1",
            headers=headers,
            timeout=10,
        )

        # RSS 방식으로 재시도
        rss_resp = requests.get(
            f"https://finance.naver.com/item/news_news.naver?code={code}&page=1",
            headers=headers,
            timeout=10,
        )

        # 파싱 시도 (HTML → 제목 추출)
        items = _parse_naver_finance_html(rss_resp.text, code, max_items)

        _set_cache(cache_key, items)
        return items

    except Exception as e:
        print(f"  ⚠️  [네이버뉴스] 수집 오류: {e}")
        return []


def _parse_naver_finance_html(html: str, code: str, max_items: int) -> list:
    """네이버 금융 뉴스 HTML에서 기사 정보를 파싱한다."""
    items = []
    try:
        # 간단한 정규식 없이 기본 파싱 (BeautifulSoup 없이)
        import re
        # 뉴스 제목 패턴 추출
        pattern = r'<title[^>]*>(.*?)</title>'
        titles = re.findall(pattern, html, re.DOTALL)

        # href 패턴 추출
        link_pattern = r'href="(https?://[^"]*n\.news\.naver[^"]*|/item/news_read[^"]*)"'
        links = re.findall(link_pattern, html)

        for i, title in enumerate(titles[1:max_items + 1]):  # 첫 번째는 페이지 제목
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            if not clean_title or len(clean_title) < 5:
                continue
            # i는 titles[1:]에서의 인덱스이므로 links[i]가 올바른 대응
            link_val = links[i] if i < len(links) else ""
            if link_val and not link_val.startswith("http"):
                link_val = f"https://finance.naver.com{link_val}"
            items.append({
                "source": "NAVER_FINANCE",
                "title":  clean_title,
                "date":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "link":   link_val,
                "code":   code,
            })

    except Exception:
        pass

    return items[:max_items]


# ── 3. 통합 뉴스 조회 ─────────────────────────────────────────

def get_all_news(
    code: str,
    days: int = 1,
    max_per_source: int = 5,
) -> list:
    """
    DART 공시 + 네이버 금융 뉴스를 통합하여 반환한다.
    날짜 최신순으로 정렬.

    Parameters
    ----------
    code           : 종목코드
    days           : 조회 기간 (일)
    max_per_source : 소스별 최대 건수

    Returns
    -------
    list of dict (통합, 날짜순)
    """
    dart_news   = fetch_dart_disclosures(code, days=days, max_items=max_per_source)
    naver_news  = fetch_naver_news(code, max_items=max_per_source)

    combined = dart_news + naver_news

    # 날짜 기준 정렬 (최신 우선)
    combined.sort(key=lambda x: x.get("date", ""), reverse=True)

    return combined


# ── 4. 뉴스 요약 텍스트 생성 (LLM 컨텍스트용) ───────────────

def build_news_context(code: str, max_items: int = 8) -> str:
    """
    LLM에 전달할 뉴스 컨텍스트 문자열을 생성한다.
    market_watcher.py의 check_llm_context()에서 사용.

    Returns
    -------
    str: 뉴스 목록 텍스트 (없으면 빈 문자열)
    """
    news_list = get_all_news(code, days=1, max_per_source=max_items // 2)

    if not news_list:
        return ""

    lines = [f"[{code}] 최근 뉴스/공시 ({len(news_list)}건):"]
    for i, item in enumerate(news_list[:max_items], 1):
        source = item.get("source", "")
        title  = item.get("title", "")
        date   = item.get("date", "")
        lines.append(f"  {i}. [{source}] {title}  ({date})")

    return "\n".join(lines)


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — 뉴스 수집 툴 테스트")
    print("=" * 60)

    test_code = "005930"   # 삼성전자

    print(f"\n[1] DART 공시 조회 ({test_code})...")
    if not DART_API_KEY:
        print("    ⚠️  DART_API_KEY 없음 — .env에 DART_API_KEY=키 입력 후 사용")
    else:
        dart = fetch_dart_disclosures(test_code, days=7, max_items=3)
        if dart:
            for item in dart:
                print(f"    [{item['date']}] {item['corp_name']} — {item['title']}")
        else:
            print("    (최근 공시 없음)")

    print(f"\n[2] 네이버 금융 뉴스 조회 ({test_code})...")
    naver = fetch_naver_news(test_code, max_items=5)
    if naver:
        for item in naver:
            print(f"    [{item['date']}] {item['title'][:50]}")
    else:
        print("    (뉴스 없음 또는 파싱 실패)")

    print(f"\n[3] 통합 뉴스 조회...")
    all_news = get_all_news(test_code)
    print(f"    총 {len(all_news)}건")

    print(f"\n[4] LLM 컨텍스트 문자열 생성...")
    ctx = build_news_context(test_code)
    if ctx:
        print(ctx)
    else:
        print("    (컨텍스트 없음)")

    print("\n" + "=" * 60)
    print("  ✅ news_tools.py 테스트 완료!")
    print("=" * 60)
