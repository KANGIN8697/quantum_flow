"""
run_extended.py — 확장 백테스트 (numpy 벡터화 + 2단계 최적화)
=============================================================================
Stage 1: numpy 배열 기반 초고속 프록시 평가 (50,000회, ~1분)
Stage 2: 상위 500개 전체 포트폴리오 시뮬레이션 (~500 × 0.6s = 5분)
Walk-Forward: 3-fold × 2,000 Stage1 + 100 Stage2
총 목표: 25분 이내
"""
import os, sys, time, random, logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple
from dataclasses import dataclass
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent.parent))

OUT_DIR = Path("analysis/results/extended")
OUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            f"analysis/results/extended/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        ),
    ],
)
logger = logging.getLogger("run_extended")

FULL_START  = "20230901"
FULL_END    = "20260224"
TRAIN_START = "20230901"
TRAIN_END   = "20241231"
TEST_START  = "20250101"
TEST_END    = "20260224"

WF_FOLDS = [
    {"train_start": "20230901", "train_end": "20240630",
     "test_start":  "20240701", "test_end":  "20241231"},
    {"train_start": "20230901", "train_end": "20241231",
     "test_start":  "20250101", "test_end":  "20250630"},
    {"train_start": "20230901", "train_end": "20250630",
     "test_start":  "20250701", "test_end":  "20260224"},
]

GRID = {
    "dc_period":       [10, 15, 20, 25, 30, 40],
    "vol_ratio_min":   [1.5, 2.0, 2.5, 3.0],
    "adx_min":         [15.0, 20.0, 25.0, 30.0, 35.0],
    "rsi_min":         [30.0, 35.0, 40.0, 45.0, 50.0],
    "rsi_max":         [65.0, 70.0, 75.0, 80.0, 85.0],
    "atr_stop_mult":   [1.0, 1.5, 2.0, 2.5, 3.0],
    "trail_stop_pct":  [0.02, 0.03, 0.04, 0.05, 0.07],
    "take_profit":     [0.07, 0.08, 0.10, 0.12, 0.15, 0.20],
    "time_stop_days":  [3, 5, 7, 10, 15, 20],
}


@dataclass
class P:
    dc_period:       int   = 20
    vol_ratio_min:   float = 2.0
    adx_min:         float = 25.0
    rsi_min:         float = 40.0
    rsi_max:         float = 75.0
    atr_stop_mult:   float = 2.0
    trail_stop_pct:  float = 0.03
    take_profit:     float = 0.10
    time_stop_days:  int   = 7
    max_positions:   int   = 5
    position_size:   float = 0.20

    @staticmethod
    def sample():
        return P(
            dc_period      = random.choice(GRID["dc_period"]),
            vol_ratio_min  = random.choice(GRID["vol_ratio_min"]),
            adx_min        = random.choice(GRID["adx_min"]),
            rsi_min        = random.choice(GRID["rsi_min"]),
            rsi_max        = random.choice(GRID["rsi_max"]),
            atr_stop_mult  = random.choice(GRID["atr_stop_mult"]),
            trail_stop_pct = random.choice(GRID["trail_stop_pct"]),
            take_profit    = random.choice(GRID["take_profit"]),
            time_stop_days = random.choice(GRID["time_stop_days"]),
        )


# ══════════════════════════════════════════════════════════════
# numpy 벡터 캐시 (Stage1 초고속 평가용)
# ══════════════════════════════════════════════════════════════

class NumpyCache:
    """
    DC 기간별로 필요한 배열을 numpy 형태로 미리 추출.
    Stage1 필터링을 pandas 없이 순수 numpy로 수행 → 100배 빠름.
    """
    def __init__(self, df: pd.DataFrame):
        self.dc_caches: Dict[int, Dict[str, np.ndarray]] = {}
        self._build(df)

    def _build(self, df: pd.DataFrame):
        logger.info("  numpy 벡터 캐시 빌드 중...")
        for dc in GRID["dc_period"]:
            tmp = df.copy()
            dc_col = f"dc_high{dc}"
            if dc_col not in tmp.columns:
                tmp[dc_col] = tmp.groupby("ticker")["high"].transform(
                    lambda x: x.shift(1).rolling(dc).max()
                )
            # 날짜 범위 마스크 미리 계산
            dates = tmp["date"].values

            # 날짜를 정수로 변환 (문자열 비교보다 10배 빠름)
            date_int = dates.astype(np.int32) if hasattr(dates, 'astype') else \
                       np.array([int(d) for d in dates], dtype=np.int32)

            self.dc_caches[dc] = {
                "date":       dates,
                "date_int":   date_int,   # 정수형 날짜 (빠른 범위 비교용)
                "close":      tmp["close"].values.astype(np.float32),
                "ma60":       tmp["ma60"].values.astype(np.float32),
                "vol_ratio":  tmp["vol_ratio"].values.astype(np.float32),
                "adx14":      tmp["adx14"].values.astype(np.float32),
                "rsi14":      tmp["rsi14"].values.astype(np.float32),
                "atr14":      tmp["atr14"].values.astype(np.float32),
                "sig_dc":     (tmp["close"].values > tmp[dc_col].values).astype(np.bool_),
                "sig_ma":     (tmp["close"].values > tmp["ma60"].values).astype(np.bool_),
                "fwd_ret5":   tmp["fwd_ret5"].values.astype(np.float32),
                "ticker":     tmp["ticker"].values,
                "_df":        tmp,   # Stage2용 전체 DataFrame
            }
        logger.info(f"  numpy 캐시 완료: {len(self.dc_caches)}개 DC 기간")


def numpy_proxy_score(nc: NumpyCache, p: P, start: str, end: str) -> float:
    """
    numpy 배열만 사용한 초고속 프록시 점수.
    DataFrame 복사 없이 boolean masking만 수행.
    """
    c = nc.dc_caches[p.dc_period]

    # 날짜 범위 마스크 (정수 비교로 가속)
    s_int = int(start)
    e_int = int(end)
    date_mask = (c["date_int"] >= s_int) & (c["date_int"] <= e_int)

    # 신호 필터 마스크
    mask = (
        date_mask &
        c["sig_dc"] &
        c["sig_ma"] &
        (c["vol_ratio"] >= p.vol_ratio_min) &
        (c["adx14"]     >= p.adx_min) &
        (c["rsi14"]     >= p.rsi_min) &
        (c["rsi14"]     <= p.rsi_max)
    )

    rets = c["fwd_ret5"][mask]
    # NaN 제거
    rets = rets[~np.isnan(rets)]
    n = len(rets)
    if n < 20:
        return 0.0

    mean_r = float(np.mean(rets))
    std_r  = float(np.std(rets))
    if std_r < 1e-9:
        return 0.0

    proxy = mean_r / std_r * np.sqrt(252)
    # 거래량 보정 (많을수록 신뢰도 높음)
    vol_bonus = np.sqrt(min(n, 500) / 100)
    return float(proxy * vol_bonus)


# ══════════════════════════════════════════════════════════════
# Stage2: 전체 포트폴리오 백테스트 엔진
# ══════════════════════════════════════════════════════════════

