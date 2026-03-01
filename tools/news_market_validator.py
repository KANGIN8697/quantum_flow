# tools/news_market_validator.py — 뉴스 긴급도 vs 미국 시장 상관관계 검증
# 2026-03-01 구현
#
# 목적:
#   거시경제 분석관(Agent 1)의 뉴스 긴급도 시그널이 실제 미국 시장 흐름과
#   일치하는지를 검증한다. 매 시간마다 뉴스 긴급도 + 미국 지수 스냅샷을
#   기록하고, 시간이 지남에 따라 "긴급도 HIGH → 실제 시장 하락" 같은
#   상관관계를 분석한다.
#
# 데이터 흐름:
#   job_hourly_news_scan (뉴스 긴급도)
#        +
#   yfinance (S&P500, NASDAQ, VIX 실시간)
#        ↓
#   MarketSnapshot 기록 (시간별)
#        ↓
#   상관관계 분석 → 일일 검증 리포트 → 텔레그램/파일 출력

import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
except ImportError:
    yf = None

from tools.utils import safe_float

# ── 설정 ──────────────────────────────────────────────────────

# 추적할 글로벌 지수
TRACKED_INDICES = {
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
    "DOW": "^DJI",
    "WTI": "CL=F",
    "GOLD": "GC=F",
    "DXY": "DX-Y.NYB",
    "USDKRW": "USDKRW=X",
}

# 스냅샷 보관 경로
SNAPSHOT_DIR = Path("outputs/news_validation")

# 긴급도 → 숫자 매핑 (상관관계 분석용)
URGENCY_NUM = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


# ══════════════════════════════════════════════════════════════
#  MarketSnapshot — 시간별 시장 스냅샷
# ══════════════════════════════════════════════════════════════

class MarketSnapshot:
    """시간별 뉴스 긴급도 + 시장 지수를 기록하는 구조체"""

    def __init__(self, timestamp: str, urgency: str, trend_narrative: str,
                 urgent_items: list, indices: dict):
        self.timestamp = timestamp            # ISO 형식
        self.urgency = urgency                # NONE/LOW/MEDIUM/HIGH/CRITICAL
        self.urgency_num = URGENCY_NUM.get(urgency, 0)
        self.trend_narrative = trend_narrative
        self.urgent_items = urgent_items      # 상위 긴급 기사
        self.indices = indices                # {"SP500": {"price": x, "change_pct": y}, ...}

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "urgency": self.urgency,
            "urgency_num": self.urgency_num,
            "trend_narrative": self.trend_narrative,
            "urgent_items": self.urgent_items,
            "indices": self.indices,
        }


# ══════════════════════════════════════════════════════════════
#  ValidationBuffer — 스냅샷 타임라인 관리
# ══════════════════════════════════════════════════════════════

