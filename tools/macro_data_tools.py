# tools/macro_data_tools.py — 거시경제 데이터 + 뉴스 수집 도구
# FRED API (VIX, DXY, TNX, SP500, USD/KRW)
# Google News RSS (한국 경제/증시 뉴스)
# 네이버 뉴스 검색 (경제 키워드)
# 긴급 뉴스 감지 (키워드 기반)

import os
import json
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from urllib.parse import quote
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

def safe_float(val, default=0.0):
    """pandas Series/numpy -> float safely"""
    try:
        if hasattr(val, 'iloc'): val = val.iloc[-1]
        if hasattr(val, 'item'): return float(val.item())
        return float(val)
    except (TypeError, ValueError, IndexError): return default



load_dotenv()

try:
    import requests
except ImportError:
    requests = None

try:
    import feedparser
except ImportError:
    feedparser = None

# ── 설정 ──────────────────────────────────────────────────
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# FRED 시리즈 ID
FRED_SERIES_DAILY = {
    "VIX":       "VIXCLS",      # VIX 공포지수
    "DXY":       "DTWEXBGS",    # 달러 인덱스 (Broad)
    "TNX":       "DGS10",       # 미국채 10년 금리
    "SP500":     "SP500",       # S&P 500
    "USDKRW":   "DEXKOUS",     # 달러/원 환율
    "FEDFUNDS": "FEDFUNDS",    # 연방기금금리
    "T10Y2Y":   "T10Y2Y",      # 10년-2년 금리차
    "T10YIE":   "T10YIE",      # 10년 기대인플레이션
}

FRED_SERIES_TREND = {
    "CPI":       "CPIAUCSL",    # 소비자물가지수 (월간)
    "CORE_CPI":  "CPILFESL",    # 근원 CPI (월간)
    "PCE":       "PCEPI",       # PCE 물가지수 (월간)
    "M2":        "WM2NS",       # M2 통화량 (주간)
    "FED_ASSETS":"WALCL",       # 연준 총자산 (주간)
    "ICSA":      "ICSA",        # 신규 실업수당 (주간)
    "UNRATE":    "UNRATE",      # 실업률 (월간)
    "UMCSENT":   "UMCSENT",     # 소비자심리지수 (월간)
}

FRED_SERIES = {**FRED_SERIES_DAILY, **FRED_SERIES_TREND}

# 긴급 뉴스 키워드 (가중치 포함)
URGENT_KEYWORDS = {
    "서킷브레이커": 10, "사이드카": 8,
    "전쟁": 9, "미사일": 8, "폭격": 9,
    "폭락": 7, "급락": 6, "패닉": 7,
    "금리인상": 5, "긴급": 5,
    "디폴트": 8, "파산": 7, "부도": 7,
    "테러": 9, "계엄": 9,
    "리세션": 6, "경기침체": 6,
    "뱅크런": 8, "유동성위기": 7,
    "블랙먼데이": 9, "블랙스완": 8,
}

_cache = {}
CACHE_TTL = 600  # 10분


# ── 1. FRED API ───────────────────────────────────────────

def fetch_fred_series(series_id: str, days_back: int = 10) -> dict:
    """FRED에서 특정 시리즈의 최근 데이터 조회"""
    if not FRED_API_KEY or not requests:
        return {"value": 0.0, "date": "", "error": "FRED_API_KEY 미설정"}
    
    cache_key = f"fred_{series_id}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]
    
    end = date.today()
    start = end - timedelta(days=days_back)
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}"
        f"&api_key={FRED_API_KEY}"
        f"&file_type=json"
        f"&sort_order=desc"
        f"&limit=5"
        f"&observation_start={start.isoformat()}"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        obs = data.get("observations", [])
        # 유효한 값 찾기 (. 은 미발표)
        for o in obs:
            if o["value"] != ".":
                result = {
                    "value": float(o["value"]),
                    "date": o["date"],
                }
                _cache[cache_key] = {"data": result, "ts": time.time()}
                return result
        return {"value": 0.0, "date": "", "error": "데이터 없음"}
    except Exception as e:
        return {"value": 0.0, "date": "", "error": str(e)}


def fetch_all_fred() -> dict:
    """모든 FRED 거시 지표를 한번에 조회"""
    results = {}
    for name, sid in FRED_SERIES_DAILY.items():
        results[name] = fetch_fred_series(sid)
    # Trend indicators (90-day lookback)
    for name, sid in FRED_SERIES_TREND.items():
        results[name] = fetch_fred_series(sid, days_back=90)
    return results


# ── 2. yfinance 보완 (최근 종가 — 주말에도 가능) ──────────