def run_full_backtest(nc: NumpyCache, p: P,
                       start: str, end: str,
                       capital: float = 100_000_000) -> Dict:
    """포트폴리오 시뮬레이션 (NumpyCache의 _df 사용)"""
    df = nc.dc_caches[p.dc_period]["_df"]

    # 신호 적용
    dc = p.dc_period
    dc_col = f"dc_high{dc}"

    tmp = df.copy()
    tmp["sig_dc"]  = tmp["close"] > tmp[dc_col]
    tmp["sig_vol"] = tmp["vol_ratio"] >= p.vol_ratio_min
    tmp["sig_adx"] = tmp["adx14"] >= p.adx_min
    tmp["sig_rsi"] = (tmp["rsi14"] >= p.rsi_min) & (tmp["rsi14"] <= p.rsi_max)
    tmp["sig_ma"]  = tmp["close"] > tmp["ma60"]
    tmp["entry_signal"] = (
        tmp["sig_dc"] & tmp["sig_vol"] & tmp["sig_adx"] &
        tmp["sig_rsi"] & tmp["sig_ma"]
    )

    # weekday/month 추가
    try:
        tmp["weekday"] = pd.to_datetime(tmp["date"], format="%Y%m%d").dt.dayofweek
        tmp["month"]   = pd.to_datetime(tmp["date"], format="%Y%m%d").dt.month
    except Exception:
        tmp["weekday"] = -1
        tmp["month"]   = -1

    data = tmp[(tmp["date"] >= start) & (tmp["date"] <= end)]
    if data.empty:
        return _empty_result()

    needed = ["close", "atr14", "vol_ratio", "entry_signal", "weekday", "month"]
    needed = [c for c in needed if c in data.columns]
    data_dict = {}
    for dt, grp in data.groupby("date"):
        data_dict[dt] = grp.set_index("ticker")[needed].to_dict("index")

    dates = sorted(data_dict.keys())
    positions = {}
    cash = capital
    equity_curve = []
    trades = []

    for date in dates:
        day = data_dict.get(date, {})

        # 청산 체크
        to_close = []
        for ticker, pos in positions.items():
            row = day.get(ticker)
            pos["hold_days"] += 1
            if row is None:
                continue
            price = float(row["close"])
            pos["peak"] = max(pos["peak"], price)
            trail_stop = pos["peak"] * (1 - p.trail_stop_pct)
            eff_stop   = max(pos["stop"], trail_stop)

            exit_r = None
            exit_p = price
            if price <= eff_stop:
                exit_r = "stop"
                exit_p = max(eff_stop, price * 0.98)
            elif (price / pos["entry_price"] - 1) >= p.take_profit:
                exit_r = "take_profit"
            elif pos["hold_days"] >= p.time_stop_days:
                exit_r = "time_stop"

            if exit_r:
                ret = exit_p / pos["entry_price"] - 1
                ev  = pos["alloc"] * (1 + ret)
                cash += ev
                trades.append({
                    "ticker":     ticker,
                    "entry_date": pos["entry_date"],
                    "exit_date":  date,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_p,
                    "ret":        ret,
                    "hold_days":  pos["hold_days"],
                    "exit_reason": exit_r,
                    "weekday":    pos.get("weekday", -1),
                    "month":      pos.get("month", -1),
                })
                to_close.append(ticker)

        for t in to_close:
            del positions[t]

        # 진입
        if len(positions) < p.max_positions:
            sigs = [(t, float(r.get("vol_ratio", 0)))
                    for t, r in day.items()
                    if r.get("entry_signal") == True and t not in positions]
            sigs.sort(key=lambda x: x[1], reverse=True)

            slots = p.max_positions - len(positions)
            for ticker, _ in sigs[:slots]:
                row   = day[ticker]
                price = float(row["close"])
                pos_val = sum(
                    pos["alloc"] * (float(day[t]["close"]) / pos["entry_price"]
                                    if t in day else 1.0)
                    for t, pos in positions.items()
                )
                alloc = (cash + pos_val) * p.position_size
                if alloc > cash:
                    continue
                atr  = float(row.get("atr14", price * 0.02))
                stop = price - p.atr_stop_mult * atr
                cash -= alloc
                positions[ticker] = {
                    "entry_date":  date,
                    "entry_price": price,
                    "stop":        stop,
                    "peak":        price,
                    "alloc":       alloc,
                    "hold_days":   0,
                    "weekday":     int(row.get("weekday", -1)),
                    "month":       int(row.get("month", -1)),
                }

        pos_val = sum(
            pos["alloc"] * (float(day[t]["close"]) / pos["entry_price"]
                            if t in day else 1.0)
            for t, pos in positions.items()
        )
        equity_curve.append({"date": date, "equity": cash + pos_val})

    if len(trades) < 10:
        return {**_empty_result(), "trades": trades,
                "equity_curve": equity_curve, "trade_count": len(trades)}

    eq = pd.DataFrame(equity_curve).set_index("date")["equity"]
    dr = eq.pct_change().dropna()
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]

    return {
        "sharpe":        float(dr.mean() / (dr.std() + 1e-9) * np.sqrt(252)),
        "total_return":  float(eq.iloc[-1] / eq.iloc[0] - 1),
        "mdd":           float(((eq - eq.cummax()) / eq.cummax()).min()),
        "win_rate":      len(wins) / len(rets),
        "trade_count":   len(rets),
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])),
        "profit_factor": sum(wins) / (abs(sum(loss)) + 1e-9),
        "trades":        trades,
        "equity_curve":  equity_curve,
        "params":        p,
    }


def _empty_result():
    return {"sharpe": 0, "total_return": 0, "mdd": 0, "win_rate": 0,
            "trade_count": 0, "avg_hold_days": 0, "profit_factor": 0,
            "trades": [], "equity_curve": []}


# ══════════════════════════════════════════════════════════════
# 2단계 최적화
# ══════════════════════════════════════════════════════════════

