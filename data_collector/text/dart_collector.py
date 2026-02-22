# data_collector/text/dart_collector.py — 전자공시(DART) 데이터 수집
# 금융감독원 전자공시시스템에서 기업 공시 정보 수집

import os
import time
import logging
import requests
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("dart_collector")

# ── 환경변수 ───────────────────────────────────────────────────
DART_API_KEY = os.getenv("DART_API_KEY", "")

# ── DART API 설정 ──────────────────────────────────────────────
DART_BASE_URL = "https://opendart.fss.or.kr/api"

# 캐시
_cache = {}
CACHE_TTL = 1800  # 30분

def fetch_dart_disclosures(corp_code: str = "", max_items: int = 20) -> list:
    """DART 공시 목록 조회"""
    if not DART_API_KEY:
        return []

    cache_key = f"dart_disclosures_{corp_code}_{max_items}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        url = f"{DART_BASE_URL}/list.json"
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de": (datetime.now() - timedelta(days=7)).strftime("%Y%m%d"),
            "end_de": datetime.now().strftime("%Y%m%d"),
            "page_count": max_items,
            "sort": "date",
            "sort_mth": "desc",
        }

        if corp_code:
            params["corp_code"] = corp_code

        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if data.get("status") != "000":
            logger.error(f"DART API 오류: {data}")
            return []

        disclosures = []
        for item in data.get("list", [])[:max_items]:
            disclosures.append({
                "corp_code": item.get("corp_code", ""),
                "corp_name": item.get("corp_name", ""),
                "report_nm": item.get("report_nm", ""),
                "rcept_no": item.get("rcept_no", ""),
                "rcept_dt": item.get("rcept_dt", ""),
                "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no', '')}",
                "source": "DART",
            })

        _cache[cache_key] = {"data": disclosures, "ts": time.time()}
        return disclosures

    except Exception as e:
        logger.error(f"DART 공시 조회 실패: {e}", exc_info=True)
        return []

def fetch_major_disclosures(max_items: int = 50) -> list:
    """주요 공시 (매출, 실적 등) 조회"""
    try:
        all_disclosures = fetch_dart_disclosures(max_items=max_items * 2)

        # 주요 공시 필터링
        major_keywords = [
            "사업보고서", "반기보고서", "분기보고서",
            "매출", "영업이익", "순이익", "실적",
            "배당", "주식매수", "자사주",
            "유상증자", "무상증자", "주식분할",
            "인수", "합병", "지분취득",
        ]

        major_disclosures = []
        for disc in all_disclosures:
            title = disc.get("report_nm", "")
            if any(keyword in title for keyword in major_keywords):
                major_disclosures.append(disc)
                if len(major_disclosures) >= max_items:
                    break

        return major_disclosures

    except Exception as e:
        logger.error(f"주요 공시 조회 실패: {e}", exc_info=True)
        return []

def fetch_corp_info(corp_code: str) -> dict:
    """기업 기본 정보 조회"""
    if not DART_API_KEY:
        return {}

    cache_key = f"dart_corp_{corp_code}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        url = f"{DART_BASE_URL}/company.json"
        params = {
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
        }

        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        if data.get("status") != "000":
            return {}

        corp_info = {
            "corp_code": data.get("corp_code", ""),
            "corp_name": data.get("corp_name", ""),
            "corp_name_eng": data.get("corp_name_eng", ""),
            "stock_code": data.get("stock_code", ""),
            "ceo_nm": data.get("ceo_nm", ""),
            "corp_cls": data.get("corp_cls", ""),
            "jurir_no": data.get("jurir_no", ""),
            "bizr_no": data.get("bizr_no", ""),
            "adres": data.get("adres", ""),
            "hm_url": data.get("hm_url", ""),
            "ir_url": data.get("ir_url", ""),
            "phn_no": data.get("phn_no", ""),
            "fax_no": data.get("fax_no", ""),
            "induty_code": data.get("induty_code", ""),
            "est_dt": data.get("est_dt", ""),
            "acc_mt": data.get("acc_mt", ""),
        }

        _cache[cache_key] = {"data": corp_info, "ts": time.time()}
        return corp_info

    except Exception as e:
        logger.error(f"기업 정보 조회 실패: {e}", exc_info=True)
        return {}

def search_corp_by_name(corp_name: str) -> list:
    """기업명으로 기업 코드 검색"""
    if not DART_API_KEY:
        return []

    try:
        url = f"{DART_BASE_URL}/corpCode.xml"
        params = {"crtfc_key": DART_API_KEY}

        resp = requests.get(url, params=params, timeout=15)
        # XML 파싱 필요하지만 간단히 구현
        # 실제로는 xml.etree.ElementTree 사용

        # 임시로 빈 리스트 반환 (실제 구현 시 XML 파싱 추가)
        return []

    except Exception as e:
        logger.error(f"기업 검색 실패: {e}", exc_info=True)
        return []