def fetch_yfinance_recent() -> dict:
    """yfinance로 최근 5일간 종가 조회 (주말에도 직전 거래일 데이터 반환)"""
    try:
        import yfinance as yf
    except ImportError as e:
        logger.debug(f"tools/macro_data_tools.py: {type(e).__name__}: {e}")
        return {}
    
    symbols = {
        "VIX": "^VIX", "DXY": "DX-Y.NYB", "TNX": "^TNX",
        "SP500": "^GSPC", "USDKRW": "USDKRW=X", "KOSPI": "^KS11",
    }
    results = {}
    for name, ticker in symbols.items():
        try:
            df = yf.download(ticker, period="5d", progress=False)
            if not df.empty:
                last = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else last
                close_val = safe_float(last["Close"].iloc[0]) if hasattr(last["Close"], "iloc") else safe_float(last["Close"])
                prev_val = safe_float(prev["Close"].iloc[0]) if hasattr(prev["Close"], "iloc") else safe_float(prev["Close"])
                chg = ((close_val - prev_val) / (prev_val or 1) * 100) if prev_val else 0
                results[name] = {
                    "value": round(close_val, 2),
                    "change_pct": round(chg, 2),
                    "date": str(df.index[-1].date()),
                }
            else:
                results[name] = {"value": 0.0, "change_pct": 0.0}
        except Exception:
            results[name] = {"value": 0.0, "change_pct": 0.0}
    return results


# ── 3. 뉴스 수집 (Google News RSS + 네이버) ───────────────

def fetch_google_news_rss(query: str = "한국 경제 증시", max_items: int = 15) -> list:
    """Google News RSS로 뉴스 헤드라인 수집"""
    if feedparser is None:
        return _fetch_google_news_fallback(query, max_items)
    
    encoded = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
    
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:max_items]:
            articles.append({
                "title": entry.get("title", ""),
                "source": entry.get("source", {}).get("title", "") if hasattr(entry.get("source", {}), "get") else "",
                "published": entry.get("published", ""),
                "link": entry.get("link", ""),
            })
        return articles
    except Exception:
        return _fetch_google_news_fallback(query, max_items)


def _fetch_google_news_fallback(query: str, max_items: int) -> list:
    """feedparser 없을 때 requests + XML 파싱으로 대체"""
    if not requests:
        return []
    encoded = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
    try:
        resp = requests.get(url, timeout=10)
        root = ET.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:max_items]:
            articles.append({
                "title": item.findtext("title", ""),
                "source": item.findtext("source", ""),
                "published": item.findtext("pubDate", ""),
                "link": item.findtext("link", ""),
            })
        return articles
    except Exception as e:
        logger.debug(f"tools/macro_data_tools.py: {type(e).__name__}: {e}")
        return []


def fetch_naver_news_search(query: str = "증시 전망", max_items: int = 10) -> list:
    """네이버 뉴스 검색 API로 뉴스 수집"""
    if not requests or not NAVER_CLIENT_ID:
        return []
    
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": max_items, "sort": "date"}
    
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        articles = []
        for item in data.get("items", []):
            title = re.sub(r"<[^>]+>", "", item.get("title", ""))
            desc = re.sub(r"<[^>]+>", "", item.get("description", ""))
            articles.append({
                "title": title,
                "description": desc[:100],
                "published": item.get("pubDate", ""),
                "link": item.get("link", ""),
            })
        return articles
    except Exception as e:
        logger.debug(f"tools/macro_data_tools.py: {type(e).__name__}: {e}")
        return []


def collect_macro_news(max_total: int = 20) -> list:
    """경제/증시 뉴스를 여러 소스에서 종합 수집"""
    all_news = []
    
    # Google News RSS — 여러 키워드
    for q in ["한국 증시", "코스피 전망", "미국 경제 금리"]:
        articles = fetch_google_news_rss(q, max_items=8)
        all_news.extend(articles)
    
    # 네이버 뉴스 (API 키 있을 때)
    for q in ["증시 전망", "경제 금리"]:
        articles = fetch_naver_news_search(q, max_items=5)
        all_news.extend(articles)
    
    # 중복 제거 (제목 기준)
    seen = set()
    unique = []
    for a in all_news:
        title = a.get("title", "").strip()
        if title and title not in seen:
            seen.add(title)
            unique.append(a)
    
    return unique[:max_total]


# ── 4. 긴급 뉴스 감지 ────────────────────────────────────

def check_urgent_news(news_list: list = None) -> dict:
    """뉴스 목록에서 긴급 키워드를 탐지하고 위험도 점수 반환"""
    if news_list is None:
        news_list = collect_macro_news(max_total=30)
    
    urgent_items = []
    total_score = 0
    
    for article in news_list:
        title = article.get("title", "")
        desc = article.get("description", "")
        text = f"{title} {desc}"
        
        matched = {}
        for keyword, weight in URGENT_KEYWORDS.items():
            if keyword in text:
                matched[keyword] = weight
        
        if matched:
            score = sum(matched.values())
            total_score += score
            urgent_items.append({
                "title": title,
                "keywords": list(matched.keys()),
                "score": score,
            })
    
    # 위험 등급 판정
    if total_score >= 20:
        level = "CRITICAL"   # 즉시 전량 매도 권고
    elif total_score >= 10:
        level = "HIGH"       # 신규 매수 중단 + 부분 청산
    elif total_score >= 5:
        level = "MEDIUM"     # 포지션 축소 권고
    else:
        level = "LOW"        # 정상
    
    return {
        "level": level,
        "total_score": total_score,
        "urgent_count": len(urgent_items),
        "urgent_items": urgent_items[:5],  # 상위 5개만
        "checked_at": datetime.now().isoformat(),
    }