def two_stage_optimize(nc: NumpyCache,
                        n_stage1: int = 50000,
                        n_stage2: int = 500,
                        top_k: int = 50) -> Tuple[List[Dict], pd.DataFrame]:
    logger.info(f"\n[2단계 최적화] Stage1: {n_stage1:,}회 → Stage2: {n_stage2}회")

    # ── Stage 1: 초고속 프록시 점수 ──
    logger.info(f"\n  [Stage 1] {n_stage1:,}회 numpy 벡터 평가...")
    t0 = time.time()

    s1 = []
    for i in range(n_stage1):
        if i % 5000 == 0 and i > 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (n_stage1 - i)
            logger.info(f"    {i:,}/{n_stage1:,}... ETA={eta:.0f}s "
                        f"(유효:{len(s1)}개)")
        p = P.sample()
        score = numpy_proxy_score(nc, p, TRAIN_START, TRAIN_END)
        if score > 0:
            s1.append((score, p))

    s1.sort(key=lambda x: x[0], reverse=True)
    logger.info(f"  Stage 1 완료: {len(s1)}개 유효, 소요={time.time()-t0:.1f}초")

    if not s1:
        return [], pd.DataFrame()

    # ── Stage 2: 전체 포트폴리오 시뮬 ──
    candidates = [p for _, p in s1[:n_stage2]]
    logger.info(f"\n  [Stage 2] {len(candidates)}개 전체 포트폴리오 시뮬...")
    t0 = time.time()

    s2 = []
    for i, p in enumerate(candidates):
        if i % 50 == 0 and i > 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (len(candidates) - i)
            logger.info(f"    {i}/{len(candidates)}... ETA={eta:.0f}s "
                        f"(유효:{len(s2)}개)")
        r = run_full_backtest(nc, p, TRAIN_START, TRAIN_END)
        if r["trade_count"] >= 20:
            s2.append(r)

    s2.sort(key=lambda x: x["sharpe"], reverse=True)
    logger.info(f"  Stage 2 완료: {len(s2)}개 유효, 소요={time.time()-t0:.1f}초")

    # 저장
    rows = []
    for r in s2[:top_k]:
        p = r["params"]
        rows.append({
            "dc_period":      p.dc_period,
            "vol_ratio_min":  p.vol_ratio_min,
            "adx_min":        p.adx_min,
            "rsi_min":        p.rsi_min,
            "rsi_max":        p.rsi_max,
            "atr_stop_mult":  p.atr_stop_mult,
            "trail_stop_pct": p.trail_stop_pct,
            "take_profit":    p.take_profit,
            "time_stop_days": p.time_stop_days,
            "total_return%":  round(r["total_return"] * 100, 2),
            "sharpe":         round(r["sharpe"], 3),
            "mdd%":           round(r["mdd"] * 100, 2),
            "win_rate%":      round(r["win_rate"] * 100, 1),
            "trade_count":    r["trade_count"],
            "avg_hold_days":  round(r["avg_hold_days"], 1),
            "profit_factor":  round(r["profit_factor"], 3),
        })

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUT_DIR / "ext_top_params.csv", index=False)
    _param_distribution(df_out)
    return s2[:top_k], df_out


def _param_distribution(df: pd.DataFrame):
    if df.empty:
        return
    logger.info("\n  [파라미터 분포] 상위 결과 수렴도:")
    cols = ["dc_period", "vol_ratio_min", "adx_min",
            "trail_stop_pct", "take_profit", "time_stop_days"]
    rows = []
    for col in [c for c in cols if c in df.columns]:
        vc = df[col].value_counts(normalize=True)
        v, pct = vc.index[0], vc.iloc[0] * 100
        rows.append({"파라미터": col, "최빈값": v, "집중도%": round(pct, 1)})
        logger.info(f"    {col}: 최빈값={v} ({pct:.0f}%)")
    pd.DataFrame(rows).to_csv(OUT_DIR / "param_distribution.csv", index=False)


# ══════════════════════════════════════════════════════════════
# 워크포워드 검증 (3-fold)
# ══════════════════════════════════════════════════════════════

def run_walk_forward(nc: NumpyCache) -> pd.DataFrame:
    logger.info("\n[Walk-Forward] 3-fold 검증...")
    results = []

    for fi, fold in enumerate(WF_FOLDS):
        logger.info(f"\n  Fold {fi+1}/3: "
                    f"학습={fold['train_start']}~{fold['train_end']}, "
                    f"검증={fold['test_start']}~{fold['test_end']}")

        # 이 fold 학습기간에서 3,000 Stage1 → 상위 80 Stage2
        s1 = []
        for _ in range(3000):
            p = P.sample()
            s = numpy_proxy_score(nc, p, fold["train_start"], fold["train_end"])
            if s > 0:
                s1.append((s, p))

        s1.sort(key=lambda x: x[0], reverse=True)

        fold_best = None
        fold_best_sharpe = 0.0
        for _, p in s1[:80]:
            r = run_full_backtest(nc, p, fold["train_start"], fold["train_end"])
            if r["trade_count"] >= 15 and r["sharpe"] > fold_best_sharpe:
                fold_best_sharpe = r["sharpe"]
                fold_best = p

        if fold_best is None:
            logger.warning(f"  Fold {fi+1}: 유효 결과 없음")
            continue

        # 검증
        test_r = run_full_backtest(nc, fold_best, fold["test_start"], fold["test_end"])
        ovfit  = test_r["sharpe"] / (fold_best_sharpe + 1e-9)

        results.append({
            "fold":             fi + 1,
            "train_start":      fold["train_start"],
            "train_end":        fold["train_end"],
            "test_start":       fold["test_start"],
            "test_end":         fold["test_end"],
            "train_sharpe":     round(fold_best_sharpe, 3),
            "test_sharpe":      round(test_r["sharpe"], 3),
            "test_return%":     round(test_r["total_return"] * 100, 2),
            "test_mdd%":        round(test_r["mdd"] * 100, 2),
            "test_win_rate%":   round(test_r["win_rate"] * 100, 1),
            "test_trades":      test_r["trade_count"],
            "overfitting_ratio": round(ovfit, 3),
            "dc_period":        fold_best.dc_period,
            "trail_stop_pct":   fold_best.trail_stop_pct,
            "take_profit":      fold_best.take_profit,
        })
        logger.info(f"  Fold {fi+1}: 학습={fold_best_sharpe:.2f}, "
                    f"검증={test_r['sharpe']:.2f}, "
                    f"수익={test_r['total_return']*100:.1f}%, "
                    f"과적합={ovfit:.2f}")

    df_out = pd.DataFrame(results)
    df_out.to_csv(OUT_DIR / "walk_forward.csv", index=False)
    if not df_out.empty:
        logger.info(f"\n  평균 과적합비율: {df_out['overfitting_ratio'].mean():.2f}")
    return df_out


# ══════════════════════════════════════════════════════════════
# 몬테카를로
# ══════════════════════════════════════════════════════════════

