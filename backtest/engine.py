"""
engine.py — 백테스트 메인 루프
랜덤 날짜 선택 → Agent1 매크로 분석 → Agent2 종목 선정 → 5일 수익률 측정
"""

import os
import sys
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.data_loader import (
    load_all_daily_csv, get_trading_dates,
    download_fred_history, download_yf_history,
    get_forward_return,
)
from backtest.mock_provider import MockMacroProvider, MockScannerProvider, MockLLMClient

logger = logging.getLogger("backtest.engine")


# ── 결과 데이터 구조 ──

@dataclass
class StockResult:
    code: str
    name: str
    entry_date: str
    entry_price: float
    exit_price: float = 0.0
    return_pct: float = 0.0
    eval_grade: str = ""
    eval_score: float = 0.0
    sector: str = ""


@dataclass
class DayResult:
    date: str
    macro_risk: str = "ON"
    macro_confidence: int = 50
    macro_sectors: List[str] = field(default_factory=list)
    urgent_action: str = "NONE"
    candidates_count: int = 0
    selected_count: int = 0
    stocks: List[StockResult] = field(default_factory=list)
    avg_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    news_count: int = 0
    dart_count: int = 0
    elapsed_sec: float = 0.0


@dataclass
class BacktestResult:
    start_date: str = ""
    end_date: str = ""
    total_days: int = 0
    test_dates: int = 0
    day_results: List[DayResult] = field(default_factory=list)
    # 집계
    avg_return: float = 0.0
    avg_benchmark: float = 0.0
    avg_excess: float = 0.0
    hit_rate: float = 0.0
    risk_off_count: int = 0
    total_stocks: int = 0


# ── Donchian / RSI 필터 (market_scanner 재현) ──

def _calc_donchian(ohlcv: pd.DataFrame, period: int = 20):
    """도치안 채널 상단"""
    if len(ohlcv) < period:
        return None
    return ohlcv["high"].tail(period).max()


def _calc_rsi(ohlcv: pd.DataFrame, period: int = 14):
    """RSI 계산"""
    if len(ohlcv) < period + 1:
        return None
    close = ohlcv["close"].values
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _technical_filter(ohlcv: pd.DataFrame, current_price: float) -> Dict:
    """기술적 필터 (Donchian 근접 + RSI 범위)"""
    donchian_upper = _calc_donchian(ohlcv)
    rsi = _calc_rsi(ohlcv)

    if donchian_upper is None or rsi is None:
        return {"pass": False}

    near_donchian = current_price >= donchian_upper * 0.95
    rsi_ok = 50 <= rsi <= 70

    return {
        "pass": near_donchian or rsi_ok,
        "donchian_upper": donchian_upper,
        "rsi": rsi,
        "near_donchian": near_donchian,
        "score": (1 if near_donchian else 0) + (1 if rsi_ok else 0),
    }


