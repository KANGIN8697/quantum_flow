"""
dart_crawler.py — DART 공시 날짜별 크롤링 + JSON 캐시
백테스트 시 특정 날짜의 기업 공시 정보를 수집

DART OpenAPI: https://opendart.fss.or.kr
- 공시검색: /api/list.json
- Rate limit: 분당 100건 이내
- API 키 필요: 환경변수 DART_API_KEY 또는 dart_api_key.txt
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger("backtest.dart_crawler")

DART_BASE_URL = "https://opendart.fss.or.kr/api"

# ── 공시 유형 필터 (백테스트에 유의미한 공시만) ──
IMPORTANT_DISCLOSURE_TYPES = {
    "A": "정기공시",       # 사업보고서, 분기보고서
    "B": "주요사항보고",   # 유상증자, 주요경영사항
    "C": "발행공시",       # 증권발행
    "D": "지분공시",       # 대량보유 변동
    "I": "거래소공시",     # 거래소 공시
}

# 키워드 기반 공시 중요도 분류
HIGH_IMPACT_KEYWORDS = [
    "유상증자", "무상증자", "감자", "합병",
    "분할", "영업양수", "영업양도",
    "대표이사변경", "최대주주변경",
    "매출액또는손익구조", "실적", "잠정실적",
    "자기주식", "전환사채", "신주인수권부사채",
    "상장폐지", "관리종목",
    "대량보유", "임원ㆍ주요주주",
]

MEDIUM_IMPACT_KEYWORDS = [
    "사업보고서", "분기보고서", "반기보고서",
    "주요사항", "조회공시",
    "타법인주식", "투자판단",
]


def _get_dart_api_key() -> str:
    """DART API 키 로드 (환경변수 → .env 파일 → txt 파일)"""
    key = os.getenv("DART_API_KEY", "")
    if key:
        return key
    # .env 파일에서 읽기
    for env_path in [".env", "../.env", "../../.env",
                     os.path.join(os.path.dirname(__file__), "../../.env")]:
        try:
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DART_API_KEY=") and not line.startswith("#"):
                            val = line.split("=", 1)[1].strip()
                            if val:
                                return val
        except Exception:
            continue
    # txt 파일에서 읽기
    for path in ["dart_api_key.txt", "../dart_api_key.txt", "config/dart_api_key.txt"]:
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read().strip()
    return ""


def _classify_impact(report_nm: str) -> str:
    """공시 제목으로 영향도 분류"""
    for kw in HIGH_IMPACT_KEYWORDS:
        if kw in report_nm:
            return "HIGH"
    for kw in MEDIUM_IMPACT_KEYWORDS:
        if kw in report_nm:
            return "MEDIUM"
    return "LOW"


def fetch_dart_disclosures(target_date: str,
                           lookback_days: int = 3,
                           corp_cls: str = "Y",
                           cache_dir: str = "backtest/cache/dart") -> List[Dict]:
    """
    특정 날짜 기준 DART 공시 조회

    Parameters:
        target_date: "YYYY-MM-DD" 형식
        lookback_days: 며칠 전부터 조회할지 (기본 3일 — 주말/공휴일 대비)
        corp_cls: Y=코스피, K=코스닥, "" = 전체
        cache_dir: 캐시 폴더

    Returns:
        [{"corp_name", "report_nm", "rcept_dt", "impact", "type"}, ...]
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{target_date}_{corp_cls}.json")

    # 캐시 확인
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        logger.info(f"DART 캐시 로드: {target_date} ({len(cached)}건)")
        return cached

    api_key = _get_dart_api_key()
    if not api_key:
        logger.warning("DART_API_KEY 미설정 — 공시 데이터 없이 진행")
        return []

    import requests

    # 날짜 계산
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    bgn_de = (dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_de = dt.strftime("%Y%m%d")

    all_disclosures = []

    # 코스피 + 코스닥 조회
    markets = [corp_cls] if corp_cls else ["Y", "K"]

    for mkt in markets:
        page = 1
        while page <= 5:  # 최대 5페이지 (500건)
            try:
                resp = requests.get(
                    f"{DART_BASE_URL}/list.json",
                    params={
                        "crtfc_key": api_key,
                        "bgn_de": bgn_de,
                        "end_de": end_de,
                        "corp_cls": mkt,
                        "page_no": page,
                        "page_count": 100,
                        "sort": "date",
                        "sort_mth": "desc",
                    },
                    timeout=10,
                )
                data = resp.json()

                if data.get("status") != "000":
                    # 000=정상, 013=조회된데이터없음
                    if data.get("status") == "013":
                        break
                    logger.warning(f"DART API 오류: {data.get('message', '')}")
                    break

                items = data.get("list", [])
                if not items:
                    break

                for item in items:
                    report_nm = item.get("report_nm", "")
                    impact = _classify_impact(report_nm)

                    # LOW 영향도는 스킵 (너무 많음)
                    if impact == "LOW":
                        continue

                    all_disclosures.append({
                        "corp_name": item.get("corp_name", ""),
                        "corp_code": item.get("corp_code", ""),
                        "stock_code": item.get("stock_code", ""),
                        "report_nm": report_nm,
                        "rcept_dt": item.get("rcept_dt", ""),
                        "flr_nm": item.get("flr_nm", ""),  # 공시 제출인
                        "type": IMPORTANT_DISCLOSURE_TYPES.get(
                            item.get("pblntf_ty", ""), "기타"
                        ),
                        "impact": impact,
                    })

                # 다음 페이지
                total_page = data.get("total_page", 1)
                if page >= total_page:
                    break
                page += 1
                time.sleep(0.7)  # Rate limit: 분당 100건

            except Exception as e:
                logger.error(f"DART API 호출 실패: {e}")
                break

    # 캐시 저장
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(all_disclosures, f, ensure_ascii=False, indent=2)

    logger.info(f"DART 공시 수집: {target_date} ({len(all_disclosures)}건)")
    return all_disclosures


def format_dart_for_agent(disclosures: List[Dict]) -> str:
    """DART 공시 리스트 → Agent 입력용 텍스트"""
    if not disclosures:
        return "최근 주요 공시 없음"

    lines = ["[최근 주요 공시]"]

    # HIGH 먼저
    high = [d for d in disclosures if d["impact"] == "HIGH"]
    medium = [d for d in disclosures if d["impact"] == "MEDIUM"]

    for d in high[:10]:
        lines.append(f"⚠️ [{d['rcept_dt']}] {d['corp_name']}: {d['report_nm']}")

    for d in medium[:10]:
        lines.append(f"📋 [{d['rcept_dt']}] {d['corp_name']}: {d['report_nm']}")

    lines.append(f"(HIGH: {len(high)}건, MEDIUM: {len(medium)}건)")
    return "\n".join(lines)


def get_stock_disclosures(disclosures: List[Dict],
                          stock_code: str) -> List[Dict]:
    """특정 종목의 공시만 필터"""
    # stock_code는 6자리 (000020), DART에서는 앞 0 포함
    return [d for d in disclosures
            if d.get("stock_code", "").strip() == stock_code.strip()]