class ValidationBuffer:
    """
    시간별 스냅샷을 축적하고, 일일 검증 분석을 수행한다.
    메모리 + 파일 이중 저장 (재시작 복원 가능).
    """

    def __init__(self, max_days: int = 90):
        self._snapshots: list[MarketSnapshot] = []
        self._lock = threading.Lock()
        self._max_seconds = max_days * 86400
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def add_snapshot(self, snapshot: MarketSnapshot):
        """스냅샷 추가 + 파일 저장"""
        with self._lock:
            self._snapshots.append(snapshot)
            self._cleanup_old()
        self._persist_snapshot(snapshot)

    def _cleanup_old(self):
        """max_days 초과 스냅샷 삭제"""
        cutoff = time.time() - self._max_seconds
        self._snapshots = [
            s for s in self._snapshots
            if datetime.fromisoformat(s.timestamp).timestamp() >= cutoff
        ]

    def _persist_snapshot(self, snapshot: MarketSnapshot):
        """일별 JSONL 파일에 스냅샷을 추가 저장"""
        try:
            date_str = snapshot.timestamp[:10]  # YYYY-MM-DD
            filepath = SNAPSHOT_DIR / f"snapshots_{date_str}.jsonl"
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"스냅샷 저장 실패: {e}")

    def get_snapshots(self, hours: int = 24) -> list[MarketSnapshot]:
        """최근 N시간 스냅샷 반환"""
        cutoff_ts = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self._lock:
            return [s for s in self._snapshots if s.timestamp >= cutoff_ts]

    def load_from_file(self, date_str: str = None):
        """파일에서 스냅샷 복원 (서비스 재시작 시)"""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        filepath = SNAPSHOT_DIR / f"snapshots_{date_str}.jsonl"
        if not filepath.exists():
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    data = json.loads(line.strip())
                    snapshot = MarketSnapshot(
                        timestamp=data["timestamp"],
                        urgency=data["urgency"],
                        trend_narrative=data.get("trend_narrative", ""),
                        urgent_items=data.get("urgent_items", []),
                        indices=data.get("indices", {}),
                    )
                    self._snapshots.append(snapshot)
            logger.info(f"스냅샷 복원: {filepath} ({len(self._snapshots)}건)")
        except Exception as e:
            logger.warning(f"스냅샷 복원 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  CorrelationAnalyzer — 뉴스 vs 시장 상관관계 분석
# ══════════════════════════════════════════════════════════════

class CorrelationAnalyzer:
    """
    스냅샷 타임라인에서 뉴스 긴급도와 시장 움직임의 상관관계를 분석한다.

    핵심 분석:
    1) 긴급도 변화 시점 전후 시장 반응 (이벤트 스터디)
    2) 시간대별 긴급도 vs 지수 변동률 방향 일치도
    3) 일일 요약 리포트 생성
    """

    def __init__(self, buffer: ValidationBuffer):
        self._buffer = buffer

    def analyze_event_impact(self, hours_back: int = 24) -> list[dict]:
        """
        긴급도가 변화한 시점을 찾아, 그 전후 시장 변동을 분석한다.
        '긴급도 HIGH 발생 후 1시간 내 S&P 500 -1.2%' 같은 인사이트.

        Returns:
        [
            {
                "event_time": str,
                "urgency_from": str, "urgency_to": str,
                "market_reaction": {"SP500": -1.2, "VIX": +3.5, ...},
                "aligned": bool,  # 예상대로 반응했는지
            },
            ...
        ]
        """
        snapshots = self._buffer.get_snapshots(hours_back)
        if len(snapshots) < 2:
            return []

        events = []
        for i in range(1, len(snapshots)):
            prev = snapshots[i - 1]
            curr = snapshots[i]

            # 긴급도가 변화한 시점만 분석
            if prev.urgency == curr.urgency:
                continue

            # 시장 반응 계산 (현재 스냅샷 vs 이전 스냅샷의 지수 변화)
            market_reaction = {}
            for idx_name in ["SP500", "NASDAQ", "VIX", "DOW"]:
                prev_price = prev.indices.get(idx_name, {}).get("price", 0)
                curr_price = curr.indices.get(idx_name, {}).get("price", 0)
                if prev_price > 0 and curr_price > 0:
                    change_pct = (curr_price - prev_price) / prev_price * 100
                    market_reaction[idx_name] = round(change_pct, 3)

            # 긴급도 상승 → 시장 하락이면 "aligned"
            urgency_increased = (
                URGENCY_NUM.get(curr.urgency, 0) > URGENCY_NUM.get(prev.urgency, 0)
            )
            sp500_dropped = market_reaction.get("SP500", 0) < 0
            vix_spiked = market_reaction.get("VIX", 0) > 0

            if urgency_increased:
                aligned = sp500_dropped or vix_spiked
            else:
                # 긴급도 하락 → 시장 반등이면 aligned
                aligned = market_reaction.get("SP500", 0) > 0

            events.append({
                "event_time": curr.timestamp,
                "urgency_from": prev.urgency,
                "urgency_to": curr.urgency,
                "trend_narrative": curr.trend_narrative,
                "market_reaction": market_reaction,
                "aligned": aligned,
            })

        return events

    def calc_direction_match_rate(self, hours_back: int = 24) -> dict:
        """
        시간대별 긴급도 방향과 시장 방향의 일치율을 계산한다.

        로직:
        - 긴급도 ≥ MEDIUM → 시장 하락(SP500↓ or VIX↑)이면 일치
        - 긴급도 ≤ LOW → 시장 안정(SP500↑ or VIX↓)이면 일치

        Returns:
        {
            "total_points": int,
            "match_count": int,
            "match_rate": float,  # 0.0 ~ 1.0
            "details": [...]
        }
        """
        snapshots = self._buffer.get_snapshots(hours_back)
        if len(snapshots) < 2:
            return {"total_points": 0, "match_count": 0, "match_rate": 0.0, "details": []}

        match_count = 0
        total = 0
        details = []

        for i in range(1, len(snapshots)):
            curr = snapshots[i]
            prev = snapshots[i - 1]

            sp500_now = curr.indices.get("SP500", {}).get("price", 0)
            sp500_prev = prev.indices.get("SP500", {}).get("price", 0)
            vix_now = curr.indices.get("VIX", {}).get("price", 0)
            vix_prev = prev.indices.get("VIX", {}).get("price", 0)

            if sp500_prev <= 0 or sp500_now <= 0:
                continue

            sp500_change = (sp500_now - sp500_prev) / sp500_prev * 100
            vix_change = ((vix_now - vix_prev) / vix_prev * 100) if vix_prev > 0 else 0

            news_bearish = curr.urgency_num >= 2  # MEDIUM 이상
            market_bearish = sp500_change < -0.1 or vix_change > 1.0

            matched = (news_bearish == market_bearish)
            if matched:
                match_count += 1
            total += 1

            details.append({
                "time": curr.timestamp,
                "urgency": curr.urgency,
                "sp500_chg": round(sp500_change, 3),
                "vix_chg": round(vix_change, 3),
                "news_bearish": news_bearish,
                "market_bearish": market_bearish,
                "matched": matched,
            })

        rate = match_count / max(total, 1)
        return {
            "total_points": total,
            "match_count": match_count,
            "match_rate": round(rate, 3),
            "details": details,
        }

    def generate_daily_report(self) -> dict:
        """
        일일 검증 리포트를 생성한다.

        Returns:
        {
            "date": str,
            "summary": str,           # 한줄 요약
            "match_rate_24h": float,   # 24시간 방향 일치율
            "match_rate_12h": float,
            "events": [...],           # 긴급도 변화 이벤트 분석
            "timeline": [...],         # 시간별 타임라인
            "conclusion": str,         # 결론
        }
        """
        events = self.analyze_event_impact(24)
        match_24h = self.calc_direction_match_rate(24)
        match_12h = self.calc_direction_match_rate(12)
        snapshots = self._buffer.get_snapshots(24)

        # 타임라인 구성
        timeline = []
        for s in snapshots:
            sp500 = s.indices.get("SP500", {})
            vix = s.indices.get("VIX", {})
            timeline.append({
                "time": s.timestamp[11:16],  # HH:MM
                "urgency": s.urgency,
                "sp500": sp500.get("price", 0),
                "sp500_chg": sp500.get("change_pct", 0),
                "vix": vix.get("price", 0),
                "narrative": s.trend_narrative[:60] if s.trend_narrative else "",
            })

        # 결론 생성
        rate = match_24h["match_rate"]
        if rate >= 0.8:
            conclusion = f"뉴스 시그널과 시장 흐름 높은 일치 ({rate:.0%}). Agent 1 분석 신뢰도 높음."
        elif rate >= 0.6:
            conclusion = f"뉴스 시그널과 시장 흐름 보통 일치 ({rate:.0%}). 부분적 신뢰 가능."
        elif rate >= 0.4:
            conclusion = f"뉴스 시그널과 시장 흐름 낮은 일치 ({rate:.0%}). 키워드 가중치 조정 필요."
        elif len(snapshots) < 3:
            conclusion = "데이터 부족 — 추가 수집 필요."
        else:
            conclusion = f"뉴스 시그널과 시장 흐름 불일치 ({rate:.0%}). 분석 로직 재검토 필요."

        # 요약
        event_count = len(events)
        aligned_count = sum(1 for e in events if e["aligned"])
        summary = (
            f"24시간 일치율 {rate:.0%} | "
            f"이벤트 {event_count}건 중 {aligned_count}건 일치 | "
            f"스냅샷 {len(snapshots)}개"
        )

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "summary": summary,
            "match_rate_24h": match_24h["match_rate"],
            "match_rate_12h": match_12h["match_rate"],
            "events": events,
            "direction_details": match_24h["details"],
            "timeline": timeline,
            "conclusion": conclusion,
        }


