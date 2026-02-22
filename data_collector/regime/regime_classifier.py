# data_collector/regime/regime_classifier.py — 시장 국면(Regime) 자동 분류
#
# 모든 데이터에 regime 태그를 붙이는 핵심 모듈
# 매일 16:00 자동 실행 (장 마감 후)
# 분류: normal / stress / extreme

import os
import sys
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("regime_classifier")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import (
    init_db, upsert_rows, query, query_df, log_collection, get_conn
)

# ══════════════════════════════════════════════════════════════
#  하드코딩 특이 구간 (소급 적용)
# ══════════════════════════════════════════════════════════════

HARDCODED_STRESS = [
    ("2020-02-20", "2020-03-23", "extreme", "코로나 폭락"),
    ("2020-11-01", "2021-01-15", "stress",  "동학개미 유동성 폭등"),
    ("2022-01-01", "2022-10-31", "stress",  "연준 금리인상 약세장"),
    ("2024-12-03", "2025-01-20", "stress",  "탄핵 정국 환율 급등"),
]


# ══════════════════════════════════════════════════════════════
#  선행-후행 관계 (Knowledge Graph 준비용)
# ══════════════════════════════════════════════════════════════

CAUSAL_RELATIONS = [
    {"from": "us_nfp_surprise",     "to": "vix",              "lag": 0,   "direction": "inverse"},
    {"from": "vix_spike",           "to": "usd_krw",          "lag": 0,   "direction": "positive"},
    {"from": "usd_krw_surge",       "to": "foreign_net_sell", "lag": 1,   "direction": "positive"},
    {"from": "foreign_net_sell",    "to": "kospi",            "lag": 0,   "direction": "positive"},
    {"from": "china_pmi_drop",      "to": "copper_price",     "lag": 5,   "direction": "positive"},
    {"from": "copper_drop",         "to": "kr_export",        "lag": 30,  "direction": "positive"},
    {"from": "kr_semiconductor_export", "to": "hynix_foreign","lag": 5,   "direction": "positive"},
    {"from": "fed_rate_hike",       "to": "t10y2y_spread",    "lag": 0,   "direction": "negative"},
    {"from": "t10y2y_inversion",    "to": "kospi_earnings",   "lag": 180, "direction": "negative"},
]


# ══════════════════════════════════════════════════════════════
#  Regime 분류 로직
# ══════════════════════════════════════════════════════════════

