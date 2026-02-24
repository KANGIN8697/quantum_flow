"""
news_crawler.py — Google News RSS 기반 날짜별 뉴스 크롤링 + JSON 캐시
백테스트 시 특정 날짜의 경제/증시 뉴스 헤드라인을 수집

Google News RSS: 날짜 필터 + 한국어 뉴스 지원, 파싱 안정적
"""

import os
import json
import logging
import time
import random
import re
from datetime import datetime, timedelta
from typing import List, Dict
from urllib.parse import quote

logger = logging.getLogger("backtest.news_crawler")

# ── 검색 키워드 ──
ECONOMY_KEYWORDS = [
    "코스피", "코스닥", "증시",
    "한국은행 금리", "원달러 환율",
    "수출 경제",
]

SECTOR_KEYWORDS = [
    "반도체 주가", "2차전지 배터리", "바이오 제약",
    "AI 인공지능 주식", "자동차 현대 기아",
    "조선 방산", "엔터 주가",
]


def _build_google_news_rss_url(query: str, date_str: str) -> str:
    """
    Google News RSS URL (날짜 필터)
    date_str: YYYY-MM-DD
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    before = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    after = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    encoded = quote(f"{query} after:{after} before:{before}")
    return f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"


def _parse_rss_titles(xml_text: str) -> List[str]:
    """RSS XML에서 뉴스 제목 추출"""
    # CDATA 형식
    titles = re.findall(r'<title><!\[CDATA\[(.+?)\]\]></title>', xml_text)
    if not titles:
        titles = re.findall(r'<title>(.+?)</title>', xml_text)

    # 첫 2개는 피드 자체 제목이므로 스킵
    headlines = []
    for t in titles[2:]:  # 첫 2개는 검색어 + "Google 뉴스"
        t = t.strip()
        # " - 출처명" 부분 제거
        t = re.sub(r'\s*-\s*[^\-]+$', '', t).strip()
        t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        t = t.replace("&quot;", '"').replace("&#39;", "'")
        if t and len(t) > 5:
            headlines.append(t)
    return headlines


def crawl_news_for_date(target_date: str,
                        cache_dir: str = "backtest/cache/news",
                        max_headlines: int = 30) -> List[str]:
    """
    특정 날짜의 경제/증시 뉴스 헤드라인 크롤링 (Google News RSS)

    Parameters:
        target_date: "YYYY-MM-DD" 형식
        cache_dir: 캐시 디렉토리
        max_headlines: 최대 헤드라인 수

    Returns:
        헤드라인 문자열 리스트
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{target_date}.json")

    # 캐시 확인
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        logger.info(f"뉴스 캐시 로드: {target_date} ({len(cached)}건)")
        return cached[:max_headlines]

    import requests

    all_headlines = []
    seen = set()

    # 경제 키워드 전체 + 섹터 키워드 중 3개 랜덤
    keywords = ECONOMY_KEYWORDS + random.sample(
        SECTOR_KEYWORDS, min(3, len(SECTOR_KEYWORDS))
    )

    for keyword in keywords:
        try:
            url = _build_google_news_rss_url(keyword, target_date)
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                titles = _parse_rss_titles(resp.text)
                for title in titles:
                    if title not in seen:
                        seen.add(title)
                        all_headlines.append(title)
            time.sleep(random.uniform(0.3, 0.8))  # 과도한 요청 방지
        except Exception as e:
            logger.warning(f"뉴스 크롤링 실패 [{keyword}]: {e}")
            continue

    logger.info(f"뉴스 크롤링 완료: {target_date} ({len(all_headlines)}건)")

    # 캐시 저장
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(all_headlines, f, ensure_ascii=False, indent=2)

    return all_headlines[:max_headlines]


def generate_mock_news(target_date: str, n: int = 15) -> List[str]:
    """
    크롤링 불가 시 날짜 기반 가상 뉴스 생성
    (실제 뉴스 없이도 백테스트 파이프라인 테스트 가능)
    """
    templates = [
        "코스피, 외국인 매수세에 {dir} 출발",
        "원/달러 환율 {rate}원대...{trend}",
        "반도체 업황 {outlook}...삼성·SK 주가 {dir2}",
        "한국은행 기준금리 동결 전망 {pct}%",
        "2차전지 관련주 {dir} 마감",
        "美 연준 위원 \"{comment}\"",
        "코스닥 {dir} 전환...바이오·AI 강세",
        "수출 {month}월 {change}% {dir3}",
        "外인 {market} {amount}억원 순{buysell}",
        "국제유가 배럴당 {price}달러...{trend2}",
        "증시 전문가 \"{quarter}분기 {outlook2}\"",
        "기관 프로그램 매매 {amount2}억원 {buysell2}",
        "美 나스닥 {pct2}% {dir4}...기술주 {mood}",
        "국내 CPI {cpi}%...물가 {price_trend}",
        "조선·방산주 {dir5}세 지속",
    ]

    random.seed(hash(target_date))
    headlines = []
    for i in range(min(n, len(templates))):
        t = templates[i]
        filled = t.format(
            dir=random.choice(["상승", "하락", "보합", "강보합"]),
            rate=random.choice(["1,300", "1,320", "1,350", "1,280"]),
            trend=random.choice(["강세", "약세", "횡보"]),
            outlook=random.choice(["개선", "부진", "회복세"]),
            dir2=random.choice(["동반 상승", "혼조", "하락"]),
            pct=random.choice(["60", "70", "80"]),
            comment=random.choice(["추가 긴축 필요", "금리 인하 시기상조", "경기 연착륙 가능"]),
            month=random.choice(["1", "3", "6", "9", "12"]),
            change=random.choice(["5.2", "-3.1", "12.4", "-7.8"]),
            dir3=random.choice(["증가", "감소"]),
            market=random.choice(["코스피", "코스닥"]),
            amount=random.choice(["1,200", "3,500", "800"]),
            buysell=random.choice(["매수", "매도"]),
            price=random.choice(["72", "78", "85", "90"]),
            trend2=random.choice(["상승 전환", "하락 지속", "변동성 확대"]),
            quarter=random.choice(["1", "2", "3", "4"]),
            outlook2=random.choice(["긍정적", "보수적", "중립"]),
            amount2=random.choice(["500", "1,000", "2,000"]),
            buysell2=random.choice(["매수", "매도"]),
            pct2=random.choice(["0.5", "1.2", "-0.8", "2.1"]),
            dir4=random.choice(["상승", "하락"]),
            mood=random.choice(["강세", "약세", "혼조"]),
            cpi=random.choice(["2.1", "3.5", "2.8"]),
            price_trend=random.choice(["안정", "상승", "둔화"]),
            dir5=random.choice(["강", "약"]),
        )
        headlines.append(filled)

    return headlines
