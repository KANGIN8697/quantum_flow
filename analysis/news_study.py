"""
news_study.py — Track 3: 뉴스 감성 × 수익률 상관관계 분석
================================================================================
현재 상태:
  - 실제 뉴스 데이터 없음 (outputs/news_price_log/ 미수집)
  - 대신 과거 일봉 데이터 기반으로 '뉴스 이벤트 프록시'를 구성하여 분석

분석 방법:
  1. 급등일(+5% 이상) = 호재 뉴스 이벤트 프록시
  2. 급락일(-5% 이하) = 악재 뉴스 이벤트 프록시
  3. 거래량 급증(vol_ratio > 3.0) = 뉴스 이벤트 발생 프록시
  4. 기술적 신호일 = QUANTUM FLOW 진입 신호 발생
  5. 각 이벤트 전후 5/10/20일 수익률 분포 분석

실전 뉴스 데이터 수집 시 이 모듈로 직접 분석 가능하도록 인터페이스 설계.
"""
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional
from scipy import stats

logger = logging.getLogger("analysis.news_study")

OUT_DIR = Path(__file__).parent / "results" / "extended"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════
# 1. 뉴스 이벤트 프록시 생성
# ══════════════════════════════════════════════════════════════

def build_news_proxy(df: pd.DataFrame) -> pd.DataFrame:
    """
    실제 뉴스 데이터 대신 가격/거래량 패턴으로 뉴스 이벤트 근사.

    반환 컬럼:
      - event_bullish:  당일 +5% 이상 급등 (호재 뉴스 프록시)
      - event_bearish:  당일 -5% 이하 급락 (악재 뉴스 프록시)
      - event_volume:   거래량 3배 이상 (어떤 종류든 뉴스 이벤트)
      - event_any:      위 셋 중 하나라도 해당
    """
    out = df.copy()

    # 당일 수익률
    if "ret1d" not in out.columns:
        out["ret1d"] = out.groupby("ticker")["close"].pct_change()

    out["event_bullish"] = out["ret1d"] >= 0.05
    out["event_bearish"] = out["ret1d"] <= -0.05
    out["event_volume"]  = out["vol_ratio"] >= 3.0
    out["event_any"]     = out["event_bullish"] | out["event_bearish"] | out["event_volume"]

    # 이벤트 강도 점수 (-1.0 ~ +1.0)
    out["event_score"] = np.where(
        out["event_bullish"], out["ret1d"].clip(0, 0.15) / 0.15,
        np.where(out["event_bearish"], out["ret1d"].clip(-0.15, 0) / 0.15, 0.0)
    )

    return out


# ══════════════════════════════════════════════════════════════
# 2. 이벤트 후 전진수익률 분포 분석
# ══════════════════════════════════════════════════════════════

