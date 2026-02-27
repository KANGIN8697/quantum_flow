"""
mock_provider.py — 실제 API 대신 과거 데이터 주입
키움 CSV 데이터 + FRED 캐시 + 뉴스를 Agent 입력 형태로 변환
"""

import os
import sys
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import pandas as pd

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.data_loader import (
    get_macro_on_date, get_volume_top_on_date,
    get_stock_ohlcv, get_forward_return,
)
from backtest.news_crawler import crawl_news_for_date, generate_mock_news
from backtest.dart_crawler import fetch_dart_disclosures, format_dart_for_agent

logger = logging.getLogger("backtest.mock_provider")


class MockMacroProvider:
    """Agent1 (macro_analyst) 입력 데이터 제공"""

    def __init__(self, fred_data: Dict, yf_data: Dict,
                 use_real_news: bool = True,
                 use_dart: bool = True,
                 news_cache_dir: str = "backtest/cache/news"):
        self.fred_data = fred_data
        self.yf_data = yf_data
        self.use_real_news = use_real_news
        self.use_dart = use_dart
        self.news_cache_dir = news_cache_dir

    def get_macro_input(self, target_date: str) -> Dict:
        """
        특정 날짜의 Agent1 입력 데이터 생성

        Returns:
            {
                "macro_data": { indicator: {value, change_pct, date, source} },
                "news": [ {title, source} ],
                "dart": [ {corp_name, report_nm, rcept_dt, impact, type} ],
                "dart_summary": "공시 요약 텍스트",
                "urgent": { level, total_score, urgent_items }
            }
        """
        # 매크로 데이터
        raw_macro = get_macro_on_date(self.fred_data, self.yf_data, target_date)

        macro_data = {}
        for name, info in raw_macro.items():
            macro_data[name] = {
                "value": info["value"],
                "change_pct": info["change_pct"],
                "date": info["date"],
                "source": "FRED" if name in self.fred_data else "yfinance",
            }

        # 뉴스 헤드라인
        if self.use_real_news:
            try:
                headlines = crawl_news_for_date(target_date, self.news_cache_dir)
            except Exception as e:
                logger.warning(f"뉴스 크롤링 실패, 가상 뉴스 사용: {e}")
                headlines = generate_mock_news(target_date)
        else:
            headlines = generate_mock_news(target_date)

        news_list = [{"title": h, "source": "네이버뉴스"} for h in headlines[:15]]

        # DART 공시 데이터
        dart_disclosures = []
        dart_summary = "DART 공시 데이터 없음"
        if self.use_dart:
            try:
                dart_disclosures = fetch_dart_disclosures(
                    target_date, lookback_days=3, corp_cls=""
                )
                dart_summary = format_dart_for_agent(dart_disclosures)
                logger.info(f"  DART 공시: {len(dart_disclosures)}건 (HIGH: "
                            f"{len([d for d in dart_disclosures if d['impact']=='HIGH'])}건)")
            except Exception as e:
                logger.warning(f"DART 공시 로드 실패: {e}")

        # 긴급 뉴스 판단 (매크로 + DART 공시 기반)
        urgent_info = self._assess_urgency(macro_data, target_date, dart_disclosures)

        return {
            "macro_data": macro_data,
            "news": news_list,
            "dart": dart_disclosures,
            "dart_summary": dart_summary,
            "urgent": urgent_info,
        }

    def _assess_urgency(self, macro_data: Dict, target_date: str,
                         dart_disclosures: List = None) -> Dict:
        """매크로 지표 급변 + DART 공시로 긴급도 판단"""
        score = 0
        items = []

        vix = macro_data.get("VIX", {})
        if vix.get("value", 0) > 30:
            score += 3
            items.append(f"VIX {vix['value']} (공포 수준)")
        elif vix.get("value", 0) > 25:
            score += 1
            items.append(f"VIX {vix['value']} (주의)")

        usdkrw = macro_data.get("USDKRW", {})
        if abs(usdkrw.get("change_pct", 0)) > 1.0:
            score += 2
            items.append(f"USD/KRW 급변 {usdkrw['change_pct']}%")

        sp500 = macro_data.get("SP500", {})
        if sp500.get("change_pct", 0) < -2.0:
            score += 3
            items.append(f"S&P500 급락 {sp500['change_pct']}%")

        # DART 공시 기반 긴급도
        if dart_disclosures:
            high_count = len([d for d in dart_disclosures if d["impact"] == "HIGH"])
            if high_count >= 5:
                score += 2
                items.append(f"DART HIGH 공시 {high_count}건 (시장 이벤트 다수)")
            # 특정 키워드 감지
            for d in dart_disclosures:
                nm = d.get("report_nm", "")
                if any(kw in nm for kw in ["상장폐지", "관리종목", "감자"]):
                    score += 1
                    items.append(f"⚠️ {d['corp_name']}: {nm}")

        level = "LOW"
        if score >= 5:
            level = "CRITICAL"
        elif score >= 2:
            level = "HIGH"

        return {
            "level": level,
            "total_score": score,
            "urgent_items": items,
        }