# ── 5. 종합 거시경제 데이터 수집 ──────────────────────────

def collect_all_macro_data() -> dict:
    """FRED + yfinance + 뉴스를 모두 수집하여 하나의 dict로 반환"""
    result = {
        "timestamp": datetime.now().isoformat(),
    }
    
    # 1) FRED 데이터
    print("  📊 FRED 거시지표 수집 중...")
    fred_data = fetch_all_fred()
    result["fred"] = fred_data
    
    # 2) yfinance 최근 종가
    print("  📈 yfinance 최근 종가 수집 중...")
    yf_data = fetch_yfinance_recent()
    result["yfinance"] = yf_data
    
    # 3) 거시 데이터 통합 (FRED 우선, yfinance 보완)
    merged = {}
    YF_PREFER = {"DXY", "USDKRW"}
    for key in ["VIX", "DXY", "TNX", "SP500", "USDKRW"]:
        fred_val = fred_data.get(key, {})
        yf_val = yf_data.get(key, {})
        
        if key in YF_PREFER and yf_val.get("value", 0) > 0:
            merged[key] = {"value": yf_val["value"], "change_pct": yf_val.get("change_pct", 0), "date": yf_val.get("date", ""), "source": "yfinance", "fred_value": fred_val.get("value", 0), "fred_date": fred_val.get("date", "")}
        elif fred_val.get("value", 0) > 0:
            merged[key] = {
                "value": fred_val["value"],
                "date": fred_val.get("date", ""),
                "source": "FRED",
            }
        elif yf_val.get("value", 0) > 0:
            merged[key] = {
                "value": yf_val["value"],
                "change_pct": yf_val.get("change_pct", 0),
                "date": yf_val.get("date", ""),
                "source": "yfinance",
            }
        else:
            merged[key] = {"value": 0.0, "source": "없음"}
    
    # KOSPI는 yfinance만
    kospi = yf_data.get("KOSPI", {})
    merged["KOSPI"] = {
        "value": kospi.get("value", 0),
        "change_pct": kospi.get("change_pct", 0),
        "source": "yfinance",
    }
    
    # FEDFUNDS는 FRED만
    ff = fred_data.get("FEDFUNDS", {})
    merged["FEDFUNDS"] = {
        "value": ff.get("value", 0),
        "date": ff.get("date", ""),
        "source": "FRED",
    }
    
    result["macro_data"] = merged
    
    # 4) 뉴스 수집
    print("  📰 경제 뉴스 수집 중...")
    news = collect_macro_news(max_total=20)
    result["news"] = news
    result["news_count"] = len(news)
    
    # 5) 긴급 뉴스 체크
    urgent = check_urgent_news(news)
    result["urgent"] = urgent
    
    print(f"  ✅ 수집 완료: 지표 {len(merged)}개, 뉴스 {len(news)}건, 긴급={urgent['level']}")
    

    # Trend data
    trend_data = {}
    for k in FRED_SERIES_TREND:
        if k in fred_data and fred_data[k].get("value", 0) > 0:
            trend_data[k] = fred_data[k]
    result["trend_data"] = trend_data
    result["trend_summary"] = _calc_trend_summary({**trend_data, **{k:v for k,v in fred_data.items() if k in ("T10Y2Y","T10YIE")}}
)
    return result


# ── 테스트 ────────────────────────────────────────────────


def _calc_trend_summary(td: dict) -> dict:
    s = {}
    if "M2" in td:
        v = td["M2"]["value"]
        s["M2"] = f"${v/1e3:.1f}T" if v > 1000 else f"${v:.0f}B"
    if "FED_ASSETS" in td:
        v = td["FED_ASSETS"]["value"]
        s["FED_ASSETS"] = f"${v/1e6:.2f}T" if v > 1e5 else f"${v:.0f}M"
    if "CPI" in td:
        s["CPI"] = f"{td['CPI']['value']:.1f}"
    if "UNRATE" in td:
        s["UNRATE"] = f"{td['UNRATE']['value']:.1f}%"
    if "UMCSENT" in td:
        s["UMCSENT"] = f"{td['UMCSENT']['value']:.1f}"
    if "ICSA" in td:
        s["ICSA"] = f"{td['ICSA']['value']/1e3:.0f}K"
    if "T10Y2Y" in td:
        v = td["T10Y2Y"]["value"]
        s["yield_curve"] = "inverted" if v < 0 else "normal"
    return s
if __name__ == "__main__":
    print("=" * 60)
    print(" MacroDataTools 테스트")
    print("=" * 60)
    
    data = collect_all_macro_data()
    
    print(f"\n거시 지표:")
    for k, v in data["macro_data"].items():
        print(f"  {k}: {v}")
    
    print(f"\n뉴스 {data['news_count']}건:")
    for n in data["news"][:5]:
        print(f"  - {n['title'][:60]}")
    
    print(f"\n긴급 뉴스 레벨: {data['urgent']['level']} (점수: {data['urgent']['total_score']})")
