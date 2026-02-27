"""
run_analysis.py — 전체 분석 파이프라인 실행 스크립트
Track 1: 매크로-종목 상관관계 분석
Track 2: 기술적 파라미터 최적화 (수천 회 시뮬레이션)
결과: analysis/results/ 폴더에 CSV + HTML 리포트
"""
import os, sys, logging, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

# 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

# 환경변수 로딩
from dotenv import load_dotenv
for env_path in [
    Path(__file__).parent.parent.parent / ".env",
    Path(__file__).parent.parent / ".env",
]:
    if env_path.exists():
        load_dotenv(env_path)
        break

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"analysis/results/analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    ]
)
logger = logging.getLogger("run_analysis")

from data_prep import load_daily_data, load_macro_data, classify_macro_regime
from tech_backtest import generate_signals, optimize, TechParams, run_backtest, _save_results
from macro_study import run_macro_study

OUT_DIR = Path("analysis/results")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 설정 ──
# 실제 데이터 범위: 2023-09-01 ~ 2026-02-24 (600거래일, 약 2.5년)
# 학습: 2023-09 ~ 2024-12 (약 340거래일, 15개월)
# 검증: 2025-01 ~ 2026-02 (약 280거래일, 14개월)
TRAIN_START = "20230901"
TRAIN_END   = "20250101"
TEST_START  = "20250101"
TEST_END    = "20260224"
N_TRIALS    = 2000         # 파라미터 탐색 횟수 (속도 최적화)


def main():
    logger.info("=" * 60)
    logger.info("QUANTUM FLOW — 분석 파이프라인 시작")
    logger.info(f"학습기간: {TRAIN_START} ~ {TRAIN_END}")
    logger.info(f"검증기간: {TEST_START} ~ {TEST_END}")
    logger.info(f"파라미터 탐색: {N_TRIALS}회")
    logger.info("=" * 60)

    # ── Step 1: 데이터 로딩 ──
    logger.info("\n[Step 1] 데이터 로딩")
    # top_n_tickers=500, min_days=60 (2.5년치 데이터 기준, 메모리/속도 균형)
    daily_df = load_daily_data(start_date=TRAIN_START, top_n_tickers=500, min_days=60)
    macro_df = load_macro_data(start_date=TRAIN_START)
    macro_df = classify_macro_regime(macro_df)

    logger.info(f"일봉: {len(daily_df):,}행, {daily_df['ticker'].nunique()}종목")
    logger.info(f"매크로: {len(macro_df)}일, 레짐분포:\n{macro_df['regime'].value_counts()}")

    # ── Step 2: Track 2 — 기본 파라미터 신호 생성 ──
    logger.info("\n[Step 2] 기본 신호 생성 (dc_period=20, vol=2.0, adx=25)")
    base_params = TechParams()
    daily_sig = generate_signals(daily_df.copy(), base_params)
    sig_count = daily_sig["entry_signal"].sum()
    logger.info(f"총 신호: {sig_count:,}건 (전체 행 대비 {sig_count/len(daily_sig)*100:.2f}%)")

    # ── Step 3: Track 1 — 매크로 상관관계 분석 ──
    logger.info("\n[Step 3] Track 1: 매크로-종목 상관관계 분석")
    macro_results = run_macro_study(daily_sig, macro_df, signal_col="entry_signal")

    # ── Step 4: Track 2 — 파라미터 최적화 (학습 기간) ──
    logger.info(f"\n[Step 4] Track 2: {N_TRIALS}회 파라미터 최적화 (학습 기간)")
    top_results = optimize(
        daily_sig,
        n_trials=N_TRIALS,
        start_date=TRAIN_START,
        end_date=TRAIN_END,
        mode="random",
        top_k=30,
        out_dir=OUT_DIR,
    )

    if not top_results:
        logger.error("유효한 결과 없음")
        return

    best = top_results[0]
    logger.info(f"\n★ 최적 파라미터 (학습기간):")
    logger.info(f"  DC={best.params.dc_period}, 거래량={best.params.vol_ratio_min}x, "
                f"ADX={best.params.adx_min}, RSI={best.params.rsi_min}~{best.params.rsi_max}")
    logger.info(f"  ATR손절={best.params.atr_stop_mult}x, 트레일={best.params.trail_stop_pct*100:.0f}%, "
                f"익절={best.params.take_profit*100:.0f}%, 타임스탑={best.params.time_stop_days}일")
    logger.info(f"  수익률={best.total_return*100:.1f}%, 샤프={best.sharpe:.2f}, "
                f"MDD={best.mdd*100:.1f}%, 승률={best.win_rate*100:.1f}%, 거래={best.trade_count}")

    # ── Step 5: 상위 5개 검증 기간 테스트 ──
    logger.info(f"\n[Step 5] 검증기간 테스트 (상위 5개 파라미터)")
    val_rows = []
    for rank, r in enumerate(top_results[:5], 1):
        val_sig = generate_signals(daily_df.copy(), r.params)
        val_r = run_backtest(val_sig, r.params, TEST_START, TEST_END)
        logger.info(f"  [{rank}위] 검증: 수익률={val_r.total_return*100:.1f}%, "
                    f"샤프={val_r.sharpe:.2f}, MDD={val_r.mdd*100:.1f}%")
        val_rows.append({
            "rank": rank,
            "train_return%":  round(r.total_return * 100, 2),
            "train_sharpe":   round(r.sharpe, 3),
            "train_mdd%":     round(r.mdd * 100, 2),
            "val_return%":    round(val_r.total_return * 100, 2),
            "val_sharpe":     round(val_r.sharpe, 3),
            "val_mdd%":       round(val_r.mdd * 100, 2),
            "val_trades":     val_r.trade_count,
            "dc_period":      r.params.dc_period,
            "vol_ratio_min":  r.params.vol_ratio_min,
            "adx_min":        r.params.adx_min,
            "atr_stop_mult":  r.params.atr_stop_mult,
            "trail_stop_pct": r.params.trail_stop_pct,
            "take_profit":    r.params.take_profit,
        })

    val_df = pd.DataFrame(val_rows)
    val_df.to_csv(OUT_DIR / "validation_results.csv", index=False)

    # ── Step 6: 통합 리포트 생성 ──
    logger.info("\n[Step 6] HTML 리포트 생성")
    _generate_html_report(best, top_results, val_df, macro_results)

    logger.info("\n" + "=" * 60)
    logger.info("분석 완료! 결과: analysis/results/")
    logger.info("=" * 60)