class MockScannerProvider:
    """Agent2 (market_scanner) 입력 데이터 제공"""

    def __init__(self, all_data: Dict[str, pd.DataFrame]):
        self.all_data = all_data

    def get_volume_top(self, target_date: pd.Timestamp, n: int = 50) -> List[Dict]:
        """거래량 상위 종목 (fetch_volume_top 대체)"""
        return get_volume_top_on_date(self.all_data, target_date, n)

    def get_ohlcv(self, ticker: str, target_date: pd.Timestamp,
                  lookback: int = 25) -> Optional[pd.DataFrame]:
        """종목 OHLCV (_fetch_ohlcv 대체)"""
        return get_stock_ohlcv(self.all_data, ticker, target_date, lookback)

    def get_forward_returns(self, tickers: List[str],
                            entry_date: pd.Timestamp,
                            days: int = 5) -> Dict[str, Optional[float]]:
        """선정 종목들의 N일 후 수익률"""
        results = {}
        for t in tickers:
            results[t] = get_forward_return(self.all_data, t, entry_date, days)
        return results

    def get_kospi_return(self, entry_date: pd.Timestamp, days: int = 5) -> Optional[float]:
        """KOSPI 벤치마크 수익률 (가장 종목 수 많은 ETF 또는 전체 평균)"""
        # KOSPI 지수 데이터가 없으면 전종목 평균으로 대체
        all_returns = []
        for ticker, df in self.all_data.items():
            ret = get_forward_return(self.all_data, ticker, entry_date, days)
            if ret is not None:
                all_returns.append(ret)
        if all_returns:
            return round(sum(all_returns) / len(all_returns), 2)
        return None


class MockLLMClient:
    """
    LLM 호출 대체 — 실제 Claude API 호출
    백테스트에서는 실제 LLM을 사용하되, 비용 절감을 위해 선택적 사용
    """

    def __init__(self, use_real_llm: bool = True, api_key: str = ""):
        self.use_real_llm = use_real_llm
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")

    def analyze_json(self, system_prompt: str, user_prompt: str,
                     model: str = "claude-sonnet-4-5-20250514") -> Dict:
        """
        LLM JSON 분석 호출
        use_real_llm=True: 실제 Claude API 호출
        use_real_llm=False: 규칙 기반 더미 응답
        """
        if self.use_real_llm and self.api_key:
            return self._call_claude(system_prompt, user_prompt, model)
        else:
            return self._rule_based_response(system_prompt, user_prompt)

    def _call_claude(self, system_prompt: str, user_prompt: str,
                     model: str) -> Dict:
        """실제 Claude API 호출"""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=model,
                max_tokens=4000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = response.content[0].text
            # JSON 파싱
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude API 호출 실패: {e}")
            return self._rule_based_response(system_prompt, user_prompt)

    def _rule_based_response(self, system_prompt: str, user_prompt: str) -> Dict:
        """규칙 기반 더미 응답 (LLM 없이 테스트용)"""
        if "macro" in system_prompt.lower() or "거시" in system_prompt.lower():
            return {
                "risk": "ON",
                "confidence": 60,
                "sectors": ["반도체", "2차전지", "자동차"],
                "avoid_sectors": [],
                "sector_multipliers": {
                    "반도체": 1.2, "2차전지": 1.1, "자동차": 1.0,
                    "바이오": 0.9, "금융": 0.8,
                },
                "report": "규칙 기반 분석 (LLM 미사용)",
                "summary": "시장 중립",
                "urgent_action": "NONE",
            }
        elif "scanner" in system_prompt.lower() or "종목" in system_prompt.lower():
            # 유저 프롬프트에서 종목 코드 추출 시도
            import re
            codes = re.findall(r'\b\d{6}\b', user_prompt)
            selected = codes[:10] if codes else []
            return {
                "selected": selected,
                "reason": "규칙 기반 선정 (LLM 미사용)",
            }
        else:
            return {"result": "unknown_prompt_type"}