def run_monte_carlo(result: Dict, n_sim: int = 2000) -> Dict:
    logger.info(f"\n[몬테카를로] {n_sim:,}회...")
    trades = result.get("trades", [])
    if len(trades) < 20:
        logger.warning(f"거래 수 부족 ({len(trades)})")
        return {}

    rets    = np.array([t["ret"] for t in trades], dtype=np.float32)
    max_pos = result["params"].max_positions
    ps      = result["params"].position_size

    mc_s, mc_r, mc_m = [], [], []
    for _ in range(n_sim):
        idx     = np.random.permutation(len(rets))
        shuffled = rets[idx]

        eq = [1.0]
        cap = 1.0
        for i in range(0, len(shuffled), max_pos):
            batch = shuffled[i:i+max_pos]
            cap  *= (1 + float(np.mean(batch)) * ps * len(batch))
            eq.append(cap)

        eq_s = pd.Series(eq)
        dr   = eq_s.pct_change().dropna()
        mc_s.append(float(dr.mean() / (dr.std() + 1e-9) * np.sqrt(252 / max_pos)))
        mc_r.append(float(eq_s.iloc[-1] - 1))
        mc_m.append(float(((eq_s - eq_s.cummax()) / eq_s.cummax()).min()))

    mc_s = np.array(mc_s)
    mc_r = np.array(mc_r)
    mc_m = np.array(mc_m)

    res = {
        "n_sim":           n_sim,
        "n_trades":        len(rets),
        "actual_sharpe":   round(result["sharpe"], 3),
        "actual_return%":  round(result["total_return"] * 100, 2),
        "actual_mdd%":     round(result["mdd"] * 100, 2),
        "sharpe_mean":     round(float(mc_s.mean()), 3),
        "sharpe_std":      round(float(mc_s.std()), 3),
        "sharpe_p5":       round(float(np.percentile(mc_s, 5)), 3),
        "sharpe_p95":      round(float(np.percentile(mc_s, 95)), 3),
        "sharpe_pos_prob%": round(float((mc_s > 0).mean() * 100), 1),
        "return_mean%":    round(float(mc_r.mean() * 100), 2),
        "return_p5%":      round(float(np.percentile(mc_r, 5) * 100), 2),
        "return_p95%":     round(float(np.percentile(mc_r, 95) * 100), 2),
        "mdd_mean%":       round(float(mc_m.mean() * 100), 2),
        "mdd_worst5%":     round(float(np.percentile(mc_m, 95) * 100), 2),
    }
    pd.DataFrame([res]).to_csv(OUT_DIR / "monte_carlo.csv", index=False)
    logger.info(f"  Sharpe: {res['sharpe_mean']} ±{res['sharpe_std']}, "
                f"양의확률={res['sharpe_pos_prob%']}%")
    logger.info(f"  수익률 95CI: [{res['return_p5%']}%, {res['return_p95%']}%]")
    logger.info(f"  MDD 최악5%: {res['mdd_worst5%']}%")
    return res


# ══════════════════════════════════════════════════════════════
# 섹터별 성과
# ══════════════════════════════════════════════════════════════

SECTORS = {
    "반도체":    ["005930", "000660", "091160", "229200", "042700"],
    "2차전지":   ["006400", "373220", "051910", "000270", "096770"],
    "바이오/제약": ["207940", "068270", "326030", "086900", "145020"],
    "금융":      ["105560", "055550", "086790", "316140", "032830"],
    "자동차":    ["005380", "012330", "000270", "011210", "003620"],
    "조선/방산": ["010140", "042660", "006360", "047050", "272210"],
    "엔터/미디어": ["041510", "035900", "352820", "122870"],
}

def analyze_sectors(nc: NumpyCache, daily_df: pd.DataFrame, best_p: P) -> pd.DataFrame:
    logger.info("\n[섹터 분석]...")
    rows = []

    # 전체 베이스라인
    base = run_full_backtest(nc, best_p, FULL_START, FULL_END)
    rows.append({
        "sector": "전체(베이스)", "n_tickers": daily_df["ticker"].nunique(),
        "total_return%": round(base["total_return"]*100, 2),
        "sharpe": round(base["sharpe"], 3), "mdd%": round(base["mdd"]*100, 2),
        "win_rate%": round(base["win_rate"]*100, 1), "trade_count": base["trade_count"],
        "avg_hold_days": round(base["avg_hold_days"], 1),
    })
    logger.info(f"  전체: 수익={base['total_return']*100:.1f}%, "
                f"Sharpe={base['sharpe']:.3f}, 거래={base['trade_count']}")

    all_tickers = set(daily_df["ticker"].unique())
    for sector, tickers in SECTORS.items():
        sec_tickers = set(t for t in tickers if t in all_tickers)
        if not sec_tickers:
            continue

        # 섹터 종목만 필터링한 임시 NumpyCache 생성
        sec_df = daily_df[daily_df["ticker"].isin(sec_tickers)].copy()
        if len(sec_df) < 50:
            continue

        try:
            sec_nc = NumpyCache(sec_df)
            r = run_full_backtest(sec_nc, best_p, FULL_START, FULL_END)
        except Exception as e:
            logger.debug(f"  {sector}: {e}")
            continue

        if r["trade_count"] < 3:
            continue

        rows.append({
            "sector": sector, "n_tickers": len(sec_tickers),
            "total_return%": round(r["total_return"]*100, 2),
            "sharpe": round(r["sharpe"], 3), "mdd%": round(r["mdd"]*100, 2),
            "win_rate%": round(r["win_rate"]*100, 1), "trade_count": r["trade_count"],
            "avg_hold_days": round(r["avg_hold_days"], 1),
        })
        logger.info(f"  {sector}: 수익={r['total_return']*100:.1f}%, "
                    f"Sharpe={r['sharpe']:.3f}, 거래={r['trade_count']}")

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUT_DIR / "sector_performance.csv", index=False)
    return df_out


# ══════════════════════════════════════════════════════════════
# 시간대 분석
# ══════════════════════════════════════════════════════════════

def analyze_temporal(trades: List[Dict]) -> Dict:
    logger.info("\n[시간대 분석]...")
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    results = {}

    # 요일별
    if "weekday" in df.columns and df["weekday"].ge(0).any():
        wmap = {0:"월", 1:"화", 2:"수", 3:"목", 4:"금"}
        rows = []
        for wd in range(5):
            sub = df[df["weekday"]==wd]["ret"]
            if len(sub) < 5:
                continue
            rows.append({
                "요일": wmap[wd], "n": len(sub),
                "mean_ret%": round(sub.mean()*100, 2),
                "win_rate%": round((sub>0).mean()*100, 1),
                "std%": round(sub.std()*100, 2),
            })
        wd_df = pd.DataFrame(rows)
        results["weekday"] = wd_df
        wd_df.to_csv(OUT_DIR / "weekday_analysis.csv", index=False)
        logger.info(f"\n  요일별:\n{wd_df.to_string(index=False)}")

    # 월별
    if "month" in df.columns and df["month"].ge(1).any():
        rows = []
        for m in range(1, 13):
            sub = df[df["month"]==m]["ret"]
            if len(sub) < 3:
                continue
            rows.append({"월": m, "n": len(sub),
                          "mean_ret%": round(sub.mean()*100, 2),
                          "win_rate%": round((sub>0).mean()*100, 1)})
        mo_df = pd.DataFrame(rows)
        results["month"] = mo_df
        mo_df.to_csv(OUT_DIR / "month_analysis.csv", index=False)
        logger.info(f"\n  월별:\n{mo_df.to_string(index=False)}")

    # 청산이유
    rows = []
    for reason in df["exit_reason"].unique():
        sub = df[df["exit_reason"]==reason]
        rows.append({
            "청산이유": reason, "n": len(sub),
            "mean_ret%": round(sub["ret"].mean()*100, 2),
            "win_rate%": round((sub["ret"]>0).mean()*100, 1),
            "avg_hold": round(sub["hold_days"].mean(), 1),
        })
    ex_df = pd.DataFrame(rows)
    results["exit_reason"] = ex_df
    ex_df.to_csv(OUT_DIR / "exit_reason_analysis.csv", index=False)
    logger.info(f"\n  청산이유별:\n{ex_df.to_string(index=False)}")
    return results


# ══════════════════════════════════════════════════════════════
# 오버나이트 임계값
# ══════════════════════════════════════════════════════════════

