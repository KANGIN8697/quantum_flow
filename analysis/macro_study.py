"""
macro_study.py — Track 1: 매크로 레짐 × 종목선정 상관관계 분석
- 어떤 거시 조건에서 기술적 신호 종목이 가장 잘 작동하는가?
- 레짐별 수익률 분포, 최적 필터 기준 탐색
"""
import logging
from typing import Dict
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

logger = logging.getLogger("analysis.macro_study")

OUT_DIR = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════
# 1. 일봉 + 매크로 병합
# ══════════════════════════════════════════════════════════════

def merge_daily_macro(daily_df: pd.DataFrame, macro_df: pd.DataFrame) -> pd.DataFrame:
    """
    일봉(종목별) + 매크로(날짜별) 병합
    - 날짜 기준 left join
    - 레짐, VIX, KOSPI 추세 등 추가
    """
    # 매크로 date 컬럼 확인
    if "date" not in macro_df.columns:
        macro_df = macro_df.reset_index()
        macro_df["date"] = macro_df["date"].astype(str).str.replace("-", "")

    macro_clean = macro_df.copy()
    macro_clean["date"] = macro_clean["date"].astype(str)

    merged = daily_df.merge(macro_clean, on="date", how="left")
    logger.info(f"병합 완료: {len(merged):,}행, 매크로 컬럼: {macro_df.shape[1]-1}개")
    return merged


# ══════════════════════════════════════════════════════════════
# 2. 레짐별 수익률 분석
# ══════════════════════════════════════════════════════════════