# ══════════════════════════════════════════════════════════════
# 백테스트 엔진
# ══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    백테스트 엔진

    Parameters:
        csv_dir: 키움 collected_data 폴더 경로
        use_real_llm: True면 실제 Claude API, False면 규칙 기반
        use_real_news: True면 네이버 뉴스 크롤링, False면 가상 뉴스
        use_dart: True면 DART 공시 조회 (API 키 필요)
        forward_days: 수익률 측정 기간 (기본 5일)
        top_n: 거래량 상위 종목 수 (기본 50)
        max_select: 최종 선정 종목 수 (기본 10)
        seed: 랜덤 시드
    """

    def __init__(self, csv_dir: str,
                 use_real_llm: bool = False,
                 use_real_news: bool = True,
                 use_dart: bool = True,
                 forward_days: int = 5,
                 top_n: int = 50,
                 max_select: int = 10,
                 seed: int = 42):
        self.csv_dir = csv_dir
        self.use_real_llm = use_real_llm
        self.use_real_news = use_real_news
        self.use_dart = use_dart
        self.forward_days = forward_days
        self.top_n = top_n
        self.max_select = max_select
        self.seed = seed

        random.seed(seed)
        np.random.seed(seed)

        # 데이터 로드
        logger.info("=" * 60)
        logger.info("키움 CSV 데이터 로딩 시작...")
        self.all_data = load_all_daily_csv(csv_dir)
        logger.info(f"로딩 완료: {len(self.all_data)}개 종목")

        if not self.all_data:
            raise ValueError(f"CSV 데이터 없음: {csv_dir}/daily/")

        self.trading_dates = get_trading_dates(self.all_data)
        logger.info(f"거래일: {self.trading_dates[0]} ~ {self.trading_dates[-1]}"
                     f" ({len(self.trading_dates)}일)")

        # FRED/yfinance 데이터는 선택적 (없어도 동작)
        self.fred_data = {}
        self.yf_data = {}
        start_str = str(self.trading_dates[0].date())
        end_str = str(self.trading_dates[-1].date())

        try:
            self.fred_data = download_fred_history(start_str, end_str)
            logger.info(f"FRED 데이터: {len(self.fred_data)}개 시리즈")
        except Exception as e:
            logger.warning(f"FRED 데이터 로드 실패 (매크로 분석 제한됨): {e}")

        try:
            self.yf_data = download_yf_history(start_str, end_str)
            logger.info(f"yfinance 데이터: {len(self.yf_data)}개 시리즈")
        except Exception as e:
            logger.warning(f"yfinance 데이터 로드 실패: {e}")

        # 프로바이더 초기화
        self.macro_provider = MockMacroProvider(
            self.fred_data, self.yf_data,
            use_real_news=use_real_news,
            use_dart=use_dart,
        )
        self.scanner_provider = MockScannerProvider(self.all_data)
        self.llm = MockLLMClient(use_real_llm=use_real_llm)

    def select_test_dates(self, n_dates: int = 50,
                          min_lookback: int = 30,
                          min_forward: int = 10) -> List[pd.Timestamp]:
        """
        테스트할 랜덤 날짜 선택
        - 앞뒤 margin 확보 (lookback용 30일, forward 수익 측정용 10일)
        """
        valid_start = min_lookback
        valid_end = len(self.trading_dates) - min_forward
        if valid_end <= valid_start:
            raise ValueError("테스트 가능한 날짜 범위가 너무 좁습니다")

        indices = sorted(random.sample(
            range(valid_start, valid_end),
            min(n_dates, valid_end - valid_start)
        ))
        dates = [self.trading_dates[i] for i in indices]
        logger.info(f"테스트 날짜 {len(dates)}개 선정: {dates[0]} ~ {dates[-1]}")
        return dates

    def run_single_day(self, test_date: pd.Timestamp) -> DayResult:
        """하루 분량 백테스트 실행"""
        t0 = time.time()
        date_str = str(test_date.date())
        day = DayResult(date=date_str)

        logger.info(f"\n{'─' * 50}")
        logger.info(f"[{date_str}] 백테스트 시작")

        # ── Step 1: Agent1 매크로 분석 ──
        macro_input = self.macro_provider.get_macro_input(date_str)
        day.news_count = len(macro_input.get("news", []))
        day.dart_count = len(macro_input.get("dart", []))

        # LLM 프롬프트에 DART 공시 요약 포함
        dart_section = macro_input.get("dart_summary", "")
        system_msg = (
            "당신은 거시경제 분석 전문가입니다. "
            "매크로 데이터, 뉴스, DART 공시를 종합 분석하여 "
            "risk(ON/OFF), confidence(0-100), sectors(추천 섹터), "
            "avoid_sectors(회피 섹터), sector_multipliers(섹터별 배율 0.5-1.5), "
            "summary, urgent_action(NONE/REDUCE/EXIT_ALL)을 JSON으로 응답하세요. "
            "DART 공시에서 유상증자/감자/상장폐지 등 중대 이벤트가 있으면 "
            "해당 섹터 배율을 낮추고, 실적 서프라이즈는 배율을 높이세요."
        )

        # macro_input에서 dart 원본은 제거 (너무 클 수 있으므로 요약만 전달)
        prompt_data = {
            "macro_data": macro_input["macro_data"],
            "news": macro_input["news"],
            "dart_summary": dart_section,
            "urgent": macro_input["urgent"],
        }

        macro_result = self.llm.analyze_json(
            system_prompt=system_msg,
            user_prompt=json.dumps(prompt_data, ensure_ascii=False, default=str),
        )

        day.macro_risk = macro_result.get("risk", "ON")
        day.macro_confidence = macro_result.get("confidence", 50)
        day.macro_sectors = macro_result.get("sectors", [])
        day.urgent_action = macro_result.get("urgent_action", "NONE")
        sector_multipliers = macro_result.get("sector_multipliers", {})

        logger.info(f"  매크로: risk={day.macro_risk}, confidence={day.macro_confidence}, "
                     f"sectors={day.macro_sectors}")

        # Risk OFF면 스킵
        if day.macro_risk == "OFF" or day.urgent_action in ("REDUCE", "EXIT_ALL"):
            logger.info(f"  ⚠️ Risk OFF 또는 긴급 조치 → 종목 선정 스킵")
            day.elapsed_sec = round(time.time() - t0, 2)
            return day

        # ── Step 2: Agent2 종목 스캐닝 ──
        volume_top = self.scanner_provider.get_volume_top(test_date, self.top_n)
        day.candidates_count = len(volume_top)
        logger.info(f"  거래량 상위: {len(volume_top)}개 후보")

        if not volume_top:
            day.elapsed_sec = round(time.time() - t0, 2)
            return day

        # 기술적 필터
        filtered = []
        for stock in volume_top:
            ohlcv = self.scanner_provider.get_ohlcv(stock["code"], test_date)
            if ohlcv is None:
                continue
            tech = _technical_filter(ohlcv, stock["price"])
            if tech.get("pass"):
                stock.update(tech)
                filtered.append(stock)

        logger.info(f"  기술적 필터 통과: {len(filtered)}개")

        if not filtered:
            day.elapsed_sec = round(time.time() - t0, 2)
            return day

        # LLM 종목 선정 (또는 규칙 기반)
        if self.use_real_llm:
            scanner_input = json.dumps({
                "candidates": [
                    {"code": s["code"], "price": s["price"],
                     "change_pct": s["change_pct"], "volume": s["volume"],
                     "rsi": s.get("rsi", 0), "near_donchian": s.get("near_donchian", False)}
                    for s in filtered[:30]
                ],
                "macro_sectors": day.macro_sectors,
                "sector_multipliers": sector_multipliers,
            }, ensure_ascii=False)

            scanner_result = self.llm.analyze_json(
                system_prompt="당신은 주식 종목 선정 전문가입니다. "
                              "후보 종목 중 최대 10개를 선정하여 "
                              "{\"selected\": [\"종목코드\", ...], \"reason\": \"...\"} "
                              "형태 JSON으로 응답하세요.",
                user_prompt=scanner_input,
            )
            selected_codes = scanner_result.get("selected", [])[:self.max_select]
        else:
            # 규칙 기반: 기술 점수 상위 + 섹터 배율
            for s in filtered:
                base_score = s.get("score", 0) * 30 + s["volume"] / 1e6
                s["final_score"] = base_score
            filtered.sort(key=lambda x: x["final_score"], reverse=True)
            selected_codes = [s["code"] for s in filtered[:self.max_select]]

        day.selected_count = len(selected_codes)
        logger.info(f"  최종 선정: {len(selected_codes)}개 {selected_codes[:5]}...")

        # ── Step 3: 5일 수익률 측정 ──
        returns = self.scanner_provider.get_forward_returns(
            selected_codes, test_date, self.forward_days
        )

        for code in selected_codes:
            ret = returns.get(code)
            if ret is None:
                continue
            # 종목 정보
            stock_info = next((s for s in volume_top if s["code"] == code), {})
            sr = StockResult(
                code=code,
                name=stock_info.get("name", code),
                entry_date=date_str,
                entry_price=stock_info.get("price", 0),
                return_pct=ret,
            )
            day.stocks.append(sr)

        # 평균 수익률
        if day.stocks:
            day.avg_return = round(
                sum(s.return_pct for s in day.stocks) / len(day.stocks), 2
            )

        # 벤치마크 (전체 시장 평균)
        bm = self.scanner_provider.get_kospi_return(test_date, self.forward_days)
        day.benchmark_return = bm if bm is not None else 0.0
        day.excess_return = round(day.avg_return - day.benchmark_return, 2)

        day.elapsed_sec = round(time.time() - t0, 2)

        logger.info(f"  결과: 평균수익 {day.avg_return}%, "
                     f"벤치마크 {day.benchmark_return}%, "
                     f"초과수익 {day.excess_return}% "
                     f"({day.elapsed_sec}초)")

        return day

    def run(self, n_dates: int = 50) -> BacktestResult:
        """전체 백테스트 실행"""
        test_dates = self.select_test_dates(n_dates)

        result = BacktestResult(
            start_date=str(self.trading_dates[0].date()),
            end_date=str(self.trading_dates[-1].date()),
            total_days=len(self.trading_dates),
            test_dates=len(test_dates),
        )

        logger.info(f"\n{'═' * 60}")
        logger.info(f"백테스트 시작: {len(test_dates)}일")
        logger.info(f"{'═' * 60}")

        for i, td in enumerate(test_dates):
            logger.info(f"\n[{i+1}/{len(test_dates)}] ", )
            day_result = self.run_single_day(td)
            result.day_results.append(day_result)

        # ── 집계 ──
        valid_days = [d for d in result.day_results if d.stocks]
        if valid_days:
            result.avg_return = round(
                sum(d.avg_return for d in valid_days) / len(valid_days), 2
            )
            result.avg_benchmark = round(
                sum(d.benchmark_return for d in valid_days) / len(valid_days), 2
            )
            result.avg_excess = round(result.avg_return - result.avg_benchmark, 2)
            result.hit_rate = round(
                len([d for d in valid_days if d.avg_return > 0]) / len(valid_days) * 100, 1
            )
            result.total_stocks = sum(len(d.stocks) for d in valid_days)

        result.risk_off_count = len([d for d in result.day_results
                                     if d.macro_risk == "OFF"])

        logger.info(f"\n{'═' * 60}")
        logger.info(f"백테스트 완료!")
        logger.info(f"  테스트일: {result.test_dates}일 (유효: {len(valid_days)}일)")
        logger.info(f"  평균수익: {result.avg_return}%")
        logger.info(f"  벤치마크: {result.avg_benchmark}%")
        logger.info(f"  초과수익: {result.avg_excess}%")
        logger.info(f"  승률: {result.hit_rate}%")
        logger.info(f"  총 선정종목: {result.total_stocks}개")
        logger.info(f"  Risk OFF: {result.risk_off_count}일")
        logger.info(f"{'═' * 60}")

        return result

    def save_result(self, result: BacktestResult,
                    output_path: str = "backtest/results/result.json"):
        """결과 JSON 저장"""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        data = asdict(result)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"결과 저장: {output_path}")