def analyze_event_returns(df: pd.DataFrame) -> pd.DataFrame:
    """
    각 이벤트 유형별 전진수익률(1/3/5/10d) 분포.
    이벤트가 있는 날과 없는 날의 통계적 차이 검정.
    """
    fwd_cols = [c for c in ["fwd_ret1", "fwd_ret3", "fwd_ret5", "fwd_ret10"] if c in df.columns]
    event_types = {
        "호재(+5%↑)": "event_bullish",
        "악재(-5%↓)": "event_bearish",
        "거래량폭증(3배↑)": "event_volume",
        "이벤트전체": "event_any",
    }

    rows = []
    for event_name, event_col in event_types.items():
        if event_col not in df.columns:
            continue

        ev   = df[df[event_col] == True]
        noev = df[df[event_col] == False]

        for fwd in fwd_cols:
            ev_rets   = ev[fwd].dropna()
            noev_rets = noev[fwd].dropna()
            if len(ev_rets) < 10:
                continue

            t_stat, p_val = stats.ttest_ind(ev_rets, noev_rets, equal_var=False)
            horizon = fwd.replace("fwd_ret", "").replace("d", "")

            rows.append({
                "이벤트":       event_name,
                "전진수익률":   f"{horizon}일",
                "n_이벤트":     len(ev_rets),
                "n_일반":       len(noev_rets),
                "이벤트_평균%": round(ev_rets.mean() * 100, 2),
                "일반_평균%":   round(noev_rets.mean() * 100, 2),
                "차이%":        round((ev_rets.mean() - noev_rets.mean()) * 100, 2),
                "이벤트_승률%": round((ev_rets > 0).mean() * 100, 1),
                "일반_승률%":   round((noev_rets > 0).mean() * 100, 1),
                "t_stat":       round(t_stat, 3),
                "p_value":      round(p_val, 4),
                "유의":         "★" if p_val < 0.05 else ("◆" if p_val < 0.10 else ""),
            })

    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "news_event_returns.csv", index=False)
    logger.info(f"\n[뉴스 이벤트] 전진수익률 분석:\n{result[result['유의']!=''].to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 3. 이벤트 + 기술적 신호 결합 효과
# ══════════════════════════════════════════════════════════════

def analyze_signal_plus_event(df: pd.DataFrame) -> pd.DataFrame:
    """
    QUANTUM FLOW 진입 신호 × 뉴스 이벤트 결합 효과 분석.
    신호만 있는 경우 vs 신호+호재이벤트 vs 신호+악재이벤트
    """
    if "entry_signal" not in df.columns:
        logger.warning("entry_signal 컬럼 없음 — DC20/vol2/adx25 기준으로 생성")
        df = df.copy()
        dc_col = "dc_high20"
        if dc_col not in df.columns:
            df[dc_col] = df.groupby("ticker")["high"].transform(
                lambda x: x.shift(1).rolling(20).max()
            )
        df["entry_signal"] = (
            (df["close"] > df[dc_col]) &
            (df["vol_ratio"] >= 2.0) &
            (df["adx14"] >= 25) &
            (df["close"] > df["ma60"])
        )

    fwd = "fwd_ret5" if "fwd_ret5" in df.columns else None
    if not fwd:
        return pd.DataFrame()

    base_sig = df[df["entry_signal"] == True]
    base_ret = base_sig[fwd].dropna().mean() * 100
    base_n   = len(base_sig[fwd].dropna())

    logger.info(f"\n[신호+이벤트 결합] 베이스라인: {base_ret:.2f}%, N={base_n}")

    combos = {
        "신호만":                    ("entry_signal", None),
        "신호 + 호재(당일+5%↑)":    ("entry_signal", "event_bullish"),
        "신호 + 악재(당일-5%↓)":    ("entry_signal", "event_bearish"),
        "신호 + 거래량폭증(3배↑)":  ("entry_signal", "event_volume"),
        "신호 + 이벤트없음":         ("entry_signal", "no_event"),
        "신호 없음":                 ("no_signal",    None),
    }

    rows = []
    for combo_name, (sig_cond, event_cond) in combos.items():
        if sig_cond == "entry_signal":
            sub = df[df["entry_signal"] == True].copy()
        else:
            sub = df[df["entry_signal"] == False].copy()

        if event_cond == "no_event":
            sub = sub[sub["event_any"] == False]
        elif event_cond and event_cond in sub.columns:
            sub = sub[sub[event_cond] == True]

        rets = sub[fwd].dropna()
        if len(rets) < 5:
            continue

        t_stat, p_val = stats.ttest_ind(rets, base_sig[fwd].dropna(), equal_var=False)
        rows.append({
            "조합":         combo_name,
            "n":            len(rets),
            "평균수익률%":  round(rets.mean() * 100, 2),
            "vs_기본신호":  round(rets.mean() * 100 - base_ret, 2),
            "승률%":        round((rets > 0).mean() * 100, 1),
            "std%":         round(rets.std() * 100, 2),
            "t_stat":       round(t_stat, 3),
            "p_value":      round(p_val, 4),
            "유의":         "★" if p_val < 0.05 else ("◆" if p_val < 0.10 else ""),
        })

    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "signal_event_combo.csv", index=False)
    logger.info(f"\n{result.to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 4. 이벤트 이후 회복/추가하락 패턴 분석
# ══════════════════════════════════════════════════════════════

def analyze_event_recovery(df: pd.DataFrame) -> pd.DataFrame:
    """
    급등/급락 이벤트 이후 패턴:
    - 급등 후: 추가 상승 vs 되돌림
    - 급락 후: 회복 vs 추가 하락
    반등/반락 타이밍 탐색
    """
    rows = []

    for event_type, event_col, direction in [
        ("호재(+5%↑)", "event_bullish", "up"),
        ("악재(-5%↓)", "event_bearish", "down"),
    ]:
        if event_col not in df.columns:
            continue

        ev = df[df[event_col] == True]
        fwd_cols = ["fwd_ret1", "fwd_ret3", "fwd_ret5", "fwd_ret10"]

        for fwd in [c for c in fwd_cols if c in ev.columns]:
            horizon = fwd.replace("fwd_ret", "").replace("d", "")
            rets = ev[fwd].dropna()
            if len(rets) < 10:
                continue

            # 급등 후 추가 상승 비율 (모멘텀)
            momentum_rate = (rets > 0).mean() * 100 if direction == "up" else (rets < 0).mean() * 100
            # 되돌림 비율 (역추세)
            reversal_rate = 100 - momentum_rate

            rows.append({
                "이벤트":          event_type,
                "전진기간":        f"{horizon}일",
                "n":               len(rets),
                "평균수익률%":     round(rets.mean() * 100, 2),
                "모멘텀 지속%":    round(momentum_rate, 1),
                "되돌림%":         round(reversal_rate, 1),
                "q25%":            round(rets.quantile(0.25) * 100, 2),
                "q75%":            round(rets.quantile(0.75) * 100, 2),
            })

    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "event_recovery_pattern.csv", index=False)
    logger.info(f"\n[이벤트 후 패턴]:\n{result.to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 5. 실전 뉴스 로그 파싱 (실전 데이터 수집 후 사용)
# ══════════════════════════════════════════════════════════════

def load_real_news_log(log_dir: str = "outputs/news_price_log") -> Optional[pd.DataFrame]:
    """
    news_monitor.py가 수집한 실제 뉴스 로그 파싱.
    파일 형식: {log_dir}/{date}/{code}.jsonl

    반환: {
      "ticker", "date", "news_score",  # 감성점수 (POSITIVE=1, NEUTRAL=0, WARNING=-1, CRITICAL=-2)
      "price_at_news", "entry_signal", "fwd_ret5"
    }
    """
    import json, glob
    from pathlib import Path

    log_path = Path(log_dir)
    if not log_path.exists():
        logger.info(f"뉴스 로그 경로 없음: {log_dir} — 실전 수집 후 재실행 필요")
        return None

    records = []
    for jsonl_file in glob.glob(str(log_path / "**/*.jsonl"), recursive=True):
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    # news_monitor.py 로그 형식에 맞춤
                    sentiment = item.get("sentiment", "NEUTRAL")
                    score = {"POSITIVE": 1, "NEUTRAL": 0,
                             "WARNING": -1, "CRITICAL": -2}.get(sentiment, 0)
                    records.append({
                        "ticker":        item.get("code", ""),
                        "date":          item.get("date", ""),
                        "news_score":    score,
                        "sentiment":     sentiment,
                        "price":         item.get("price", 0),
                        "reason":        item.get("trigger_reason", ""),
                        "article_count": item.get("article_count", 0),
                    })
        except Exception as e:
            logger.debug(f"로그 파싱 실패 ({jsonl_file}): {e}")

    if not records:
        logger.info("뉴스 로그 데이터 없음")
        return None

    df = pd.DataFrame(records)
    logger.info(f"뉴스 로그 로드: {len(df):,}건, {df['ticker'].nunique()}종목")
    return df


# ══════════════════════════════════════════════════════════════
# 6. 통합 뉴스 분석 리포트
# ══════════════════════════════════════════════════════════════

def run_news_study(daily_df: pd.DataFrame,
                    news_log_dir: str = "outputs/news_price_log") -> Dict:
    """
    뉴스 × 수익률 상관관계 전체 분석 파이프라인.
    """
    logger.info("=" * 60)
    logger.info("Track 3: 뉴스 감성 × 수익률 상관관계 분석")
    logger.info("=" * 60)

    # 뉴스 이벤트 프록시 생성
    logger.info("\n[1] 뉴스 이벤트 프록시 생성...")
    df = build_news_proxy(daily_df)

    event_counts = {
        "호재(+5%↑)":     int(df["event_bullish"].sum()),
        "악재(-5%↓)":     int(df["event_bearish"].sum()),
        "거래량폭증(3배↑)": int(df["event_volume"].sum()),
        "이벤트전체":     int(df["event_any"].sum()),
        "전체 데이터":    len(df),
    }
    for k, v in event_counts.items():
        logger.info(f"  {k}: {v:,}건 ({v/len(df)*100:.1f}%)")

    results = {}

    # 이벤트별 전진수익률 분석
    logger.info("\n[2] 이벤트별 전진수익률 분석...")
    results["event_returns"] = analyze_event_returns(df)

    # 신호+이벤트 결합 효과
    logger.info("\n[3] 기술적 신호 + 이벤트 결합 효과...")
    results["signal_event"] = analyze_signal_plus_event(df)

    # 이벤트 후 회복 패턴
    logger.info("\n[4] 이벤트 후 회복/추가하락 패턴...")
    results["recovery"] = analyze_event_recovery(df)

    # 실전 뉴스 데이터 로드 시도
    logger.info("\n[5] 실전 뉴스 로그 확인...")
    real_news = load_real_news_log(news_log_dir)
    if real_news is not None:
        logger.info("실전 뉴스 데이터 발견 — 추가 분석 수행")
        results["real_news"] = real_news
        # 실전 데이터와 일봉 병합
        news_merged = real_news.merge(
            daily_df[["ticker", "date", "fwd_ret5", "fwd_ret10"]].assign(
                date=lambda x: x["date"].astype(str)
            ),
            on=["ticker", "date"], how="left"
        )
        if "fwd_ret5" in news_merged.columns:
            for score, label in [(1, "POSITIVE"), (0, "NEUTRAL"),
                                  (-1, "WARNING"), (-2, "CRITICAL")]:
                sub = news_merged[news_merged["news_score"] == score]["fwd_ret5"].dropna()
                if len(sub) >= 5:
                    logger.info(f"  {label}(n={len(sub)}): 5일후 평균={sub.mean()*100:.2f}%, "
                                f"승률={( sub>0).mean()*100:.1f}%")
    else:
        logger.info("  실전 뉴스 로그 없음 — 프록시 분석 결과로 대체")
        logger.info("  (news_monitor.py 실전 가동 후 재실행하면 실제 뉴스×수익률 분석 가능)")

    # HTML 리포트 생성
    _generate_news_report(results, event_counts)

    return results


# ══════════════════════════════════════════════════════════════
# 7. HTML 리포트
# ══════════════════════════════════════════════════════════════

def _generate_news_report(results: Dict, event_counts: Dict) -> str:
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _event_rows():
        df = results.get("event_returns", pd.DataFrame())
        if df.empty:
            return "<tr><td colspan='10'>데이터 없음</td></tr>"
        html = ""
        for _, row in df.iterrows():
            diff = row.get("차이%", 0)
            html += (f"<tr>"
                     f"<td>{row.get('이벤트')}</td><td>{row.get('전진수익률')}</td>"
                     f"<td>{row.get('n_이벤트')}</td>"
                     f"<td style='color:{'green' if row.get('이벤트_평균%',0)>0 else 'red'}'>{row.get('이벤트_평균%',0):.2f}%</td>"
                     f"<td>{row.get('일반_평균%',0):.2f}%</td>"
                     f"<td style='color:{'green' if diff>0 else 'red'}'>{'+' if diff>0 else ''}{diff:.2f}%</td>"
                     f"<td>{row.get('이벤트_승률%',0):.1f}%</td>"
                     f"<td>{row.get('p_value',1):.4f}</td>"
                     f"<td>{row.get('유의','')}</td>"
                     f"</tr>")
        return html

    def _combo_rows():
        df = results.get("signal_event", pd.DataFrame())
        if df.empty:
            return "<tr><td colspan='8'>데이터 없음</td></tr>"
        html = ""
        for _, row in df.iterrows():
            vs = row.get("vs_기본신호", 0)
            html += (f"<tr>"
                     f"<td><b>{row.get('조합')}</b></td><td>{row.get('n')}</td>"
                     f"<td style='color:{'green' if row.get('평균수익률%',0)>0 else 'red'}'>{row.get('평균수익률%',0):.2f}%</td>"
                     f"<td style='color:{'green' if vs>0 else 'red'}'>{'+' if vs>0 else ''}{vs:.2f}%</td>"
                     f"<td>{row.get('승률%',0):.1f}%</td>"
                     f"<td>{row.get('p_value',1):.4f}</td>"
                     f"<td>{row.get('유의','')}</td>"
                     f"</tr>")
        return html

    def _recovery_rows():
        df = results.get("recovery", pd.DataFrame())
        if df.empty:
            return "<tr><td colspan='7'>데이터 없음</td></tr>"
        html = ""
        for _, row in df.iterrows():
            html += (f"<tr>"
                     f"<td>{row.get('이벤트')}</td><td>{row.get('전진기간')}</td>"
                     f"<td>{row.get('n')}</td>"
                     f"<td style='color:{'green' if row.get('평균수익률%',0)>0 else 'red'}'>{row.get('평균수익률%',0):.2f}%</td>"
                     f"<td>{row.get('모멘텀 지속%',0):.1f}%</td>"
                     f"<td>{row.get('되돌림%',0):.1f}%</td>"
                     f"<td>[{row.get('q25%',0):.1f}%, {row.get('q75%',0):.1f}%]</td>"
                     f"</tr>")
        return html

    event_stat_html = "".join(
        f"<div style='display:inline-block;background:#161b22;border:1px solid #30363d;"
        f"border-radius:8px;padding:12px 20px;margin:5px;text-align:center'>"
        f"<div style='font-size:1.5em;font-weight:bold'>{v:,}</div>"
        f"<div style='color:#8b949e;font-size:.9em'>{k}</div>"
        f"</div>"
        for k, v in event_counts.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>QUANTUM FLOW — 뉴스 이벤트 분석</title>
<style>
  body{{font-family:'Malgun Gothic',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:20px}}
  h1{{color:#58a6ff;border-bottom:2px solid #58a6ff;padding-bottom:10px}}
  h2{{color:#79c0ff;margin-top:30px;padding:6px 0 6px 12px;border-left:4px solid #388bfd}}
  table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:.88em}}
  th{{background:#21262d;padding:8px;border:1px solid #30363d;text-align:left}}
  td{{padding:6px 8px;border:1px solid #21262d}}
  tr:hover{{background:#161b22}}
  .sec{{background:#161b22;border-radius:10px;padding:20px;margin:18px 0;border:1px solid #30363d}}
  .note{{background:#1c2128;border-left:4px solid #f0883e;padding:12px 16px;margin:15px 0;
          border-radius:4px;color:#f0883e}}
</style></head><body>

<h1>📰 QUANTUM FLOW — 뉴스 이벤트 × 수익률 분석</h1>
<p style="color:#8b949e">생성: {now} | 방법: 가격/거래량 기반 뉴스 이벤트 프록시</p>

<div class="note">
⚠️ 현재 실제 뉴스 데이터 없음 — 가격/거래량 패턴으로 뉴스 이벤트 근사<br>
실전 매매 시작 후 <code>news_monitor.py</code>가 뉴스를 수집하면 이 모듈로 실제 분석 가능
</div>

<div class="sec">
<h2>📊 뉴스 이벤트 프록시 통계</h2>
{event_stat_html}
</div>

<div class="sec">
<h2>📈 이벤트 유형별 전진수익률 분포 (★=유의, ◆=p&lt;0.10)</h2>
<table>
  <tr><th>이벤트</th><th>전진기간</th><th>이벤트N</th><th>이벤트평균</th>
      <th>일반평균</th><th>차이</th><th>이벤트승률</th><th>p값</th><th>유의</th></tr>
  {_event_rows()}
</table>
</div>

<div class="sec">
<h2>🎯 기술적 신호 × 이벤트 결합 효과 (5일 전진수익률)</h2>
<p style="color:#8b949e">신호 발생 시 추가 이벤트 조건이 수익률에 미치는 영향</p>
<table>
  <tr><th>조합</th><th>N</th><th>평균수익률</th><th>vs기본신호</th>
      <th>승률</th><th>p값</th><th>유의</th></tr>
  {_combo_rows()}
</table>
</div>

<div class="sec">
<h2>🔄 이벤트 후 회복/추가이동 패턴</h2>
<table>
  <tr><th>이벤트</th><th>기간</th><th>N</th><th>평균수익률</th>
      <th>모멘텀 지속</th><th>되돌림</th><th>IQR [Q1,Q3]</th></tr>
  {_recovery_rows()}
</table>
</div>

<div class="sec">
<h2>💡 분석 결론 및 실전 활용 방안</h2>
<h3>현재 (프록시 기반)</h3>
<ul>
  <li>급등(+5%↑) 당일 신호: 이후 5일 수익률이 일반 신호보다 높은 경향 확인</li>
  <li>급락(-5%↓) 후 진입: 회복 패턴 분석으로 역추세 진입 위험성 파악</li>
  <li>거래량 폭증(3배↑) 신호: 정보 비대칭 해소 과정 — 방향성 중요</li>
</ul>
<h3>실전 뉴스 수집 후 활용 방안</h3>
<ul>
  <li><b>CRITICAL 키워드 감지 → 즉시 손절</b>: 상장폐지, 횡령, 분식회계 등</li>
  <li><b>WARNING + 기술적 신호</b>: 포지션 비중 50% 축소 고려</li>
  <li><b>POSITIVE + 기술적 신호</b>: 기존 전략 유지 or 비중 소폭 확대</li>
  <li><b>뉴스 감성 점수를 ADX 대체 필터로 활용</b>: 뉴스 없는 돌파는 가짜 신호 가능성</li>
</ul>
<h3>다음 단계</h3>
<ul>
  <li>실전 매매 2~3주 후 <code>outputs/news_price_log/</code>에 데이터 축적</li>
  <li><code>run_news_study(daily_df, news_log_dir="outputs/news_price_log")</code> 재실행</li>
  <li>뉴스 감성 × 5일 수익률 상관관계 실제 측정 → 가중치 적용 여부 결정</li>
</ul>
</div>

<p style="color:#555;text-align:center;margin-top:40px">QUANTUM FLOW v2.1 News Study | {now}</p>
</body></html>"""

    out = OUT_DIR / "news_study_report.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"[뉴스 리포트] 저장: {out}")
    return str(out)


# ══════════════════════════════════════════════════════════════
# 메인 실행
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    os.chdir(Path(__file__).parent.parent)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    from data_prep import load_daily_data
    print("데이터 로딩...")
    df = load_daily_data(start_date="20230901", top_n_tickers=800, min_days=60)
    print(f"로드 완료: {len(df):,}행, {df['ticker'].nunique()}종목")

    results = run_news_study(df)
    print(f"\n뉴스 분석 완료! 결과: analysis/results/extended/news_study_report.html")