def analyze_regime_returns(df: pd.DataFrame,
                           signal_col: str = "entry_signal",
                           fwd_col: str = "fwd_ret5") -> pd.DataFrame:
    """
    레짐별로 신호 종목의 전진수익률 분포 분석
    반환: regime × 통계 DataFrame
    """
    sig = df[df[signal_col] == True].copy()
    if sig.empty or "regime" not in sig.columns:
        logger.warning("신호 없거나 regime 컬럼 없음")
        return pd.DataFrame()

    rows = []
    for regime, grp in sig.groupby("regime"):
        rets = grp[fwd_col].dropna()
        if len(rets) < 10:
            continue
        rows.append({
            "regime":         regime,
            "count":          len(rets),
            "mean_ret":       rets.mean() * 100,
            "median_ret":     rets.median() * 100,
            "win_rate":       (rets > 0).mean() * 100,
            "std":            rets.std() * 100,
            "sharpe_proxy":   rets.mean() / (rets.std() + 1e-9) * np.sqrt(252),
            "p25":            rets.quantile(0.25) * 100,
            "p75":            rets.quantile(0.75) * 100,
            "worst5pct":      rets.quantile(0.05) * 100,
        })

    result = pd.DataFrame(rows)
    logger.info(f"\n레짐별 수익률:\n{result.to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 3. 매크로 지표별 상관관계 분석
# ══════════════════════════════════════════════════════════════

def analyze_macro_correlation(df: pd.DataFrame,
                              fwd_col: str = "fwd_ret5",
                              signal_col: str = "entry_signal") -> pd.DataFrame:
    """
    신호 종목 기준, 각 매크로 지표와 전진수익률의 상관관계 분석
    - Pearson 상관계수
    - t-검정 (유의성)
    - 구간별 수익률 (사분위)
    """
    sig = df[df[signal_col] == True].copy() if signal_col in df.columns else df.copy()

    macro_cols = [
        "VIX", "yf_VIX", "DXY", "yf_DXY", "TNX", "yf_SP500",
        "yf_KOSPI", "yf_KOSDAQ", "yf_USDKRW", "yf_WTI", "yf_GOLD",
        "vix_chg5d", "kospi_ret5d", "kospi_ret20d", "dxy_chg5d",
        "kospi_above_ma20", "dollar_strong", "yield_rising", "regime_score",
    ]
    macro_cols = [c for c in macro_cols if c in sig.columns]

    rows = []
    for col in macro_cols:
        sub = sig[[col, fwd_col]].dropna()
        if len(sub) < 30:
            continue
        corr, pval = stats.pearsonr(sub[col], sub[fwd_col])
        # 사분위별 평균 수익률
        try:
            q = pd.qcut(sub[col], 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
            q_means = sub.groupby(q, observed=False)[fwd_col].mean() * 100
            q1_ret = round(q_means.get("Q1", 0), 2)
            q4_ret = round(q_means.get("Q4", 0), 2)
        except Exception:
            q1_ret, q4_ret = 0.0, 0.0

        rows.append({
            "macro_var":  col,
            "n":          len(sub),
            "pearson_r":  round(corr, 4),
            "p_value":    round(pval, 4),
            "significant": "★" if pval < 0.05 else "",
            "Q1_ret%":    q1_ret,
            "Q4_ret%":    q4_ret,
            "Q4_vs_Q1":   round(q4_ret - q1_ret, 2),
        })

    result = pd.DataFrame(rows).sort_values("pearson_r", key=abs, ascending=False)
    logger.info(f"\n매크로 상관관계 Top10:\n{result.head(10).to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 4. 최적 매크로 필터 탐색
# ══════════════════════════════════════════════════════════════

def find_best_macro_filters(df: pd.DataFrame,
                            signal_col: str = "entry_signal",
                            fwd_col: str = "fwd_ret5") -> pd.DataFrame:
    """
    각 매크로 조건 조합 적용 시 수익률이 개선되는 필터 탐색
    예: VIX < 20 & KOSPI_ma20_위 & DXY 약세
    """
    sig = df[df[signal_col] == True].copy() if signal_col in df.columns else df.copy()
    base_ret = sig[fwd_col].mean() * 100
    base_n   = len(sig)
    logger.info(f"베이스라인: 평균수익률={base_ret:.2f}%, N={base_n}")

    filter_candidates = {}

    # VIX 레벨 필터
    if "yf_VIX" in sig.columns or "VIX" in sig.columns:
        vix = sig.get("VIX", sig.get("yf_VIX"))
        for thr in [15, 18, 20, 22, 25]:
            mask = vix < thr
            filter_candidates[f"VIX<{thr}"] = mask

    # KOSPI MA 필터
    if "kospi_above_ma20" in sig.columns:
        filter_candidates["KOSPI>MA20"] = sig["kospi_above_ma20"] == 1

    # 달러 필터
    if "dollar_strong" in sig.columns:
        filter_candidates["달러약세"] = sig["dollar_strong"] == 0
        filter_candidates["달러강세"] = sig["dollar_strong"] == 1

    # 레짐 필터
    if "regime" in sig.columns:
        filter_candidates["RiskON"]    = sig["regime"] == "risk_on"
        filter_candidates["Neutral+"]  = sig["regime"].isin(["risk_on", "neutral"])

    # KOSPI 추세
    if "kospi_ret5d" in sig.columns:
        for thr in [-0.02, 0, 0.01, 0.02]:
            filter_candidates[f"KOSPI5d>{thr*100:.0f}%"] = sig["kospi_ret5d"] > thr

    rows = []
    for fname, mask in filter_candidates.items():
        filtered = sig[mask][fwd_col].dropna()
        if len(filtered) < 20:
            continue
        t_stat, p_val = stats.ttest_ind(filtered, sig[fwd_col].dropna())
        rows.append({
            "filter":       fname,
            "n":            len(filtered),
            "coverage%":    round(len(filtered) / base_n * 100, 1),
            "mean_ret%":    round(filtered.mean() * 100, 2),
            "vs_base":      round(filtered.mean() * 100 - base_ret, 2),
            "win_rate%":    round((filtered > 0).mean() * 100, 1),
            "t_stat":       round(t_stat, 3),
            "p_value":      round(p_val, 4),
            "significant":  "★" if p_val < 0.05 else "",
        })

    result = pd.DataFrame(rows).sort_values("vs_base", ascending=False)
    logger.info(f"\n매크로 필터 효과:\n{result.to_string(index=False)}")
    return result


# ══════════════════════════════════════════════════════════════
# 5. 섹터 × 레짐 분석
# ══════════════════════════════════════════════════════════════

def analyze_sector_regime(df: pd.DataFrame,
                          fwd_col: str = "fwd_ret5") -> pd.DataFrame:
    """
    KOSPI/KOSDAQ 대표 섹터 종목 그룹별 레짐 영향 분석
    (종목코드 prefix 기반 간략 섹터 구분)
    """
    if "regime" not in df.columns:
        logger.warning("regime 컬럼 없음")
        return pd.DataFrame()

    # 간단 섹터 매핑 (KOSPI 업종 대표종목 prefix)
    sector_map = {
        "삼성전자": ["005930"],
        "SK하이닉스": ["000660"],
        "반도체ETF": ["091160", "229200"],
        "2차전지": ["006400", "373220", "051910"],
        "자동차": ["005380", "012330", "000270"],
        "바이오": ["207940", "068270", "096770"],
        "금융": ["105560", "055550", "086790"],
    }

    rows = []
    for regime, rgrp in df.groupby("regime"):
        ret_all = rgrp[fwd_col].dropna()
        rows.append({
            "sector": "전체",
            "regime": regime,
            "n": len(ret_all),
            "mean_ret%": round(ret_all.mean() * 100, 2),
            "win_rate%": round((ret_all > 0).mean() * 100, 1),
        })
        for sec_name, tickers in sector_map.items():
            sec = rgrp[rgrp["ticker"].isin(tickers)][fwd_col].dropna()
            if len(sec) < 5:
                continue
            rows.append({
                "sector": sec_name,
                "regime": regime,
                "n": len(sec),
                "mean_ret%": round(sec.mean() * 100, 2),
                "win_rate%": round((sec > 0).mean() * 100, 1),
            })

    result = pd.DataFrame(rows)
    pivot = result.pivot_table(
        index="sector", columns="regime",
        values="mean_ret%", aggfunc="mean"
    )
    logger.info(f"\n섹터×레짐 수익률:\n{pivot.to_string()}")
    return result


# ══════════════════════════════════════════════════════════════
# 6. 전체 분석 실행 (통합)
# ══════════════════════════════════════════════════════════════

def run_macro_study(daily_df: pd.DataFrame,
                   macro_df: pd.DataFrame,
                   signal_col: str = "entry_signal") -> Dict:
    """
    Track 1 전체 분석 파이프라인
    반환: 분석 결과 dict
    """

    logger.info("=== Track 1: 매크로-종목 상관관계 분석 시작 ===")

    # 병합
    merged = merge_daily_macro(daily_df, macro_df)

    results = {}

    # 레짐별 수익률
    for fwd in [1, 3, 5, 10]:
        col = f"fwd_ret{fwd}"
        if col in merged.columns:
            results[f"regime_returns_{fwd}d"] = analyze_regime_returns(
                merged, signal_col, col
            )

    # 매크로 상관관계
    results["macro_correlation"] = analyze_macro_correlation(merged, "fwd_ret5", signal_col)

    # 최적 필터
    results["best_filters"] = find_best_macro_filters(merged, signal_col, "fwd_ret5")

    # 섹터 분석
    results["sector_regime"] = analyze_sector_regime(merged, "fwd_ret5")

    # CSV 저장
    for name, df_result in results.items():
        if isinstance(df_result, pd.DataFrame) and not df_result.empty:
            path = OUT_DIR / f"{name}.csv"
            df_result.to_csv(path, index=False)
            logger.info(f"저장: {path}")

    return results
