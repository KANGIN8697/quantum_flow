# data_collector/macro_intelligence.py — MacroIntelligence 인터페이스
#
# Agent 1(매크로 분석관)이 호출하는 통합 인터페이스 클래스
# AWS 서버에서 이 모듈을 import하여 로컬 DB(S3 경유) 데이터를 참조

import os
import sys
import logging
from datetime import datetime

logger = logging.getLogger("macro_intelligence")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.db_manager import query, init_db


class MacroIntelligence:
    """
    Agent 1이 호출하는 매크로 인텔리전스 통합 인터페이스.

    사용법:
        mi = MacroIntelligence()
        regime = mi.get_today_regime()
        similar = mi.get_similar_historical_periods(n=5)
        snapshot = mi.get_macro_snapshot()
        multipliers = mi.get_sector_macro_multiplier()
    """

    def __init__(self):
        init_db()

    # ── 1. 오늘 Regime 조회 ───────────────────────────────

    def get_today_regime(self) -> dict:
        """
        오늘 날짜의 regime 반환.

        Returns
        -------
        dict: {"regime": "normal"|"stress"|"extreme",
               "score": int, "triggers": list}
        """
        import json
        today = datetime.now().strftime("%Y-%m-%d")
        rows = query("SELECT * FROM market_regime WHERE date=?", (today,))

        if rows:
            r = rows[0]
            triggers = json.loads(r.get("trigger_reasons", "[]"))
            return {
                "regime": r["regime"],
                "score": r["score"],
                "triggers": triggers,
                "vix": r.get("vix"),
                "kospi_change": r.get("kospi_change"),
                "usd_krw_change": r.get("usd_krw_change"),
                "sp500_change": r.get("sp500_change"),
            }

        # DB에 없으면 실시간 분류
        try:
            from data_collector.regime.regime_classifier import classify_regime
            result = classify_regime(today)
            triggers = json.loads(result.get("trigger_reasons", "[]"))
            return {
                "regime": result["regime"],
                "score": result["score"],
                "triggers": triggers,
            }
        except Exception as e:
            logger.warning(f"Regime 분류 실패: {e}")
            return {"regime": "normal", "score": 0, "triggers": []}

    # ── 2. 유사 과거 국면 검색 ────────────────────────────

    def get_similar_historical_periods(self, n: int = 5) -> list:
        """
        오늘 상황과 유사한 과거 국면 검색.

        Returns
        -------
        list of dict: [{date, regime, similarity, kospi_3d, summary}, ...]
        """
        try:
            from data_collector.vector.vector_store_builder import (
                find_similar_periods, _build_daily_summary
            )
            today = datetime.now().strftime("%Y-%m-%d")
            today_summary = _build_daily_summary(today)
            return find_similar_periods(today_summary, n=n)
        except Exception as e:
            logger.warning(f"유사 국면 검색 실패: {e}")
            return []

    # ── 3. 매크로 스냅샷 ──────────────────────────────────

    def get_macro_snapshot(self) -> dict:
        """
        주요 매크로 지표 최신값 반환.

        Returns
        -------
        dict: {"vix": float, "usd_krw": float, "sp500_change": float,
               "fed_rate": float, "kr_rate": float, "t10y2y": float, ...}
        """
        snapshot = {}

        # 해외 지표 (global_daily 최신)
        global_keys = {
            "^VIX": "vix",
            "KRW=X": "usd_krw",
            "^GSPC": "sp500",
            "^TNX": "us_10y",
            "DX-Y.NYB": "dxy",
            "CL=F": "wti",
            "GC=F": "gold",
            "^HSI": "hsi",
            "^N225": "nikkei",
        }

        for ticker, key in global_keys.items():
            rows = query(
                "SELECT date, close FROM global_daily WHERE ticker=? ORDER BY date DESC LIMIT 2",
                (ticker,)
            )
            if rows:
                snapshot[key] = rows[0]["close"]
                if len(rows) >= 2 and rows[1]["close"] and rows[1]["close"] > 0:
                    change = (rows[0]["close"] - rows[1]["close"]) / rows[1]["close"] * 100
                    snapshot[f"{key}_change"] = round(change, 2)

        # 한국 매크로
        kr_keys = {
            "722Y001": "kr_rate",
            "021Y201": "kr_cpi",
            "008Y007": "kr_unemployment",
        }
        for code, key in kr_keys.items():
            rows = query(
                "SELECT value FROM kr_macro WHERE indicator_code=? ORDER BY date DESC LIMIT 1",
                (code,)
            )
            if rows and rows[0]["value"] is not None:
                snapshot[key] = rows[0]["value"]

        # 미국 매크로
        us_keys = {
            "FEDFUNDS": "fed_rate",
            "T10Y2Y": "t10y2y",
            "CPIAUCSL": "us_cpi",
            "UNRATE": "us_unemployment",
        }
        for series_id, key in us_keys.items():
            rows = query(
                "SELECT value FROM us_macro WHERE series_id=? ORDER BY date DESC LIMIT 1",
                (series_id,)
            )
            if rows and rows[0]["value"] is not None:
                snapshot[key] = rows[0]["value"]

        # 외인 5일 순매수 합계 (향후 연결)
        snapshot["foreign_net_5d"] = None

        return snapshot

    # ── 4. 섹터 매크로 멀티플라이어 ──────────────────────

    def get_sector_macro_multiplier(self) -> dict:
        """
        현재 매크로 환경 기반 섹터별 가중치 반환.
        (v2.1 기능 6번과 연동)

        로직:
        - 달러 약세 → 수출주(반도체, 자동차) 가산
        - 금리 인하 기대 → 바이오, 성장주 가산
        - VIX 상승 → 내수 방어주 가산, 성장주 감산
        - 원유 상승 → 정유화학 가산, 운송 감산
        """
        snapshot = self.get_macro_snapshot()
        multipliers = {
            "반도체": 1.0,
            "자동차": 1.0,
            "바이오": 1.0,
            "내수": 1.0,
            "금융": 1.0,
            "정유화학": 1.0,
            "2차전지": 1.0,
            "IT": 1.0,
        }

        vix = snapshot.get("vix")
        dxy_change = snapshot.get("dxy_change")
        wti = snapshot.get("wti")
        fed_rate = snapshot.get("fed_rate")
        t10y2y = snapshot.get("t10y2y")

        # 달러 약세 → 수출주 가산
        if dxy_change is not None and dxy_change < -0.5:
            multipliers["반도체"] += 0.15
            multipliers["자동차"] += 0.10
            multipliers["2차전지"] += 0.10

        # 달러 강세 → 수출주 감산
        if dxy_change is not None and dxy_change > 0.5:
            multipliers["반도체"] -= 0.10
            multipliers["자동차"] -= 0.10
            multipliers["내수"] += 0.10

        # VIX 상승 → 방어주 가산, 성장주 감산
        if vix is not None:
            if vix >= 25:
                multipliers["내수"] += 0.15
                multipliers["금융"] += 0.10
                multipliers["바이오"] -= 0.15
                multipliers["IT"] -= 0.10
            elif vix <= 15:
                multipliers["바이오"] += 0.10
                multipliers["IT"] += 0.10
                multipliers["2차전지"] += 0.10

        # 원유 방향
        wti_change = snapshot.get("wti_change")
        if wti_change is not None:
            if wti_change > 3:
                multipliers["정유화학"] += 0.15
            elif wti_change < -3:
                multipliers["정유화학"] -= 0.10

        # 금리 스프레드 (경기 침체 신호)
        if t10y2y is not None and t10y2y < 0:
            multipliers["내수"] += 0.10
            multipliers["금융"] -= 0.15
            multipliers["반도체"] -= 0.10

        # 범위 클램프 (0.5 ~ 1.5)
        for sector in multipliers:
            multipliers[sector] = round(max(0.5, min(1.5, multipliers[sector])), 2)

        return multipliers

    # ── 요약 출력 ────────────────────────────────────────

    def summary(self) -> str:
        """전체 상황 요약 문자열 반환."""
        regime = self.get_today_regime()
        snapshot = self.get_macro_snapshot()
        multipliers = self.get_sector_macro_multiplier()

        lines = [
            f"[MacroIntelligence] {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"  Regime: {regime['regime']} (score={regime['score']})",
        ]
        if regime.get("triggers"):
            lines.append(f"  Triggers: {', '.join(regime['triggers'])}")

        lines.append(f"  VIX: {snapshot.get('vix', 'N/A')}")
        lines.append(f"  USD/KRW: {snapshot.get('usd_krw', 'N/A')}")
        lines.append(f"  S&P500: {snapshot.get('sp500', 'N/A')} ({snapshot.get('sp500_change', 'N/A')}%)")
        lines.append(f"  Fed Rate: {snapshot.get('fed_rate', 'N/A')}")
        lines.append(f"  T10Y2Y: {snapshot.get('t10y2y', 'N/A')}")
        lines.append(f"  Sector Multipliers: {multipliers}")

        return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 60)
    print("  QUANTUM FLOW — MacroIntelligence")
    print("=" * 60)

    mi = MacroIntelligence()
    print(mi.summary())