def _generate_html_report(best, top_results, val_df, macro_results):
    """분석 결과 HTML 리포트"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 상위 파라미터 테이블
    top_rows = ""
    for i, r in enumerate(top_results[:20], 1):
        p = r.params
        top_rows += f"""
        <tr>
            <td>{i}</td>
            <td>{p.dc_period}</td>
            <td>{p.vol_ratio_min}</td>
            <td>{p.adx_min}</td>
            <td>{p.rsi_min}~{p.rsi_max}</td>
            <td>{p.atr_stop_mult}</td>
            <td>{p.trail_stop_pct*100:.0f}%</td>
            <td>{p.take_profit*100:.0f}%</td>
            <td style="color:{'green' if r.total_return>0 else 'red'}">{r.total_return*100:.1f}%</td>
            <td>{r.sharpe:.2f}</td>
            <td style="color:red">{r.mdd*100:.1f}%</td>
            <td>{r.win_rate*100:.1f}%</td>
            <td>{r.trade_count}</td>
        </tr>"""

    # 검증 테이블
    val_rows_html = ""
    for _, row in val_df.iterrows():
        color = "green" if row["val_return%"] > 0 else "red"
        val_rows_html += f"""
        <tr>
            <td>{int(row['rank'])}</td>
            <td>{row['dc_period']}</td>
            <td>{row['vol_ratio_min']}</td>
            <td>{row['adx_min']}</td>
            <td style="color:green">{row['train_return%']}%</td>
            <td>{row['train_sharpe']}</td>
            <td style="color:{'green' if row['val_return%']>0 else 'red'}">{row['val_return%']}%</td>
            <td>{row['val_sharpe']}</td>
            <td style="color:red">{row['val_mdd%']}%</td>
        </tr>"""

    # 매크로 필터 테이블
    macro_filter_html = ""
    if "best_filters" in macro_results and not macro_results["best_filters"].empty:
        for _, row in macro_results["best_filters"].head(10).iterrows():
            sign = "+" if row["vs_base"] > 0 else ""
            macro_filter_html += f"""
            <tr>
                <td>{row['filter']}</td>
                <td>{int(row['n'])}</td>
                <td>{row['coverage%']}%</td>
                <td>{row['mean_ret%']}%</td>
                <td style="color:{'green' if row['vs_base']>0 else 'red'}">{sign}{row['vs_base']}%</td>
                <td>{row['win_rate%']}%</td>
                <td>{row.get('significant','')}</td>
            </tr>"""

    # 매크로 상관관계 테이블
    macro_corr_html = ""
    if "macro_correlation" in macro_results and not macro_results["macro_correlation"].empty:
        for _, row in macro_results["macro_correlation"].head(10).iterrows():
            macro_corr_html += f"""
            <tr>
                <td>{row['macro_var']}</td>
                <td>{int(row['n'])}</td>
                <td>{row['pearson_r']}</td>
                <td>{row['p_value']}</td>
                <td>{row.get('significant','')}</td>
                <td>{row['Q1_ret%']}%</td>
                <td>{row['Q4_ret%']}%</td>
                <td style="color:{'green' if row['Q4_vs_Q1']>0 else 'red'}">{row['Q4_vs_Q1']}%</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>QUANTUM FLOW — 백테스트 분석 리포트</title>