def classify_regime(date: str) -> dict:
    """
    특정 날짜의 시장 국면을 분류한다.

    점수 기반 분류:
      - VIX >= 30: +3, >= 20: +1
      - 코스피 일간 등락 |%| >= 3: +3, >= 2: +1
      - 원달러 일간 변동 |%| >= 2: +2, >= 1: +1
      - 외인 순매수 < -5000억: +2, < -2000억: +1
      - 거래대금 30일 평균 대비 3배+: +1
      - S&P500 일간 등락 |%| >= 2: +2

    결과: score >= 7 → extreme, >= 3 → stress, else → normal

    Parameters
    ----------
    date : ISO 날짜 ("2026-02-20")

    Returns
    -------
    dict: {regime, score, vix, kospi_change, usd_krw_change,
           foreign_net, sp500_change, volume_ratio, trigger_reasons}
    """
    score = 0
    triggers = []

    # 1. VIX 조회
    vix = _get_global_value(date, "^VIX")
    if vix is not None:
        if vix >= 30:
            score += 3
            triggers.append(f"VIX극단({vix:.1f})")
        elif vix >= 20:
            score += 1
            triggers.append(f"VIX상승({vix:.1f})")

    # 2. 코스피 일간 등락
    kospi_change = _get_kospi_change(date)
    if kospi_change is not None:
        if abs(kospi_change) >= 3.0:
            score += 3
            triggers.append(f"코스피급변({kospi_change:+.1f}%)")
        elif abs(kospi_change) >= 2.0:
            score += 1
            triggers.append(f"코스피변동({kospi_change:+.1f}%)")

    # 3. 원달러 일간 변동
    usd_krw_change = _get_fx_change(date)
    if usd_krw_change is not None:
        if abs(usd_krw_change) >= 2.0:
            score += 2
            triggers.append(f"환율급변({usd_krw_change:+.1f}%)")
        elif abs(usd_krw_change) >= 1.0:
            score += 1
            triggers.append(f"환율변동({usd_krw_change:+.1f}%)")

    # 4. 외인 순매수 (향후 데이터 소스 연결 시 사용)
    foreign_net = _get_foreign_net(date)
    if foreign_net is not None:
        if foreign_net < -5000:
            score += 2
            triggers.append(f"외인대규모매도({foreign_net:,.0f}억)")
        elif foreign_net < -2000:
            score += 1
            triggers.append(f"외인순매도({foreign_net:,.0f}억)")

    # 5. 거래대금 이상 급증
    volume_ratio = _get_volume_ratio(date)
    if volume_ratio is not None and volume_ratio >= 3.0:
        score += 1
        triggers.append(f"거래량급증({volume_ratio:.1f}x)")

    # 6. S&P500 등락
    sp500_change = _get_sp500_change(date)
    if sp500_change is not None and abs(sp500_change) >= 2.0:
        score += 2
        triggers.append(f"S&P500급변({sp500_change:+.1f}%)")

    # ── 하드코딩 특이 구간 병합 (더 높은 등급 우선)
    hardcoded = _check_hardcoded(date)

    # 최종 분류
    if score >= 7:
        regime = "extreme"
    elif score >= 3:
        regime = "stress"
    else:
        regime = "normal"

    # 하드코딩과 병합 (더 높은 등급 우선)
    if hardcoded:
        hc_regime = hardcoded["regime"]
        regime_rank = {"normal": 0, "stress": 1, "extreme": 2}
        if regime_rank.get(hc_regime, 0) > regime_rank.get(regime, 0):
            regime = hc_regime
            triggers.append(f"하드코딩:{hardcoded['reason']}")

    return {
        "date": date,
        "regime": regime,
        "score": score,
        "vix": vix,
        "kospi_change": kospi_change,
        "usd_krw_change": usd_krw_change,
        "foreign_net": foreign_net,
        "sp500_change": sp500_change,
        "volume_ratio": volume_ratio,
        "trigger_reasons": json.dumps(triggers, ensure_ascii=False),
    }


# ── 데이터 조회 헬퍼 ──────────────────────────────────────

def _get_global_value(date: str, ticker: str) -> float:
    """global_daily 테이블에서 특정 날짜의 close 값 조회."""
    rows = query(
        "SELECT close FROM global_daily WHERE date=? AND ticker=?",
        (date, ticker)
    )
    return rows[0]["close"] if rows else None


def _get_daily_change(date: str, ticker: str) -> float:
    """전일 대비 등락률(%) 계산."""
    rows = query(
        "SELECT close FROM global_daily WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 2",
        (ticker, date)
    )
    if len(rows) < 2 or not rows[1]["close"]:
        return None
    prev = rows[1]["close"]
    curr = rows[0]["close"]
    if prev <= 0:
        return None
    return round((curr - prev) / prev * 100, 2)


def _get_kospi_change(date: str) -> float:
    """코스피 일간 등락률. 코스피 지수는 global_daily에 ^KS11 없으면 None."""
    # KOSPI는 yfinance ^KS11로 수집 가능 (global_collector에 추가 가능)
    change = _get_daily_change(date, "^KS11")
    if change is not None:
        return change
    # 폴백: KRW=X 기반 간접 추정 (없으면 None)
    return None


def _get_fx_change(date: str) -> float:
    """원달러 일간 변동률."""
    return _get_daily_change(date, "KRW=X")


def _get_foreign_net(date: str) -> float:
    """외인 순매수 (억원). 현재 DB에 소스 없으면 None."""
    # TODO: KIS API 또는 별도 소스에서 외인 순매수 데이터 수집 후 연결
    return None


def _get_volume_ratio(date: str) -> float:
    """코스피 거래대금 30일 평균 대비 비율. 현재 None."""
    # TODO: daily_ohlcv 전 종목 합산 또는 별도 지표
    return None


def _get_sp500_change(date: str) -> float:
    """S&P500 일간 등락률."""
    return _get_daily_change(date, "^GSPC")


