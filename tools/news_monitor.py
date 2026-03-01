# tools/news_monitor.py — 이벤트 드리븐 종목 뉴스 모니터링 시스템
#
# 동작 방식:
#   1. MarketWatcher가 종목 이상 감지 (돈치안 돌파 / 거래량 급증 / 급등) → 트리거
#   2. 해당 종목 뉴스 즉시 수집 (Naver + Google News)
#   3. 악재 키워드 감지 시 → 즉시 텔레그램 CRITICAL 알림 (자동 손절 트리거용)
#   4. 집중 모니터링 모드 진입: 장 마감 or 청산까지 10분마다 재수집
#   5. 수집된 뉴스 + 해당 시점 가격 → 파일 로깅 (추후 상관관계 분석용)

import os
import json
import logging
import asyncio
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("news_monitor")
KST = ZoneInfo("Asia/Seoul")

# ── 악재 키워드 등급 정의 ─────────────────────────────────────
# CRITICAL: 즉시 손절 트리거 (룰 기반, LLM 불필요)
CRITICAL_KEYWORDS = [
    "상장폐지", "거래정지", "감자", "횡령", "배임", "분식회계",
    "파산", "회생절차", "부도", "기업회생", "상장적격성",
    "검찰 수사", "금감원 조사", "증선위", "불공정거래",
    "유상증자", "주식담보", "반기보고서 미제출",
]

# WARNING: 텔레그램 경고 알림 (자동 대응 없음, 인간 판단)
WARNING_KEYWORDS = [
    "실적 쇼크", "어닝 쇼크", "매출 급감", "손실", "적자 전환",
    "대규모 손실", "재무구조 악화", "신용등급 하락",
    "CEO 사임", "대표이사 사임", "경영진 교체",
    "리콜", "제품 결함", "집단소송",
    "계약 해지", "수주 취소", "납품 중단",
    "중국 제재", "미국 제재", "수출 금지",
]

# POSITIVE: 호재 텔레그램 알림 (자동 매수 없음, 참고만)
POSITIVE_KEYWORDS = [
    "수주", "계약 체결", "실적 상향", "어닝 서프라이즈",
    "신제품", "특허", "인수", "합병", "지분 취득",
    "목표가 상향", "매수 추천", "강력매수",
]

# ── 집중 모니터링 설정 ────────────────────────────────────────
INTENSIVE_INTERVAL_MIN = 10    # 집중 모니터링 주기 (분)
INTENSIVE_MAX_HOURS    = 6     # 최대 유지 시간 (시간)
MARKET_CLOSE_TIME      = "15:30"