<style>
  body {{ font-family: 'Malgun Gothic', sans-serif; background: #0d1117; color: #e6edf3; margin: 20px; }}
  h1 {{ color: #58a6ff; border-bottom: 2px solid #58a6ff; padding-bottom: 10px; }}
  h2 {{ color: #79c0ff; margin-top: 30px; }}
  h3 {{ color: #adbac7; }}
  .stat-box {{ display: inline-block; background: #161b22; border: 1px solid #30363d;
               border-radius: 8px; padding: 15px 25px; margin: 10px; text-align: center; min-width: 140px; }}
  .stat-val  {{ font-size: 2em; font-weight: bold; }}
  .green {{ color: #3fb950; }}
  .red {{ color: #f85149; }}
  .blue {{ color: #58a6ff; }}
  table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
  th {{ background: #21262d; padding: 10px; text-align: left; border: 1px solid #30363d; }}
  td {{ padding: 8px; border: 1px solid #21262d; font-size: 0.9em; }}
  tr:hover {{ background: #161b22; }}
  .section {{ background: #161b22; border-radius: 8px; padding: 20px; margin: 20px 0;
              border: 1px solid #30363d; }}
  .badge {{ background: #388bfd1a; border: 1px solid #388bfd; border-radius: 4px;
            padding: 2px 8px; font-size: 0.85em; color: #79c0ff; }}
</style>
</head>
<body>
<h1>🚀 QUANTUM FLOW — 백테스트 분석 리포트</h1>
<p>생성일시: {now} | 학습: {TRAIN_START}~{TRAIN_END} | 검증: {TEST_START}~{TEST_END} | 탐색횟수: {N_TRIALS:,}회</p>

<div class="section">
<h2>★ 최적 파라미터 (학습기간 기준)</h2>
<div class="stat-box"><div class="stat-val {'green' if best.total_return>0 else 'red'}">{best.total_return*100:.1f}%</div><div>총 수익률</div></div>
<div class="stat-box"><div class="stat-val blue">{best.sharpe:.2f}</div><div>샤프비율</div></div>
<div class="stat-box"><div class="stat-val red">{best.mdd*100:.1f}%</div><div>최대낙폭</div></div>
<div class="stat-box"><div class="stat-val">{best.win_rate*100:.1f}%</div><div>승률</div></div>
<div class="stat-box"><div class="stat-val">{best.trade_count}</div><div>총 거래수</div></div>
<div class="stat-box"><div class="stat-val">{best.avg_hold_days:.1f}일</div><div>평균 보유</div></div>
<table style="margin-top:20px; max-width:700px">
  <tr><th>파라미터</th><th>최적값</th><th>의미</th></tr>
  <tr><td>돈치안 기간</td><td>{best.params.dc_period}일</td><td>N일 고점 돌파 진입</td></tr>
  <tr><td>거래량 필터</td><td>{best.params.vol_ratio_min}x</td><td>20일 평균 대비 최소 배율</td></tr>
  <tr><td>ADX 최소값</td><td>{best.params.adx_min}</td><td>추세 강도 필터</td></tr>
  <tr><td>RSI 범위</td><td>{best.params.rsi_min}~{best.params.rsi_max}</td><td>과매도/과매수 회피</td></tr>
  <tr><td>ATR 손절 배수</td><td>{best.params.atr_stop_mult}x</td><td>ATR × 배수 = 손절폭</td></tr>
  <tr><td>트레일링 스탑</td><td>{best.params.trail_stop_pct*100:.0f}%</td><td>최고점 대비 하락률</td></tr>
  <tr><td>이익실현</td><td>{best.params.take_profit*100:.0f}%</td><td>고정 익절 기준</td></tr>
  <tr><td>타임스탑</td><td>{best.params.time_stop_days}일</td><td>미진입 시 기계적 매도</td></tr>
</table>
</div>

<div class="section">
<h2>📊 상위 20개 파라미터 조합 (학습기간)</h2>
<table>
  <tr><th>순위</th><th>DC기간</th><th>거래량</th><th>ADX</th><th>RSI범위</th><th>ATR배수</th><th>트레일</th><th>익절</th>
      <th>수익률</th><th>샤프</th><th>MDD</th><th>승률</th><th>거래수</th></tr>
  {top_rows}
</table>
</div>

<div class="section">
<h2>🧪 검증기간 결과 (Forward Test: {TEST_START}~{TEST_END})</h2>
<p>학습기간 상위 5개를 검증기간에 적용한 결과 — 과적합 여부 판단</p>
<table>
  <tr><th>순위</th><th>DC기간</th><th>거래량</th><th>ADX</th>
      <th>학습수익률</th><th>학습샤프</th><th>검증수익률</th><th>검증샤프</th><th>검증MDD</th></tr>
  {val_rows_html}
</table>
</div>

<div class="section">
<h2>🌍 Track 1: 매크로 필터 효과 (vs 기본 신호)</h2>
<p>각 거시경제 조건 적용 시 전진수익률(5일) 개선도 — ★ = 통계적 유의 (p&lt;0.05)</p>
<table>
  <tr><th>필터 조건</th><th>N</th><th>커버리지</th><th>평균수익률</th><th>vs 기본</th><th>승률</th><th>유의성</th></tr>
  {macro_filter_html}
</table>
</div>

<div class="section">
<h2>📈 매크로 지표 × 5일 전진수익률 상관관계</h2>
<p>Q1=하위25%, Q4=상위25% 구간 평균수익률</p>
<table>
  <tr><th>매크로 지표</th><th>N</th><th>피어슨r</th><th>p값</th><th>유의</th><th>Q1수익</th><th>Q4수익</th><th>Q4-Q1</th></tr>
  {macro_corr_html}
</table>
</div>

<div class="section">
<h2>💡 분석 결론 및 권고사항</h2>
<h3>기술적 매매로직</h3>
<ul>
  <li>최적 돈치안 기간: <strong>{best.params.dc_period}일</strong> — 현재 설정({20}일)과 {'동일' if best.params.dc_period==20 else '다름'}</li>
  <li>거래량 필터: <strong>{best.params.vol_ratio_min}x</strong> — 현재 설정(2.0x)과 {'동일' if best.params.vol_ratio_min==2.0 else '다름'}</li>
  <li>ADX 기준: <strong>{best.params.adx_min}</strong> — 현재 설정(25)과 {'동일' if best.params.adx_min==25 else '다름'}</li>
  <li>ATR 손절: <strong>{best.params.atr_stop_mult}x</strong></li>
  <li>트레일링 스탑: <strong>{best.params.trail_stop_pct*100:.0f}%</strong> — 현재 설정(5%)과 {'동일' if best.params.trail_stop_pct==0.05 else '다름'}</li>
</ul>
<h3>매크로 필터 권고</h3>
<p>위 매크로 필터 테이블의 <strong>★ 표시된 조건</strong>을 거시경제 필터로 추가하면 신호 정확도 향상 가능.</p>
</div>

<p style="color:#666; text-align:center; margin-top:40px">
QUANTUM FLOW v2.1 | Generated by Claude | {now}
</p>
</body>
</html>"""

    out_path = OUT_DIR / "analysis_report.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"HTML 리포트 저장: {out_path}")


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