def _check_hardcoded(date: str) -> dict:
    """하드코딩 특이 구간에 해당하면 반환."""
    for start, end, regime, reason in HARDCODED_STRESS:
        if start <= date <= end:
            return {"regime": regime, "reason": reason}
    return None


# ══════════════════════════════════════════════════════════════
#  Regime 소급 적용 (기존 데이터에 태그 부여)
# ══════════════════════════════════════════════════════════════

def backfill_regimes(start_date: str = "2020-01-01", end_date: str = None):
    """
    과거 날짜별 regime을 일괄 분류하여 market_regime 테이블에 저장.
    또한 ohlcv/daily_ohlcv 테이블의 regime 컬럼도 업데이트.
    """
    init_db()

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # global_daily에서 거래일 목록 추출
    trading_dates = query(
        "SELECT DISTINCT date FROM global_daily WHERE date>=? AND date<=? ORDER BY date",
        (start_date, end_date)
    )

    if not trading_dates:
        logger.warning("backfill 대상 날짜 없음 (global_daily 비어있음)")
        return 0

    rows = []
    for row in trading_dates:
        date = row["date"]
        result = classify_regime(date)
        rows.append(result)

    if rows:
        inserted = upsert_rows("market_regime", rows)
        logger.info(f"Regime backfill 완료: {inserted}건 ({start_date} ~ {end_date})")

        # ohlcv/daily_ohlcv에도 regime 컬럼 업데이트
        _apply_regime_to_ohlcv(rows)

        log_collection("regime", "backfill", rows_added=inserted)
        return inserted

    return 0


def _apply_regime_to_ohlcv(regime_rows: list):
    """분류된 regime을 가격 테이블의 regime 컬럼에 반영."""
    try:
        with get_conn() as conn:
            for row in regime_rows:
                date = row["date"]
                regime = row["regime"]
                # daily_ohlcv
                conn.execute(
                    "UPDATE daily_ohlcv SET regime=? WHERE date=?",
                    (regime, date)
                )
                # 분봉 테이블 (날짜 패턴 매칭)
                for table in ["ohlcv_5m", "ohlcv_15m", "ohlcv_60m"]:
                    conn.execute(
                        f"UPDATE {table} SET regime=? WHERE datetime LIKE ?",
                        (regime, f"{date}%")
                    )
    except Exception as e:
        logger.error(f"regime 적용 오류: {e}")


# ── 일일 자동 실행 ────────────────────────────────────────

def run_daily():
    """매일 16:00 실행: 오늘 날짜의 regime 분류."""
    init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    result = classify_regime(today)

    upsert_rows("market_regime", [result])
    _apply_regime_to_ohlcv([result])

    regime = result["regime"]
    score = result["score"]
    triggers = json.loads(result["trigger_reasons"])

    logger.info(f"오늘 Regime: {regime} (score={score}) — {', '.join(triggers) or '정상'}")
    log_collection("regime", "daily", rows_added=1)

    # 텔레그램 알림 (stress/extreme인 경우)
    if regime != "normal":
        try:
            from tools.notifier_tools import _send
            _send(
                f"<b>[Regime 경보]</b> {today}\n"
                f"국면: <b>{regime.upper()}</b> (점수: {score})\n"
                f"트리거: {', '.join(triggers)}"
            )
        except Exception:
            pass

    return result


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — 시장 국면 분류기")
    print("=" * 55)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="과거 데이터 소급 적용")
    parser.add_argument("--start", type=str, default="2020-01-01", help="소급 시작일")
    parser.add_argument("--date", type=str, help="특정 날짜 분류")
    args = parser.parse_args()

    if args.backfill:
        count = backfill_regimes(start_date=args.start)
        print(f"\n  {count}건 소급 적용 완료")
    elif args.date:
        result = classify_regime(args.date)
        print(f"\n  날짜: {args.date}")
        print(f"  국면: {result['regime']} (점수: {result['score']})")
        triggers = json.loads(result["trigger_reasons"])
        if triggers:
            print(f"  트리거: {', '.join(triggers)}")
    else:
        result = run_daily()
        print(f"\n  오늘: {result['regime']} (점수: {result['score']})")