class NewsMonitor:
    """
    이벤트 드리븐 종목 뉴스 모니터링 시스템.
    MarketWatcher에서 attach() 후 사용.
    """

    def __init__(self):
        self._intensive: Dict[str, dict] = {}  # {code: {"start": datetime, "stock_name": str}}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── 외부 진입점 ──────────────────────────────────────────

    def trigger(self, code: str, stock_name: str, reason: str, price: float):
        """
        MarketWatcher가 이상 감지 시 호출.
        즉시 뉴스 수집 + 집중 모니터링 등록.

        Parameters
        ----------
        code        : 종목코드 (예: "005930")
        stock_name  : 종목명 (예: "삼성전자")
        reason      : 트리거 사유 (예: "돈치안 돌파", "거래량 300%")
        price       : 트리거 시점 현재가
        """
        logger.info(f"[NewsMonitor] 트리거: {stock_name}({code}) — {reason}")

        # 즉시 비동기 수집 (별도 스레드에서 이벤트 루프 실행)
        threading.Thread(
            target=self._sync_trigger,
            args=(code, stock_name, reason, price),
            daemon=True,
        ).start()

    def _sync_trigger(self, code, stock_name, reason, price):
        """스레드에서 실행되는 동기 래퍼"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._async_trigger(code, stock_name, reason, price)
            )
        finally:
            loop.close()

    async def _async_trigger(self, code, stock_name, reason, price):
        """즉시 뉴스 수집 + 집중 모니터링 등록"""
        # 1. 즉시 뉴스 수집
        articles = await collect_stock_news_async(code, stock_name, max_items=15)

        # 2. 분석 및 대응
        result = analyze_news(articles, code, stock_name)

        # 3. 로깅 (주가 + 뉴스 동시 기록)
        log_news_price_event(
            code=code, stock_name=stock_name, price=price,
            reason=reason, articles=articles, analysis=result,
        )

        # 4. 텔레그램 알림
        _notify_news_event(
            code=code, stock_name=stock_name, price=price,
            reason=reason, result=result, articles=articles,
        )

        # 5. 집중 모니터링 등록
        with self._lock:
            if code not in self._intensive:
                self._intensive[code] = {
                    "start":      datetime.now(KST),
                    "stock_name": stock_name,
                    "price_at_trigger": price,
                }
                logger.info(f"  집중 모니터링 등록: {stock_name}({code})")

        # 6. 모니터링 루프 미실행 중이면 시작
        if not self._running:
            self.start_loop()

    def start_loop(self):
        """집중 모니터링 백그라운드 루프 시작"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("[NewsMonitor] 집중 모니터링 루프 시작")

    def stop_loop(self):
        """루프 중지"""
        self._running = False
        logger.info("[NewsMonitor] 집중 모니터링 루프 중지")

    def remove(self, code: str):
        """특정 종목 모니터링 해제 (청산 시 호출)"""
        with self._lock:
            if code in self._intensive:
                del self._intensive[code]
                logger.info(f"[NewsMonitor] 모니터링 해제: {code}")

    # ── 집중 모니터링 루프 ───────────────────────────────────

    def _monitor_loop(self):
        """10분마다 집중 모니터링 종목 뉴스 재수집"""
        while self._running:
            now = datetime.now(KST)
            now_hm = now.strftime("%H:%M")

            # 장 마감 이후면 루프 중지
            if now_hm > MARKET_CLOSE_TIME:
                logger.info("[NewsMonitor] 장 마감 — 집중 모니터링 종료")
                self._running = False
                break

            with self._lock:
                targets = dict(self._intensive)

            expired = []
            for code, info in targets.items():
                elapsed_hours = (now - info["start"]).total_seconds() / 3600
                if elapsed_hours > INTENSIVE_MAX_HOURS:
                    expired.append(code)
                    continue

                # 현재가 조회 후 뉴스 수집
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self._intensive_check(code, info["stock_name"])
                    )
                finally:
                    loop.close()

            # 만료 종목 제거
            with self._lock:
                for code in expired:
                    self._intensive.pop(code, None)
                    logger.info(f"[NewsMonitor] 집중 모니터링 만료: {code}")

            # 루프 중인 종목 없으면 루프 중지
            with self._lock:
                if not self._intensive:
                    self._running = False
                    break

            time.sleep(INTENSIVE_INTERVAL_MIN * 60)

    async def _intensive_check(self, code: str, stock_name: str):
        """집중 모니터링 주기 체크"""
        articles = await collect_stock_news_async(code, stock_name, max_items=10)
        if not articles:
            return

        result = analyze_news(articles, code, stock_name)

        # 가격 조회
        price = _get_current_price(code)

        # 로깅
        log_news_price_event(
            code=code, stock_name=stock_name, price=price,
            reason="집중모니터링_주기체크", articles=articles, analysis=result,
        )

        # CRITICAL만 재알림 (반복 알림 방지)
        if result["level"] == "CRITICAL":
            _notify_news_event(
                code=code, stock_name=stock_name, price=price,
                reason="집중모니터링_CRITICAL재감지", result=result, articles=articles,
            )


# ══════════════════════════════════════════════════════════════
# 뉴스 수집 (비동기)
# ══════════════════════════════════════════════════════════════

async def collect_stock_news_async(
    code: str,
    stock_name: str,
    max_items: int = 15,
) -> List[Dict]:
    """종목 특화 뉴스 비동기 수집 (Naver + Google News RSS)"""
    all_articles = []

    # 네이버 뉴스 검색 (종목명)
    try:
        from data_collector.text.news_collector import fetch_naver_news
        naver = fetch_naver_news(stock_name, max_items=max_items)
        all_articles.extend(naver)
        logger.debug(f"  Naver: {len(naver)}건")
    except Exception as e:
        logger.debug(f"  Naver 실패: {e}")

    # Google News RSS (종목명 + 코드)
    try:
        from data_collector.text.news_collector import fetch_google_news_rss
        google = fetch_google_news_rss(f"{stock_name} 주가", max_items=10)
        all_articles.extend(google)
        logger.debug(f"  Google: {len(google)}건")
    except Exception as e:
        logger.debug(f"  Google 실패: {e}")

    # DART 공시 (당일)
    try:
        from backtest.dart_crawler import crawl_dart_for_date
        today = datetime.now(KST).strftime("%Y-%m-%d")
        dart = crawl_dart_for_date(today, corp_code=code)
        for d in dart[:3]:
            all_articles.append({
                "title":     f"[공시] {d.get('report_nm', '')}",
                "source":    "DART",
                "pubDate":   d.get("rcept_dt", ""),
                "link":      d.get("rcept_no", ""),
            })
        logger.debug(f"  DART: {len(dart)}건")
    except Exception as e:
        logger.debug(f"  DART 실패: {e}")

    # 중복 제거
    seen, unique = set(), []
    for a in all_articles:
        t = a.get("title", "").strip()
        if t and t not in seen:
            seen.add(t)
            unique.append(a)

    unique.sort(key=lambda x: x.get("pubDate", ""), reverse=True)
    return unique[:max_items]