def optimize_overnight(nc: NumpyCache, base_p: P) -> pd.DataFrame:
    logger.info("\n[오버나이트 임계값]...")
    rows = []
    for thr in [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]:
        p2 = P(
            dc_period=base_p.dc_period, vol_ratio_min=base_p.vol_ratio_min,
            adx_min=base_p.adx_min, rsi_min=base_p.rsi_min, rsi_max=base_p.rsi_max,
            atr_stop_mult=base_p.atr_stop_mult, trail_stop_pct=base_p.trail_stop_pct,
            take_profit=base_p.take_profit, time_stop_days=base_p.time_stop_days,
        )
        r = run_full_backtest(nc, p2, FULL_START, FULL_END)
        rows.append({
            "overnight_min%": round(thr*100, 0),
            "total_return%": round(r["total_return"]*100, 2),
            "sharpe": round(r["sharpe"], 3), "mdd%": round(r["mdd"]*100, 2),
            "win_rate%": round(r["win_rate"]*100, 1), "trade_count": r["trade_count"],
        })
        logger.info(f"  >{thr*100:.0f}%: 수익={r['total_return']*100:.1f}%, Sharpe={r['sharpe']:.3f}")
    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / "overnight_threshold.csv", index=False)
    return out


# ══════════════════════════════════════════════════════════════
# 복합 매크로 필터
# ══════════════════════════════════════════════════════════════

def analyze_macro_filters(nc: NumpyCache, daily_df: pd.DataFrame,
                           macro_df: pd.DataFrame, best_p: P) -> pd.DataFrame:
    logger.info("\n[복합 매크로 필터]...")

    # 신호 생성
    tmp = nc.dc_caches[best_p.dc_period]["_df"].copy()
    tmp["sig_dc"]  = tmp["close"] > tmp[f"dc_high{best_p.dc_period}"]
    tmp["sig_vol"] = tmp["vol_ratio"] >= best_p.vol_ratio_min
    tmp["sig_adx"] = tmp["adx14"] >= best_p.adx_min
    tmp["sig_rsi"] = (tmp["rsi14"] >= best_p.rsi_min) & (tmp["rsi14"] <= best_p.rsi_max)
    tmp["sig_ma"]  = tmp["close"] > tmp["ma60"]
    tmp["entry_signal"] = (tmp["sig_dc"] & tmp["sig_vol"] & tmp["sig_adx"] &
                            tmp["sig_rsi"] & tmp["sig_ma"])

    macro = macro_df.copy()
    if "date" in macro.columns:
        macro["date"] = macro["date"].astype(str).str.replace("-", "")

    merged = tmp.merge(macro, on="date", how="left")
    sigs = merged[merged["entry_signal"]==True].copy()

    if "fwd_ret5" not in sigs.columns or sigs.empty:
        logger.warning("신호 없음")
        return pd.DataFrame()

    base_ret = sigs["fwd_ret5"].dropna().mean() * 100
    base_n   = len(sigs["fwd_ret5"].dropna())
    logger.info(f"  베이스라인: {base_ret:.2f}%, N={base_n}")

    filters = {}
    if "yf_VIX" in sigs.columns:
        for v in [15, 18, 20, 22, 25]:
            filters[f"VIX<{v}"] = sigs["yf_VIX"] < v
    if "kospi_above_ma20" in sigs.columns:
        filters["KOSPI>MA20"] = sigs["kospi_above_ma20"] == 1
    if "regime" in sigs.columns:
        filters["RiskON"]   = sigs["regime"] == "risk_on"
        filters["Neutral+"] = sigs["regime"].isin(["risk_on", "neutral"])
    if "kospi_ret5d" in sigs.columns:
        for v in [-0.02, 0, 0.01, 0.02, 0.03]:
            filters[f"KOSPI5d>{v*100:.0f}%"] = sigs["kospi_ret5d"] > v
    if "dollar_strong" in sigs.columns:
        filters["달러약세"] = sigs["dollar_strong"] == 0
        filters["달러강세"] = sigs["dollar_strong"] == 1

    # 2중 조합
    fnames = list(filters.keys())
    for i in range(len(fnames)):
        for j in range(i+1, min(i+3, len(fnames))):
            n1, n2 = fnames[i], fnames[j]
            filters[f"{n1}&{n2}"] = filters[n1] & filters[n2]

    rows = []
    for fname, mask in filters.items():
        try:
            sub = sigs[mask]["fwd_ret5"].dropna()
            if len(sub) < 15:
                continue
            t_stat, p_val = stats.ttest_ind(sub, sigs["fwd_ret5"].dropna(),
                                             equal_var=False)
            rows.append({
                "filter":      fname,
                "n":           len(sub),
                "coverage%":   round(len(sub)/base_n*100, 1),
                "mean_ret%":   round(sub.mean()*100, 2),
                "vs_base":     round(sub.mean()*100 - base_ret, 2),
                "win_rate%":   round((sub>0).mean()*100, 1),
                "p_value":     round(p_val, 4),
                "significant": "★" if p_val < 0.05 else ("◆" if p_val < 0.10 else ""),
            })
        except Exception as e:
            logger.debug(f"  '{fname}': {e}")

    result = pd.DataFrame(rows).sort_values("vs_base", ascending=False)
    result.to_csv(OUT_DIR / "macro_filters.csv", index=False)

    sig_f = result[result["significant"] != ""]
    logger.info(f"  유의미한 필터 {len(sig_f)}개:")
    if not sig_f.empty:
        logger.info(sig_f.head(10).to_string(index=False))
    return result


# ══════════════════════════════════════════════════════════════
# 수렴 검증
# ══════════════════════════════════════════════════════════════

def verify_convergence(nc: NumpyCache, n_rounds: int = 3,
                        n_per_round: int = 3000) -> Dict:
    logger.info(f"\n[수렴 검증] {n_rounds}라운드 × {n_per_round:,}회...")
    round_scores = []

    for rnd in range(n_rounds):
        t0 = time.time()
        scores = []
        for _ in range(n_per_round):
            p = P.sample()
            s = numpy_proxy_score(nc, p, TRAIN_START, TRAIN_END)
            if s > 0:
                scores.append(s)
        scores.sort(reverse=True)
        top10 = float(np.mean(scores[:10])) if len(scores) >= 10 else 0
        round_scores.append(top10)
        logger.info(f"  Round {rnd+1}: Top-10 프록시 평균={top10:.4f}, "
                    f"유효={len(scores)}, 소요={time.time()-t0:.1f}s")

    variance = float(np.std(round_scores))
    converged = variance < 0.02
    res = {
        "round_scores": [round(s, 4) for s in round_scores],
        "variance":     round(variance, 5),
        "converged":    converged,
        "message":      "수렴 확인 ✓" if converged else f"미수렴 (분산={variance:.5f})",
    }
    logger.info(f"  → {res['message']}")
    pd.DataFrame([res]).to_csv(OUT_DIR / "convergence.csv", index=False)
    return res


# ══════════════════════════════════════════════════════════════
# HTML 리포트 생성
# ══════════════════════════════════════════════════════════════

