# tools/news_tools.py — 뉴스 수집 툴
# Phase 6 구현: DART 공시, 네이버 금융 뉴스, 통합 조회, LLM 컨텍스트 생성

import os
import re
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DART_API_KEY = os.getenv("DART_API_KEY", "")
_news_cache: dict = {}
CACHE_TTL = 300


def _is_cache_valid(key):
    if key not in _news_cache: return False
    ts, _ = _news_cache[key]; return time.time() - ts < CACHE_TTL

def _get_cache(key):
    _, data = _news_cache[key]; return data

def _set_cache(key, data):
    _news_cache[key] = (time.time(), data)


def fetch_dart_disclosures(code, days=1, max_items=5):
    """
    DART OpenAPI에서 특정 종목의 최근 공시를 조회한다.
    DART_API_KEY 없으면 빈 리스트 반환.
    반환: [{source, title, corp_name, date, link}, ...]
    """
    cache_key = f"dart_{code}_{days}"
    if _is_cache_valid(cache_key): return _get_cache(cache_key)
    if not DART_API_KEY: return []
    end_date   = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now()-timedelta(days=days)).strftime("%Y%m%d")
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {"crtfc_key":DART_API_KEY,"stock_code":code,
              "bgn_de":start_date,"end_de":end_date,
              "sort":"date","sort_mth":"desc","page_count":str(max_items)}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if data.get("status")!="000": return []
        items=[]
        for item in data.get("list",[])[:max_items]:
            items.append({"source":"DART","title":item.get("report_nm",""),
                "corp_name":item.get("corp_name",""),"date":item.get("rcept_dt",""),
                "link":f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no','')}" })
        _set_cache(cache_key, items); return items
    except Exception as e:
        print(f"  [DART] 오류: {e}"); return []


def fetch_naver_news(code, max_items=10):
    """
    네이버 금융 뉴스 페이지에서 최신 뉴스를 수집한다.
    반환: [{source, title, date, link, code}, ...]
    """
    cache_key = f"naver_{code}"
    if _is_cache_valid(cache_key): return _get_cache(cache_key)
    headers = {"User-Agent":"Mozilla/5.0 (compatible; QUANTUM_FLOW/1.0)"}
    try:
        resp = requests.get(
            f"https://finance.naver.com/item/news_news.naver?code={code}&page=1",
            headers=headers, timeout=10)
        items = _parse_naver_html(resp.text, code, max_items)
        _set_cache(cache_key, items); return items
    except Exception as e:
        print(f"  [네이버뉴스] 오류: {e}"); return []


def _parse_naver_html(html, code, max_items):
    """네이버 금융 HTML에서 기사 제목/링크를 파싱한다."""
    items = []
    try:
        titles = re.findall(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
        links  = re.findall(r'href="(/item/news_read[^"]*)"', html)
        for i, title in enumerate(titles[1:max_items+1]):
            clean = re.sub(r'<[^>]+>','',title).strip()
            if not clean or len(clean)<5: continue
            link = f"https://finance.naver.com{links[i]}" if i<len(links) else ""
            items.append({"source":"NAVER_FINANCE","title":clean,
                "date":datetime.now().strftime("%Y-%m-%d %H:%M"),
                "link":link,"code":code})
    except Exception:
        pass
    return items[:max_items]


def get_all_news(code, days=1, max_per_source=5):
    """DART 공시 + 네이버 뉴스를 통합하여 날짜 최신순으로 반환한다."""
    combined = (fetch_dart_disclosures(code,days=days,max_items=max_per_source)
              + fetch_naver_news(code,max_items=max_per_source))
    combined.sort(key=lambda x: x.get("date",""), reverse=True)
    return combined


def build_news_context(code, max_items=8):
    """LLM에 전달할 뉴스 컨텍스트 문자열 생성. market_watcher에서 사용."""
    news_list = get_all_news(code, days=1, max_per_source=max_items//2)
    if not news_list: return ""
    lines = [f"[{code}] 최근 뉴스/공시 ({len(news_list)}건):"]
    for i, item in enumerate(news_list[:max_items], 1):
        lines.append(f"  {i}. [{item.get('source','')}] {item.get('title','')}  ({item.get('date','')})")
    return "\n".join(lines)


if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW - 뉴스 수집 툴 테스트")
    print("=" * 60)
    test_code = "005930"
    print(f"\n[1] DART 공시 ({test_code})...")
    if not DART_API_KEY:
        print("    DART_API_KEY 없음 — .env에 DART_API_KEY=키 입력 후 사용 (선택)")
    else:
        for item in fetch_dart_disclosures(test_code, days=7):
            print(f"    [{item['date']}] {item['corp_name']} — {item['title']}")
    print(f"\n[2] 네이버 금융 뉴스 ({test_code})...")
    naver = fetch_naver_news(test_code, max_items=5)
    if naver:
        for item in naver: print(f"    [{item['date']}] {item['title'][:50]}")
    else:
        print("    (뉴스 없음 또는 파싱 실패)")
    print(f"\n[3] LLM 컨텍스트...")
    ctx = build_news_context(test_code)
    print(ctx if ctx else "    (컨텍스트 없음)")
    print("\n  Phase 6 news_tools.py - 구현 완료!")
    print("=" * 60)