# ══════════════════════════════════════════════════════════════
# 뉴스 분석 (키워드 기반, LLM 불사용)
# ══════════════════════════════════════════════════════════════

def analyze_news(
    articles: List[Dict],
    code: str,
    stock_name: str,
) -> Dict:
    """
    키워드 기반 뉴스 분석.
    LLM 없이 룰 기반으로 빠르게 등급 판정.

    Returns
    -------
    {
        "level": "CRITICAL" | "WARNING" | "POSITIVE" | "NEUTRAL",
        "critical_hits": [...],
        "warning_hits":  [...],
        "positive_hits": [...],
        "article_count": int,
    }
    """
    critical_hits, warning_hits, positive_hits = [], [], []

    for article in articles:
        title = article.get("title", "") + " " + article.get("description", "")

        for kw in CRITICAL_KEYWORDS:
            if kw in title:
                critical_hits.append({"keyword": kw, "title": article.get("title", "")[:60]})
                break

        for kw in WARNING_KEYWORDS:
            if kw in title:
                warning_hits.append({"keyword": kw, "title": article.get("title", "")[:60]})
                break

        for kw in POSITIVE_KEYWORDS:
            if kw in title:
                positive_hits.append({"keyword": kw, "title": article.get("title", "")[:60]})
                break

    if critical_hits:
        level = "CRITICAL"
    elif warning_hits:
        level = "WARNING"
    elif positive_hits:
        level = "POSITIVE"
    else:
        level = "NEUTRAL"

    return {
        "level":         level,
        "critical_hits": critical_hits[:3],
        "warning_hits":  warning_hits[:3],
        "positive_hits": positive_hits[:3],
        "article_count": len(articles),
    }


# ══════════════════════════════════════════════════════════════
# 뉴스+주가 동시 로깅 (추후 상관관계 분석용)
# ══════════════════════════════════════════════════════════════