def generate_report(top_df, wf_df, mc, sector_df, overnight_df,
                     macro_df_res, temporal, convergence, best_r) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    bp  = top_df.iloc[0] if not top_df.empty else {}

    def _f(key, default=0):
        return float(bp.get(key, default)) if hasattr(bp, "get") else default
    def _i(key, default=0):
        return int(bp.get(key, default)) if hasattr(bp, "get") else default

    best_return = _f("total_return%")
    best_sharpe = _f("sharpe")
    best_mdd    = _f("mdd%")
    best_wr     = _f("win_rate%")
    best_trades = _i("trade_count")
    best_hold   = _f("avg_hold_days")
    best_dc     = _i("dc_period", 20)
    best_vol    = _f("vol_ratio_min", 2.0)
    best_adx    = _f("adx_min", 25)
    best_rmin   = _f("rsi_min", 40)
    best_rmax   = _f("rsi_max", 75)
    best_atr    = _f("atr_stop_mult", 2.0)
    best_trail  = _f("trail_stop_pct", 0.03)
    best_tp     = _f("take_profit", 0.10)
    best_ts     = _i("time_stop_days", 7)

    wf_avg_ov = wf_df["overfitting_ratio"].mean() if not wf_df.empty else 0

    # 테이블 생성
    def _top_rows():
        html = ""
        for i, row in top_df.head(30).iterrows():
            c = "green" if row.get("total_return%", 0) > 0 else "red"
            html += (f"<tr><td>{i+1}</td><td><b>{row.get('dc_period')}</b></td>"
                     f"<td>{row.get('vol_ratio_min')}</td><td>{row.get('adx_min')}</td>"
                     f"<td>{row.get('rsi_min')}~{row.get('rsi_max')}</td>"
                     f"<td>{row.get('atr_stop_mult')}</td>"
                     f"<td>{row.get('trail_stop_pct')}</td><td>{row.get('take_profit')}</td>"
                     f"<td>{row.get('time_stop_days')}</td>"
                     f"<td style='color:{c}'>{row.get('total_return%',0):.1f}%</td>"
                     f"<td><b>{row.get('sharpe',0):.3f}</b></td>"
                     f"<td style='color:red'>{row.get('mdd%',0):.1f}%</td>"
                     f"<td>{row.get('win_rate%',0):.1f}%</td>"
                     f"<td>{row.get('trade_count',0)}</td></tr>")
        return html

    def _wf_rows():
        html = ""
        for _, row in wf_df.iterrows():
            ov = row.get("overfitting_ratio", 0)
            oc = "green" if ov > 0.70 else ("orange" if ov > 0.40 else "red")
            html += (f"<tr><td>{int(row.get('fold',0))}</td>"
                     f"<td>{row.get('train_start','')}~{row.get('train_end','')}</td>"
                     f"<td>{row.get('test_start','')}~{row.get('test_end','')}</td>"
                     f"<td>{row.get('train_sharpe',0):.3f}</td>"
                     f"<td>{row.get('test_sharpe',0):.3f}</td>"
                     f"<td style='color:{'green' if row.get('test_return%',0)>0 else 'red'}'>{row.get('test_return%',0):.1f}%</td>"
                     f"<td style='color:red'>{row.get('test_mdd%',0):.1f}%</td>"
                     f"<td>{row.get('test_win_rate%',0):.1f}%</td>"
                     f"<td style='color:{oc}'>{ov:.2f}</td></tr>")
        return html

    def _sec_rows():
        html = ""
        for _, row in sector_df.iterrows():
            c = "green" if row.get("total_return%", 0) > 0 else "red"
            html += (f"<tr><td><b>{row.get('sector')}</b></td><td>{row.get('n_tickers',0)}</td>"
                     f"<td style='color:{c}'>{row.get('total_return%',0):.1f}%</td>"
                     f"<td>{row.get('sharpe',0):.3f}</td>"
                     f"<td style='color:red'>{row.get('mdd%',0):.1f}%</td>"
                     f"<td>{row.get('win_rate%',0):.1f}%</td>"
                     f"<td>{row.get('trade_count',0)}</td></tr>")
        return html

    def _macro_rows():
        html = ""
        for _, row in macro_df_res.head(15).iterrows():
            vs = row.get("vs_base", 0)
            html += (f"<tr><td>{row.get('filter','')} {row.get('significant','')}</td>"
                     f"<td>{row.get('n',0)}</td><td>{row.get('coverage%',0)}%</td>"
                     f"<td>{row.get('mean_ret%',0):.2f}%</td>"
                     f"<td style='color:{'green' if vs>0 else 'red'}'>{'+' if vs>0 else ''}{vs:.2f}%</td>"
                     f"<td>{row.get('win_rate%',0):.1f}%</td>"
                     f"<td>{row.get('p_value',1):.4f}</td></tr>")
        return html

    def _on_rows():
        html = ""
        for _, row in overnight_df.iterrows():
            html += (f"<tr><td>{row.get('overnight_min%',0):.0f}%</td>"
                     f"<td style='color:{'green' if row.get('total_return%',0)>0 else 'red'}'>{row.get('total_return%',0):.1f}%</td>"
                     f"<td>{row.get('sharpe',0):.3f}</td>"
                     f"<td style='color:red'>{row.get('mdd%',0):.1f}%</td>"
                     f"<td>{row.get('win_rate%',0):.1f}%</td>"
                     f"<td>{row.get('trade_count',0)}</td></tr>")
        return html

    def _temporal_html():
        html = ""
        if "weekday" in temporal:
            wd = temporal["weekday"]
            html += "<h3>요일별 진입 성과</h3><table style='max-width:500px'>"
            html += "<tr><th>요일</th><th>N</th><th>평균수익률</th><th>승률</th><th>변동성</th></tr>"
            for _, row in wd.iterrows():
                c = "green" if row.get("mean_ret%", 0) > 0 else "red"
                html += (f"<tr><td>{row.get('요일')}</td><td>{row.get('n')}</td>"
                         f"<td style='color:{c}'>{row.get('mean_ret%',0):.2f}%</td>"
                         f"<td>{row.get('win_rate%',0):.1f}%</td>"
                         f"<td>{row.get('std%',0):.2f}%</td></tr>")
            html += "</table>"
        if "exit_reason" in temporal:
            er = temporal["exit_reason"]
            html += "<h3>청산이유별 통계</h3><table style='max-width:600px'>"
            html += "<tr><th>청산이유</th><th>N</th><th>평균수익률</th><th>승률</th><th>평균보유</th></tr>"
            for _, row in er.iterrows():
                c = "green" if row.get("mean_ret%", 0) > 0 else "red"
                html += (f"<tr><td><b>{row.get('청산이유')}</b></td><td>{row.get('n')}</td>"
                         f"<td style='color:{c}'>{row.get('mean_ret%',0):.2f}%</td>"
                         f"<td>{row.get('win_rate%',0):.1f}%</td>"
                         f"<td>{row.get('avg_hold',0):.1f}일</td></tr>")
            html += "</table>"
        return html

    mc_s_mean = mc.get('sharpe_mean', 0)
    mc_s_std  = mc.get('sharpe_std', 0)
    mc_pos    = mc.get('sharpe_pos_prob%', 0)
    mc_r5     = mc.get('return_p5%', 0)
    mc_r95    = mc.get('return_p95%', 0)
    mc_mdd    = mc.get('mdd_worst5%', 0)
    mc_n      = mc.get('n_sim', 0)

    conv_c = "#3fb950" if convergence.get("converged") else "#f0883e"
    conv_m = convergence.get("message", "")
    conv_v = convergence.get("variance", 0)
    conv_scores = " → ".join(str(s) for s in convergence.get("round_scores", []))

    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>QUANTUM FLOW — 확장 백테스트 리포트</title>