# ══════════════════════════════════════════════════════════════
#  시장 지수 스냅샷 수집 함수
# ══════════════════════════════════════════════════════════════

def fetch_index_snapshot() -> dict:
    """
    현재 미국 시장 주요 지수 가격을 yfinance로 수집한다.
    뉴스 스캔과 동시에 호출되어 스냅샷에 포함됨.

    Returns: {"SP500": {"price": x, "change_pct": y}, ...}
    """
    if yf is None:
        return {}

    indices = {}
    for name, symbol in TRACKED_INDICES.items():
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="2d", interval="1d")
            if df.empty or len(df) < 1:
                indices[name] = {"price": 0, "change_pct": 0}
                continue

            latest = df.iloc[-1]
            close = safe_float(latest["Close"])

            if len(df) >= 2:
                prev = df.iloc[-2]
                prev_close = safe_float(prev["Close"])
                change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0
            else:
                change_pct = 0

            indices[name] = {
                "price": round(close, 2),
                "change_pct": round(change_pct, 3),
                "date": str(df.index[-1].date()),
            }
        except Exception as e:
            logger.debug(f"지수 스냅샷 실패 ({name}): {e}")
            indices[name] = {"price": 0, "change_pct": 0}

    return indices


# ══════════════════════════════════════════════════════════════
#  싱글턴 + 메인 실행 함수
# ══════════════════════════════════════════════════════════════