def log_news_price_event(
    code: str,
    stock_name: str,
    price: float,
    reason: str,
    articles: List[Dict],
    analysis: Dict,
    log_dir: str = "outputs/news_price_log",
):
    """
    뉴스 수집 시점의 가격과 헤드라인을 함께 기록.
    나중에 "뉴스 X 이후 N일 수익률" 상관관계 분석에 활용.

    저장 경로: outputs/news_price_log/YYYYMMDD/{code}.jsonl
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        today = datetime.now(KST).strftime("%Y%m%d")
        daily_dir = os.path.join(log_dir, today)
        os.makedirs(daily_dir, exist_ok=True)

        record = {
            "timestamp":    datetime.now(KST).isoformat(),
            "code":         code,
            "stock_name":   stock_name,
            "price":        price,
            "trigger":      reason,
            "news_level":   analysis["level"],
            "critical_hits": analysis["critical_hits"],
            "warning_hits":  analysis["warning_hits"],
            "positive_hits": analysis["positive_hits"],
            "headline_count": analysis["article_count"],
            "headlines": [a.get("title", "")[:80] for a in articles[:10]],
        }

        fpath = os.path.join(daily_dir, f"{code}.jsonl")
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug(f"[NewsLog] {code} 저장: {fpath}")

    except Exception as e:
        logger.error(f"[NewsLog] 로깅 실패 ({code}): {e}")


# ══════════════════════════════════════════════════════════════
# 텔레그램 알림
# ══════════════════════════════════════════════════════════════

def _notify_news_event(
    code: str,
    stock_name: str,
    price: float,
    reason: str,
    result: Dict,
    articles: List[Dict],
):
    """뉴스 이벤트 텔레그램 알림"""
    level = result["level"]

    # NEUTRAL이면 조용히 로깅만 (알림 없음)
    if level == "NEUTRAL":
        return

    try:
        from tools.notifier_tools import _send

        # 등급별 이모지
        emoji = {"CRITICAL": "🚨", "WARNING": "⚠️", "POSITIVE": "📈"}.get(level, "ℹ️")

        lines = [
            f"{emoji} <b>[종목 뉴스 {level}]</b> {stock_name}({code})",
            f"트리거: {reason}",
            f"현재가: {price:,.0f}원",
        ]

        if result["critical_hits"]:
            lines.append("\n<b>🚨 악재 키워드:</b>")
            for h in result["critical_hits"][:2]:
                lines.append(f"  • [{h['keyword']}] {h['title']}")

        if result["warning_hits"]:
            lines.append("\n<b>⚠️ 경고 키워드:</b>")
            for h in result["warning_hits"][:2]:
                lines.append(f"  • [{h['keyword']}] {h['title']}")

        if result["positive_hits"]:
            lines.append("\n<b>📈 호재 키워드:</b>")
            for h in result["positive_hits"][:2]:
                lines.append(f"  • [{h['keyword']}] {h['title']}")

        # 최신 헤드라인 2개
        if articles:
            lines.append("\n<b>최신 헤드라인:</b>")
            for a in articles[:2]:
                lines.append(f"  • {a.get('title', '')[:60]}")

        if level == "CRITICAL":
            lines.append("\n⚡ <b>자동 손절 검토 필요!</b>")

        _send("\n".join(lines))

    except Exception as e:
        logger.debug(f"텔레그램 알림 실패: {e}")


# ══════════════════════════════════════════════════════════════
# 자동 손절 트리거 (CRITICAL 키워드 감지 시)
# ══════════════════════════════════════════════════════════════

def trigger_emergency_stop(code: str, stock_name: str, keyword: str, dry_run: bool = True):
    """
    CRITICAL 악재 키워드 감지 시 자동 손절 실행.

    Parameters
    ----------
    code        : 종목코드
    stock_name  : 종목명
    keyword     : 감지된 악재 키워드
    dry_run     : True면 실행 없이 로그만 (기본값 True — 안전 우선)
    """
    from shared_state import get_positions, remove_position

    positions = get_positions()
    if code not in positions:
        logger.info(f"[AutoStop] {code}: 보유 포지션 없음 — 스킵")
        return

    pos = positions[code]
    qty = pos.get("quantity", 0)

    if dry_run:
        logger.warning(
            f"[AutoStop][DRY_RUN] {stock_name}({code}): "
            f"악재 키워드 '{keyword}' → 시장가 손절 예정 (qty={qty})"
        )
    else:
        try:
            from tools.order_executor import sell_market
            from tools.trade_logger import log_trade

            result = sell_market(code, qty=0, dry_run=False)
            log_trade(
                "EMERGENCY_STOP", code,
                reason=f"악재 키워드 자동 손절: {keyword}",
                position_pct=pos.get("entry_pct", 0),
            )
            remove_position(code)
            logger.warning(f"[AutoStop] {stock_name}({code}): 자동 손절 실행 완료")

        except Exception as e:
            logger.error(f"[AutoStop] {code} 자동 손절 실패: {e}", exc_info=True)

    # 텔레그램 알림
    try:
        from tools.notifier_tools import _send
        mode = "[DRY_RUN]" if dry_run else "[실행]"
        _send(
            f"🚨 <b>자동 손절 {mode}</b>\n"
            f"{stock_name}({code})\n"
            f"악재 키워드: <b>{keyword}</b>\n"
            f"수량: {qty}주"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# 유틸리티
# ══════════════════════════════════════════════════════════════

def _get_current_price(code: str) -> float:
    """shared_state 또는 KIS API에서 현재가 조회"""
    try:
        from shared_state import get_positions
        pos = get_positions().get(code, {})
        if pos.get("current_price", 0) > 0:
            return float(pos["current_price"])
    except Exception:
        pass

    try:
        from shared_state import get_state
        prices = get_state("ws_prices") or {}
        p = prices.get(code, {})
        if p.get("price", 0) > 0:
            return float(p["price"])
    except Exception:
        pass

    return 0.0


# ── 싱글톤 인스턴스 ──────────────────────────────────────────
_monitor_instance: Optional[NewsMonitor] = None


def get_news_monitor() -> NewsMonitor:
    """NewsMonitor 싱글톤 반환"""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = NewsMonitor()
    return _monitor_instance