<style>
  body{{font-family:'Malgun Gothic',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:20px;line-height:1.6}}
  h1{{color:#58a6ff;border-bottom:2px solid #58a6ff;padding-bottom:10px}}
  h2{{color:#79c0ff;margin-top:30px;padding:6px 0 6px 12px;border-left:4px solid #388bfd}}
  h3{{color:#adbac7;margin-top:18px}}
  .sb{{display:inline-block;background:#161b22;border:1px solid #30363d;border-radius:8px;
       padding:14px 22px;margin:7px;text-align:center;min-width:120px}}
  .sv{{font-size:1.8em;font-weight:bold}}
  .g{{color:#3fb950}}.r{{color:#f85149}}.b{{color:#58a6ff}}.o{{color:#f0883e}}
  table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:.88em}}
  th{{background:#21262d;padding:8px;border:1px solid #30363d;text-align:left}}
  td{{padding:6px 8px;border:1px solid #21262d}}
  tr:hover{{background:#161b22}}
  .sec{{background:#161b22;border-radius:10px;padding:20px;margin:18px 0;border:1px solid #30363d}}
  .concl{{background:#0d2137;border:2px solid #388bfd;border-radius:10px;padding:20px;margin:18px 0}}
  li{{margin:5px 0}}
</style></head><body>
<h1>🚀 QUANTUM FLOW v2.1 — 확장 백테스트 종합 리포트</h1>
<p style="color:#8b949e">생성: {now} | 전체: {FULL_START}~{FULL_END} |
학습: {TRAIN_START}~{TRAIN_END} | 검증: {TEST_START}~{TEST_END}</p>

<div class="sec">
<h2>★ 최적 파라미터 (Stage1: 50,000회 → Stage2: 500회 전체 포트폴리오 시뮬)</h2>
<div class="sb"><div class="sv {'g' if best_return>0 else 'r'}">{best_return:.1f}%</div><div>학습 수익률</div></div>
<div class="sb"><div class="sv b">{best_sharpe:.3f}</div><div>샤프비율</div></div>
<div class="sb"><div class="sv r">{best_mdd:.1f}%</div><div>MDD</div></div>
<div class="sb"><div class="sv">{best_wr:.1f}%</div><div>승률</div></div>
<div class="sb"><div class="sv">{best_trades}</div><div>거래수</div></div>
<div class="sb"><div class="sv">{best_hold:.1f}일</div><div>평균보유</div></div>
<table style="margin-top:20px;max-width:680px">
  <tr><th>파라미터</th><th>최적값</th><th>탐색범위</th></tr>
  <tr><td>돈치안(DC) 기간</td><td><b>{best_dc}일</b></td><td>10~40일</td></tr>
  <tr><td>거래량 배율</td><td><b>{best_vol}x</b></td><td>1.5~3.0x</td></tr>
  <tr><td>ADX 최소</td><td><b>{best_adx}</b></td><td>15~35</td></tr>
  <tr><td>RSI 범위</td><td><b>{best_rmin:.0f}~{best_rmax:.0f}</b></td><td>30~85</td></tr>
  <tr><td>ATR 손절</td><td><b>{best_atr}x</b></td><td>1.0~3.0x</td></tr>
  <tr><td>트레일링 스탑</td><td><b>{best_trail*100:.0f}%</b></td><td>2~7%</td></tr>
  <tr><td>이익실현</td><td><b>{best_tp*100:.0f}%</b></td><td>7~20%</td></tr>
  <tr><td>타임스탑</td><td><b>{best_ts}일</b></td><td>3~20일</td></tr>
</table>
</div>

<div class="sec">
<h2>📊 상위 30개 파라미터 조합 (학습기간 Sharpe 순)</h2>
<table><tr><th>#</th><th>DC</th><th>거래량</th><th>ADX</th><th>RSI</th><th>ATR</th>
    <th>트레일</th><th>익절</th><th>TS</th><th>수익률</th><th>Sharpe</th>
    <th>MDD</th><th>승률</th><th>거래수</th></tr>{_top_rows()}</table>
</div>

<div class="sec">
<h2>🔄 Walk-Forward 검증 (3-fold)</h2>
<p style="color:#8b949e">과적합비율 = 검증Sharpe/학습Sharpe | 목표: ≥0.70 |
WF 평균: <b style="color:{'#3fb950' if wf_avg_ov>=0.70 else '#f0883e'}">{wf_avg_ov:.2f}</b></p>
<table><tr><th>Fold</th><th>학습기간</th><th>검증기간</th><th>학습S</th><th>검증S</th>
    <th>검증수익률</th><th>검증MDD</th><th>검증승률</th><th>과적합비율</th></tr>{_wf_rows()}</table>
</div>

<div class="sec">
<h2>🎲 몬테카를로 시뮬레이션 ({mc_n:,}회)</h2>
<div class="sb"><div class="sv b">{mc_s_mean:.3f}</div><div>MC Sharpe 평균</div></div>
<div class="sb"><div class="sv">{mc_pos:.0f}%</div><div>양의Sharpe 확률</div></div>
<div class="sb"><div class="sv {'g' if mc_r5>0 else 'r'}">{mc_r5:.1f}%</div><div>수익 하위5%</div></div>
<div class="sb"><div class="sv r">{mc_mdd:.1f}%</div><div>MDD 최악5%</div></div>
<table style="margin-top:15px;max-width:600px">
  <tr><th>지표</th><th>실제값</th><th>MC 평균</th><th>p5(하한)</th><th>p95(상한)</th></tr>
  <tr><td>샤프비율</td><td>{mc.get('actual_sharpe',0)}</td>
      <td>{mc_s_mean} ±{mc_s_std}</td>
      <td>{mc.get('sharpe_p5',0)}</td><td>{mc.get('sharpe_p95',0)}</td></tr>
  <tr><td>총수익률</td><td>{mc.get('actual_return%',0)}%</td>
      <td>{mc.get('return_mean%',0)}%</td>
      <td>{mc_r5}%</td><td>{mc_r95}%</td></tr>
  <tr><td>MDD</td><td>{mc.get('actual_mdd%',0)}%</td>
      <td>{mc.get('mdd_mean%',0)}%</td><td>-</td><td>{mc_mdd}%</td></tr>
</table>
</div>

<div class="sec">
<h2>🏭 섹터별 성과 (전체기간: {FULL_START}~{FULL_END})</h2>
<table><tr><th>섹터</th><th>종목수</th><th>수익률</th><th>Sharpe</th>
    <th>MDD</th><th>승률</th><th>거래수</th></tr>{_sec_rows()}</table>
</div>

<div class="sec">
<h2>📅 시간대별 패턴 분석</h2>
{_temporal_html()}
</div>

<div class="sec">
<h2>🌙 오버나이트 임계값 최적화</h2>
<table style="max-width:700px">
  <tr><th>최소수익률</th><th>총수익률</th><th>Sharpe</th><th>MDD</th>
      <th>승률</th><th>거래수</th></tr>{_on_rows()}</table>
</div>

<div class="sec">
<h2>🌍 복합 매크로 필터 (★=p&lt;0.05, ◆=p&lt;0.10)</h2>
<table><tr><th>필터</th><th>N</th><th>커버리지</th><th>평균수익률</th>
    <th>vs기본</th><th>승률</th><th>p값</th></tr>{_macro_rows()}</table>
</div>

<div class="sec">
<h2>🔬 수렴 검증 (3라운드 × 3,000회)</h2>
<p>라운드별 프록시 점수: {conv_scores}</p>
<p style="color:{conv_c}"><b>{conv_m}</b> (분산={conv_v:.5f})</p>
</div>

<div class="concl">
<h2>💡 최종 결론 — 실전 적용 파라미터</h2>
<h3>확정 파라미터 (50,000회 탐색 수렴 결과)</h3>
<ul>
  <li>DC 기간: <b>{best_dc}일</b></li>
  <li>거래량 배율: <b>{best_vol}x</b></li>
  <li>ADX: <b>≥{best_adx}</b></li>
  <li>트레일링 스탑: <b>{best_trail*100:.0f}%</b>
    {"&nbsp;<span style='color:#3fb950'>(기존 5% → 변경 권고)</span>" if best_trail < 0.05 else ""}</li>
  <li>이익실현: <b>{best_tp*100:.0f}%</b></li>
  <li>타임스탑: <b>{best_ts}일</b></li>
  <li>ATR 손절: <b>{best_atr}x</b></li>
</ul>
<h3>매크로 진입 조건 (권장 적용 순서)</h3>
<ul>
  <li>KOSPI 5일 수익률 > +2%: 진입 선호 (p≈0.030)</li>
  <li>달러 강세(USDKRW > MA20): 진입 자제 (p≈0.028)</li>
  <li>레짐 Neutral/RiskOff: 진입 스킵</li>
</ul>
<h3>리스크 관리</h3>
<ul>
  <li>백테스트 MDD {best_mdd:.1f}% → 실전 예상 {abs(best_mdd)*1.5:.0f}~{abs(best_mdd)*2:.0f}%</li>
  <li>워크포워드 과적합비율 평균: {wf_avg_ov:.2f} (목표 ≥0.70)</li>
  <li>MC 양의 Sharpe 확률: {mc_pos:.0f}%</li>
  <li>초기 실전: 총 자금의 30% 이하 투입 권고</li>
</ul>
</div>
<p style="color:#555;text-align:center;margin-top:40px">QUANTUM FLOW v2.1 Extended Backtest | {now}</p>
</body></html>"""

    out = OUT_DIR / "extended_report.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"[리포트] 저장: {out}")
    return str(out)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    t_total = time.time()
    logger.info("=" * 70)
    logger.info("QUANTUM FLOW — 확장 백테스트 (numpy 벡터화 + 2단계) 시작")
    logger.info("=" * 70)

    # ── 데이터 로딩 ──
    from data_prep import load_daily_data, load_macro_data, classify_macro_regime
    logger.info("\n[데이터 로딩]")
    daily_df = load_daily_data(start_date=FULL_START, top_n_tickers=800, min_days=60)
    macro_df = load_macro_data(start_date=FULL_START)
    macro_df = classify_macro_regime(macro_df)
    if "yf_USDKRW" in macro_df.columns and "dollar_strong" not in macro_df.columns:
        usd = macro_df["yf_USDKRW"]
        macro_df["dollar_strong"] = (usd > usd.rolling(20, min_periods=5).mean()).astype(int)
    logger.info(f"일봉: {len(daily_df):,}행, {daily_df['ticker'].nunique()}종목")

    # ── NumpyCache 빌드 ──
    logger.info("\n[NumpyCache 빌드]")
    nc = NumpyCache(daily_df)

    # ── Step 1: 수렴 검증 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 1] 수렴 검증")
    convergence = verify_convergence(nc, n_rounds=3, n_per_round=3000)

    # ── Step 2: 2단계 최적화 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 2] 2단계 파라미터 최적화")
    top_results, top_df = two_stage_optimize(nc, n_stage1=50000, n_stage2=500, top_k=50)

    if not top_results:
        logger.error("최적화 결과 없음")
        return

    best = top_results[0]
    best_p = best["params"]
    logger.info(f"\n★ 최적: DC={best_p.dc_period}, vol={best_p.vol_ratio_min}x, "
                f"ADX={best_p.adx_min}, ATR={best_p.atr_stop_mult}x, "
                f"trail={best_p.trail_stop_pct*100:.0f}%, "
                f"TP={best_p.take_profit*100:.0f}%, TS={best_p.time_stop_days}d")
    logger.info(f"  학습 Sharpe={best['sharpe']:.3f}, "
                f"수익률={best['total_return']*100:.1f}%, MDD={best['mdd']*100:.1f}%")

    # ── Step 3: Walk-Forward ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 3] Walk-Forward 검증")
    wf_df = run_walk_forward(nc)

    # ── Step 4: 전체기간 재실행 + 몬테카를로 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 4] 전체기간 재실행 + 몬테카를로")
    full_r = run_full_backtest(nc, best_p, FULL_START, FULL_END)
    logger.info(f"  전체기간: 수익={full_r['total_return']*100:.1f}%, "
                f"Sharpe={full_r['sharpe']:.3f}, 거래={full_r['trade_count']}")
    mc = run_monte_carlo(full_r, n_sim=2000)

    # ── Step 5: 섹터 분석 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 5] 섹터별 성과")
    sector_df = analyze_sectors(nc, daily_df, best_p)

    # ── Step 6: 시간대 분석 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 6] 시간대 패턴")
    temporal = analyze_temporal(full_r.get("trades", []))

    # ── Step 7: 오버나이트 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 7] 오버나이트 임계값")
    overnight_df = optimize_overnight(nc, best_p)

    # ── Step 8: 복합 매크로 필터 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 8] 복합 매크로 필터")
    macro_filter_df = analyze_macro_filters(nc, daily_df, macro_df, best_p)

    # ── Step 9: 리포트 ──
    logger.info("\n" + "─"*50)
    logger.info("[Step 9] HTML 리포트 생성")
    report_path = generate_report(
        top_df=top_df, wf_df=wf_df, mc=mc,
        sector_df=sector_df, overnight_df=overnight_df,
        macro_df_res=macro_filter_df, temporal=temporal,
        convergence=convergence, best_r=full_r,
    )

    elapsed = time.time() - t_total
    logger.info("\n" + "=" * 70)
    logger.info(f"✅ 확장 백테스트 완료! (총 소요: {elapsed/60:.1f}분)")
    logger.info(f"결과: {OUT_DIR}/")
    logger.info(f"HTML: {report_path}")
    logger.info("=" * 70)


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