_validation_buffer: ValidationBuffer | None = None
_correlation_analyzer: CorrelationAnalyzer | None = None
_singleton_lock = threading.Lock()


def get_validation_buffer() -> ValidationBuffer:
    """ValidationBuffer 싱글턴"""
    global _validation_buffer
    with _singleton_lock:
        if _validation_buffer is None:
            _validation_buffer = ValidationBuffer(max_days=7)
            # 오늘자 스냅샷 파일에서 복원 시도
            _validation_buffer.load_from_file()
        return _validation_buffer


def get_correlation_analyzer() -> CorrelationAnalyzer:
    """CorrelationAnalyzer 싱글턴"""
    global _correlation_analyzer
    with _singleton_lock:
        if _correlation_analyzer is None:
            _correlation_analyzer = CorrelationAnalyzer(get_validation_buffer())
        return _correlation_analyzer


def record_hourly_snapshot(urgency: str, trend_narrative: str,
                           urgent_items: list) -> MarketSnapshot:
    """
    시간별 스냅샷을 기록한다.
    main.py의 job_hourly_news_scan() 완료 후 호출됨.

    1) 미국 시장 지수 스냅샷 수집 (yfinance)
    2) 뉴스 긴급도 정보와 합쳐서 스냅샷 생성
    3) ValidationBuffer에 저장

    Returns: 생성된 MarketSnapshot
    """
    indices = fetch_index_snapshot()

    snapshot = MarketSnapshot(
        timestamp=datetime.now().isoformat(),
        urgency=urgency,
        trend_narrative=trend_narrative,
        urgent_items=urgent_items,
        indices=indices,
    )

    buf = get_validation_buffer()
    buf.add_snapshot(snapshot)

    logger.info(
        f"[시장검증] 스냅샷 기록 — 긴급도 {urgency} | "
        f"SP500 {indices.get('SP500', {}).get('price', '?')} | "
        f"VIX {indices.get('VIX', {}).get('price', '?')}"
    )

    return snapshot


def generate_validation_report() -> dict:
    """일일 검증 리포트 생성 (main.py의 일일 리포트 job에서 호출)"""
    analyzer = get_correlation_analyzer()
    return analyzer.generate_daily_report()


def format_validation_report_telegram(report: dict) -> str:
    """검증 리포트를 텔레그램 메시지 형식으로 변환"""
    lines = [
        f"📊 뉴스-시장 검증 리포트 ({report['date']})",
        f"",
        f"일치율: 24h {report['match_rate_24h']:.0%} | 12h {report['match_rate_12h']:.0%}",
        f"",
    ]

    # 이벤트 요약
    events = report.get("events", [])
    if events:
        lines.append(f"⚡ 긴급도 변화 이벤트 ({len(events)}건):")
        for ev in events[:5]:
            marker = "✅" if ev["aligned"] else "❌"
            sp500_chg = ev["market_reaction"].get("SP500", 0)
            lines.append(
                f"  {marker} {ev['urgency_from']}→{ev['urgency_to']} | "
                f"S&P500 {sp500_chg:+.2f}%"
            )
        lines.append("")

    # 결론
    lines.append(f"💡 {report['conclusion']}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  테스트 블록
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  뉴스-시장 상관관계 검증 테스트")
    print("=" * 60)

    # 1) 지수 스냅샷 테스트
    print("\n[1] 미국 시장 지수 스냅샷 수집...")
    indices = fetch_index_snapshot()
    for name, data in indices.items():
        price = data.get("price", 0)
        chg = data.get("change_pct", 0)
        print(f"  {name}: {price:,.2f} ({chg:+.2f}%)")

    # 2) 스냅샷 기록 테스트
    print("\n[2] 스냅샷 기록 테스트...")
    snapshot = record_hourly_snapshot(
        urgency="MEDIUM",
        trend_narrative="전쟁 관련 뉴스 3건 감지",
        urgent_items=[{"title": "미-이란 긴장 고조", "score": 8}],
    )
    print(f"  기록 완료: {snapshot.timestamp}")

    # 3) 리포트 생성 테스트
    print("\n[3] 검증 리포트 생성...")
    report = generate_validation_report()
    print(f"  일치율: {report['match_rate_24h']:.0%}")
    print(f"  결론: {report['conclusion']}")

    # 4) 텔레그램 포맷
    print("\n[4] 텔레그램 메시지 미리보기:")
    msg = format_validation_report_telegram(report)
    print(msg)

    print("\n" + "=" * 60)
    print("  ✅ news_market_validator.py 테스트 완료!")
    print("=" * 60)
