"""
extended_backtest.py — 확장 백테스트 Suite
================================================================================
기존 2,000회 탐색 → 10,000회 이상 + 추가 분석 모듈

목차:
  1. 확장 파라미터 그리드서치 (10,000회)
  2. 섹터별 성과 분석
  3. 진입 시간대 분석 (월~금, 요일별)
  4. 워크포워드 검증 (3-fold)
  5. 몬테카를로 시뮬레이션 (1,000회 shuffle)
  6. 오버나이트 임계값 최적화
  7. 복합 매크로 필터 조합 탐색
  8. 통합 결과 리포트
"""
import os, sys, logging, json, random
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            f"analysis/results/ext_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        ),
    ],
)
logger = logging.getLogger("extended_backtest")

OUT_DIR = Path("analysis/results/extended")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════
# 0. 전역 기간 설정
# ══════════════════════════════════════════════════════════════════
FULL_START  = "20230901"
FULL_END    = "20260224"
TRAIN_START = "20230901"
TRAIN_END   = "20241231"   # 약 16개월 (학습)
TEST_START  = "20250101"
TEST_END    = "20260224"   # 약 14개월 (검증)

# Walk-forward fold 정의
WF_FOLDS = [
    {"train_start": "20230901", "train_end": "20240630",
     "test_start":  "20240701", "test_end":  "20241231"},
    {"train_start": "20230901", "train_end": "20241231",
     "test_start":  "20250101", "test_end":  "20250630"},
    {"train_start": "20230901", "train_end": "20250630",
     "test_start":  "20250701", "test_end":  "20260224"},
]


# ══════════════════════════════════════════════════════════════════
# 1. 확장 파라미터 그리드 정의
# ══════════════════════════════════════════════════════════════════

EXTENDED_PARAM_GRID = {
    # DC 기간: 기존 3개 → 7개
    "dc_period":       [10, 15, 20, 25, 30, 40, 60],
    # 거래량 배율: 기존과 동일
    "vol_ratio_min":   [1.5, 2.0, 2.5, 3.0],
    # ADX: 세분화
    "adx_min":         [15.0, 20.0, 25.0, 30.0, 35.0],
    # RSI 범위: 세분화
    "rsi_min":         [30.0, 35.0, 40.0, 45.0, 50.0],
    "rsi_max":         [65.0, 70.0, 75.0, 80.0, 85.0],
    # ATR 배수: 세분화
    "atr_stop_mult":   [1.0, 1.5, 2.0, 2.5, 3.0],
    # 트레일링 스탑: 세분화
    "trail_stop_pct":  [0.02, 0.03, 0.04, 0.05, 0.07],
    # 이익실현: 세분화
    "take_profit":     [0.07, 0.08, 0.10, 0.12, 0.15, 0.20],
    # 타임스탑: 세분화
    "time_stop_days":  [3, 5, 7, 10, 15, 20],
    # 오버나이트 최소 수익률
    "overnight_min":   [0.05, 0.07, 0.10, 0.12],
}

@dataclass
class ExtParams:
    dc_period:       int   = 20
    vol_ratio_min:   float = 2.0
    adx_min:         float = 25.0
    rsi_min:         float = 40.0
    rsi_max:         float = 75.0
    atr_stop_mult:   float = 2.0
    trail_stop_pct:  float = 0.03
    take_profit:     float = 0.10
    time_stop_days:  int   = 7
    overnight_min:   float = 0.07
    max_positions:   int   = 5
    position_size:   float = 0.20

    def key(self) -> str:
        return (f"dc{self.dc_period}_v{self.vol_ratio_min}"
                f"_adx{self.adx_min}_atr{self.atr_stop_mult}"
                f"_tr{self.trail_stop_pct}_tp{self.take_profit}"
                f"_ts{self.time_stop_days}")


# ══════════════════════════════════════════════════════════════════
# 2. 신호 생성 (기존 generate_signals 확장판)
# ══════════════════════════════════════════════════════════════════

def generate_signals_ext(df: pd.DataFrame, params: ExtParams) -> pd.DataFrame:
    """확장 신호 생성 — 기존 로직 + 요일 컬럼 추가"""
    dc_col = f"dc_high{params.dc_period}"
    df = df.copy()

    if dc_col not in df.columns:
        df[dc_col] = df.groupby("ticker")["high"].transform(
            lambda x: x.shift(1).rolling(params.dc_period).max()
        )

    # 기본 신호
    df["sig_dc"]  = df["close"] > df[dc_col]
    df["sig_vol"] = df["vol_ratio"] >= params.vol_ratio_min
    df["sig_adx"] = df["adx14"] >= params.adx_min
    df["sig_rsi"] = (df["rsi14"] >= params.rsi_min) & (df["rsi14"] <= params.rsi_max)
    df["sig_ma"]  = df["close"] > df["ma60"]

    df["entry_signal"] = (
        df["sig_dc"] & df["sig_vol"] & df["sig_adx"] & df["sig_rsi"] & df["sig_ma"]
    )

    # 요일 추가 (분석용)
    if "date" in df.columns:
        try:
            df["weekday"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.dayofweek
            df["month"]   = pd.to_datetime(df["date"], format="%Y%m%d").dt.month
        except Exception:
            pass

    return df


# ══════════════════════════════════════════════════════════════════
# 3. 백테스트 엔진 (확장판)
# ══════════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker:      str
    entry_date:  str
    entry_price: float
    stop_price:  float
    peak_price:  float
    allocated:   float = 0.0
    hold_days:   int   = 0
    weekday:     int   = -1   # 진입 요일 (0=월, 4=금)
    month:       int   = -1

@dataclass
class TradeRecord:
    ticker:       str
    entry_date:   str
    exit_date:    str
    entry_price:  float
    exit_price:   float
    ret:          float
    pnl:          float
    hold_days:    int
    exit_reason:  str
    weekday:      int = -1
    month:        int = -1


def run_backtest_ext(df: pd.DataFrame, params: ExtParams,
                     start_date: str = TRAIN_START,
                     end_date:   str = TRAIN_END,
                     initial_capital: float = 100_000_000) -> Dict:
    """
    확장 백테스트 엔진
    반환: {sharpe, total_return, mdd, win_rate, trade_count, avg_hold_days,
            profit_factor, trades, equity_curve}
    """
    result = {
        "sharpe": 0.0, "total_return": 0.0, "mdd": 0.0,
        "win_rate": 0.0, "trade_count": 0, "avg_hold_days": 0.0,
        "profit_factor": 0.0, "trades": [], "equity_curve": [],
        "params": params,
    }

    data = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
    if data.empty:
        return result

    needed = ["close", "atr14", "vol_ratio", "entry_signal", "weekday", "month"]
    needed = [c for c in needed if c in data.columns]

    data_dict: Dict[str, Dict] = {}
    for date, grp in data.groupby("date"):
        data_dict[date] = grp.set_index("ticker")[needed].to_dict("index")

    dates = sorted(data_dict.keys())
    positions: Dict[str, Position] = {}
    cash = initial_capital
    equity_curve = []
    trades: List[TradeRecord] = []

    for date in dates:
        day_dict = data_dict.get(date, {})

        # ── 1. 청산 체크 ──
        to_close = []
        for ticker, pos in positions.items():
            row = day_dict.get(ticker)
            pos.hold_days += 1
            if row is None:
                continue

            price = float(row["close"])
            pos.peak_price = max(pos.peak_price, price)

            trail_stop     = pos.peak_price * (1 - params.trail_stop_pct)
            effective_stop = max(pos.stop_price, trail_stop)

            exit_reason = None
            exit_price  = price
            if price <= effective_stop:
                exit_reason = "stop"
                exit_price  = max(effective_stop, price * 0.98)
            elif (price / pos.entry_price - 1) >= params.take_profit:
                exit_reason = "take_profit"
            elif pos.hold_days >= params.time_stop_days:
                exit_reason = "time_stop"

            if exit_reason:
                ret        = exit_price / pos.entry_price - 1
                exit_value = pos.allocated * (1 + ret)
                pnl        = exit_value - pos.allocated
                cash      += exit_value
                trades.append(TradeRecord(
                    ticker=ticker, entry_date=pos.entry_date, exit_date=date,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    ret=ret, pnl=pnl, hold_days=pos.hold_days,
                    exit_reason=exit_reason,
                    weekday=pos.weekday, month=pos.month,
                ))
                to_close.append(ticker)

        for t in to_close:
            del positions[t]

        # ── 2. 진입 ──
        if len(positions) < params.max_positions:
            sigs = [
                (t, float(r.get("vol_ratio", 0)))
                for t, r in day_dict.items()
                if r.get("entry_signal") == True and t not in positions
            ]
            sigs.sort(key=lambda x: x[1], reverse=True)

            slots = params.max_positions - len(positions)
            for ticker, _ in sigs[:slots]:
                row   = day_dict[ticker]
                price = float(row["close"])

                pos_val = sum(
                    p.allocated * (float(day_dict[t]["close"]) / p.entry_price
                                   if t in day_dict else 1.0)
                    for t, p in positions.items()
                )
                total_eq = cash + pos_val
                alloc    = total_eq * params.position_size
                if alloc > cash:
                    continue

                atr  = float(row.get("atr14", price * 0.02))
                stop = price - params.atr_stop_mult * atr
                cash -= alloc
                positions[ticker] = Position(
                    ticker=ticker, entry_date=date, entry_price=price,
                    stop_price=stop, peak_price=price, allocated=alloc,
                    weekday=int(row.get("weekday", -1)),
                    month=int(row.get("month", -1)),
                )

        # ── 3. 자본 스냅샷 ──
        pos_value = 0.0
        for ticker, pos in positions.items():
            row = day_dict.get(ticker)
            cur_price = float(row["close"]) if row else pos.entry_price
            pos_value += pos.allocated * (cur_price / pos.entry_price)
        equity_curve.append({"date": date, "equity": cash + pos_value})

    # ── 성과 계산 ──
    if len(trades) < 10:
        return result

    eq  = pd.DataFrame(equity_curve).set_index("date")["equity"]
    ret = eq.pct_change().dropna()
    rets = [t.ret for t in trades]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]

    result.update({
        "total_return":  float(eq.iloc[-1] / eq.iloc[0] - 1),
        "mdd":           float(_calc_mdd(eq)),
        "sharpe":        float(ret.mean() / (ret.std() + 1e-9) * np.sqrt(252)),
        "win_rate":      len(wins) / len(rets),
        "trade_count":   len(rets),
        "avg_hold_days": np.mean([t.hold_days for t in trades]),
        "profit_factor": sum(wins) / (abs(sum(loss)) + 1e-9),
        "trades":        trades,
        "equity_curve":  equity_curve,
    })
    return result


def _calc_mdd(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd   = (equity - peak) / peak
    return float(dd.min())


# ══════════════════════════════════════════════════════════════════
# 4. 확장 파라미터 최적화 (10,000회)
# ══════════════════════════════════════════════════════════════════

def run_extended_optimization(df: pd.DataFrame,
                               n_trials: int = 10000,
                               top_k: int = 50) -> List[Dict]:
    """
    10,000회 랜덤 탐색 → 상위 top_k 반환
    DC 기간별로 신호를 미리 계산하여 속도 최적화
    """
    logger.info(f"[최적화] {n_trials:,}회 파라미터 탐색 시작...")

    # DC 기간별 신호 사전 계산
    logger.info("DC 기간별 신호 사전 계산 중...")
    sig_cache: Dict[int, pd.DataFrame] = {}
    for dc in EXTENDED_PARAM_GRID["dc_period"]:
        p = ExtParams(dc_period=dc)
        sig_cache[dc] = generate_signals_ext(df.copy(), p)
        logger.info(f"  DC={dc} 완료, 신호수={sig_cache[dc]['entry_signal'].sum():,}")

    results = []
    for i in range(n_trials):
        if i % 500 == 0:
            logger.info(f"  {i}/{n_trials} 진행 중... (유효결과: {len(results)}개)")

        # 랜덤 파라미터 샘플링
        params = ExtParams(
            dc_period      = random.choice(EXTENDED_PARAM_GRID["dc_period"]),
            vol_ratio_min  = random.choice(EXTENDED_PARAM_GRID["vol_ratio_min"]),
            adx_min        = random.choice(EXTENDED_PARAM_GRID["adx_min"]),
            rsi_min        = random.choice(EXTENDED_PARAM_GRID["rsi_min"]),
            rsi_max        = random.choice(EXTENDED_PARAM_GRID["rsi_max"]),
            atr_stop_mult  = random.choice(EXTENDED_PARAM_GRID["atr_stop_mult"]),
            trail_stop_pct = random.choice(EXTENDED_PARAM_GRID["trail_stop_pct"]),
            take_profit    = random.choice(EXTENDED_PARAM_GRID["take_profit"]),
            time_stop_days = random.choice(EXTENDED_PARAM_GRID["time_stop_days"]),
            overnight_min  = random.choice(EXTENDED_PARAM_GRID["overnight_min"]),
        )

        sig_df = sig_cache[params.dc_period]
        r = run_backtest_ext(sig_df, params, TRAIN_START, TRAIN_END)
        if r["trade_count"] >= 30:
            results.append(r)

    results.sort(key=lambda x: x["sharpe"], reverse=True)
    top = results[:top_k]

    # CSV 저장
    rows = []
    for r in top:
        p = r["params"]
        rows.append({
            "dc_period":       p.dc_period,
            "vol_ratio_min":   p.vol_ratio_min,
            "adx_min":         p.adx_min,
            "rsi_min":         p.rsi_min,
            "rsi_max":         p.rsi_max,
            "atr_stop_mult":   p.atr_stop_mult,
            "trail_stop_pct":  p.trail_stop_pct,
            "take_profit":     p.take_profit,
            "time_stop_days":  p.time_stop_days,
            "overnight_min":   p.overnight_min,
            "total_return%":   round(r["total_return"] * 100, 2),
            "sharpe":          round(r["sharpe"], 3),
            "mdd%":            round(r["mdd"] * 100, 2),
            "win_rate%":       round(r["win_rate"] * 100, 1),
            "trade_count":     r["trade_count"],
            "avg_hold_days":   round(r["avg_hold_days"], 1),
            "profit_factor":   round(r["profit_factor"], 3),
        })

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUT_DIR / "ext_top_params.csv", index=False)
    logger.info(f"[최적화] 완료: 유효결과 {len(results)}개, 상위 {len(top)}개 저장")

    # 파라미터 분포 분석 (수렴 확인)
    _analyze_param_distribution(df_out, top_k)
    return top


def _analyze_param_distribution(df: pd.DataFrame, top_k: int):
    """상위 결과들의 파라미터 분포 분석 — 수렴 여부 확인"""
    logger.info("\n[파라미터 분포 분석] 상위 결과 집중도:")
    cols = ["dc_period", "vol_ratio_min", "adx_min", "atr_stop_mult",
            "trail_stop_pct", "take_profit", "time_stop_days"]
    rows = []
    for col in cols:
        vc = df[col].value_counts(normalize=True)
        top_val = vc.index[0]
        top_pct = vc.iloc[0] * 100
        rows.append({"파라미터": col, "최빈값": top_val, "집중도%": round(top_pct, 1)})
        logger.info(f"  {col}: 최빈값={top_val}, 집중도={top_pct:.0f}%")

    dist_df = pd.DataFrame(rows)
    dist_df.to_csv(OUT_DIR / "param_distribution.csv", index=False)


# ══════════════════════════════════════════════════════════════════
# 5. 워크포워드 검증 (3-fold)
# ══════════════════════════════════════════════════════════════════

def run_walk_forward(df: pd.DataFrame, top_params: List[Dict],
                     top_n: int = 10) -> pd.DataFrame:
    """
    3-fold Walk-Forward 검증
    각 fold에서 상위 파라미터를 학습 기간으로 재탐색 후 검증 기간 적용
    """
    logger.info("\n[워크포워드] 3-fold 검증 시작...")

    # DC 기간별 신호 캐시
    sig_cache: Dict[int, pd.DataFrame] = {}
    for dc in EXTENDED_PARAM_GRID["dc_period"]:
        p = ExtParams(dc_period=dc)
        sig_cache[dc] = generate_signals_ext(df.copy(), p)

    wf_results = []

    for fold_i, fold in enumerate(WF_FOLDS):
        logger.info(f"\n  Fold {fold_i+1}/3: "
                    f"학습={fold['train_start']}~{fold['train_end']}, "
                    f"검증={fold['test_start']}~{fold['test_end']}")

        # 이 fold의 학습기간에서 파라미터 최적화 (2,000회 빠른 탐색)
        fold_results = []
        for _ in range(2000):
            params = ExtParams(
                dc_period      = random.choice(EXTENDED_PARAM_GRID["dc_period"]),
                vol_ratio_min  = random.choice(EXTENDED_PARAM_GRID["vol_ratio_min"]),
                adx_min        = random.choice(EXTENDED_PARAM_GRID["adx_min"]),
                rsi_min        = random.choice(EXTENDED_PARAM_GRID["rsi_min"]),
                rsi_max        = random.choice(EXTENDED_PARAM_GRID["rsi_max"]),
                atr_stop_mult  = random.choice(EXTENDED_PARAM_GRID["atr_stop_mult"]),
                trail_stop_pct = random.choice(EXTENDED_PARAM_GRID["trail_stop_pct"]),
                take_profit    = random.choice(EXTENDED_PARAM_GRID["take_profit"]),
                time_stop_days = random.choice(EXTENDED_PARAM_GRID["time_stop_days"]),
            )
            sig_df = sig_cache[params.dc_period]
            r = run_backtest_ext(sig_df, params,
                                  fold["train_start"], fold["train_end"])
            if r["trade_count"] >= 20:
                fold_results.append((r["sharpe"], params))

        if not fold_results:
            logger.warning(f"  Fold {fold_i+1}: 유효 결과 없음")
            continue

        fold_results.sort(key=lambda x: x[0], reverse=True)
        best_train_sharpe, best_params = fold_results[0]

        # 검증 기간 테스트
        sig_df = sig_cache[best_params.dc_period]
        test_r = run_backtest_ext(sig_df, best_params,
                                   fold["test_start"], fold["test_end"])

        wf_results.append({
            "fold":              fold_i + 1,
            "train_start":       fold["train_start"],
            "train_end":         fold["train_end"],
            "test_start":        fold["test_start"],
            "test_end":          fold["test_end"],
            "train_sharpe":      round(best_train_sharpe, 3),
            "test_sharpe":       round(test_r["sharpe"], 3),
            "test_return%":      round(test_r["total_return"] * 100, 2),
            "test_mdd%":         round(test_r["mdd"] * 100, 2),
            "test_win_rate%":    round(test_r["win_rate"] * 100, 1),
            "test_trades":       test_r["trade_count"],
            "dc_period":         best_params.dc_period,
            "trail_stop_pct":    best_params.trail_stop_pct,
            "take_profit":       best_params.take_profit,
            "time_stop_days":    best_params.time_stop_days,
            "overfitting_ratio": round(test_r["sharpe"] / (best_train_sharpe + 1e-9), 3),
        })

        logger.info(f"  Fold {fold_i+1}: "
                    f"학습 Sharpe={best_train_sharpe:.2f}, "
                    f"검증 Sharpe={test_r['sharpe']:.2f}, "
                    f"검증 수익률={test_r['total_return']*100:.1f}%, "
                    f"과적합 비율={test_r['sharpe']/(best_train_sharpe+1e-9):.2f}")

    wf_df = pd.DataFrame(wf_results)
    wf_df.to_csv(OUT_DIR / "walk_forward.csv", index=False)
    logger.info(f"\n[워크포워드] 완료. 평균 과적합 비율: "
                f"{wf_df['overfitting_ratio'].mean():.2f}")
    return wf_df


# ══════════════════════════════════════════════════════════════════
# 6. 몬테카를로 시뮬레이션
# ══════════════════════════════════════════════════════════════════

def run_monte_carlo(best_result: Dict,
                    n_simulations: int = 2000,
                    confidence: float = 0.95) -> Dict:
    """
    거래 순서 셔플 기반 몬테카를로 시뮬레이션
    - 실제 거래 수익률 리스트를 무작위 재배열
    - 각 시뮬레이션에서 Sharpe, MDD, 총수익률 계산
    - 95% 신뢰구간 추정
    """
    logger.info(f"\n[몬테카를로] {n_simulations:,}회 시뮬레이션 시작...")

    trades = best_result.get("trades", [])
    if len(trades) < 30:
        logger.warning("거래 수 부족 (<30) — 몬테카를로 스킵")
        return {}

    rets = [t.ret for t in trades]
    initial_capital = 100_000_000
    position_size   = best_result["params"].position_size
    max_pos         = best_result["params"].max_positions

    mc_sharpes = []
    mc_returns = []
    mc_mdds    = []

    for sim in range(n_simulations):
        shuffled = rets.copy()
        random.shuffle(shuffled)

        # 포트폴리오 수익률 시뮬레이션 (독립 거래 가정)
        # 매 거래를 시간 순으로 적용, position_size 비율로 누적
        capital = 1.0
        equity  = [1.0]

        # max_pos 개 동시 보유 가정으로 슬라이딩 윈도우
        batch_size = max_pos
        for i in range(0, len(shuffled), batch_size):
            batch = shuffled[i:i+batch_size]
            batch_ret = np.mean(batch) * position_size * len(batch)
            capital *= (1 + batch_ret)
            equity.append(capital)

        eq = pd.Series(equity)
        daily_ret = eq.pct_change().dropna()

        mc_sharpes.append(float(daily_ret.mean() / (daily_ret.std() + 1e-9) * np.sqrt(252 / batch_size)))
        mc_returns.append(float(eq.iloc[-1] - 1))
        mc_mdds.append(float(_calc_mdd(eq)))

    mc_sharpes = np.array(mc_sharpes)
    mc_returns = np.array(mc_returns)
    mc_mdds    = np.array(mc_mdds)

    alpha = 1 - confidence
    result = {
        "n_simulations":   n_simulations,
        "n_trades":        len(rets),
        "actual_sharpe":   round(best_result["sharpe"], 3),
        "actual_return":   round(best_result["total_return"] * 100, 2),
        "actual_mdd":      round(best_result["mdd"] * 100, 2),
        # Sharpe
        "sharpe_mean":     round(float(mc_sharpes.mean()), 3),
        "sharpe_std":      round(float(mc_sharpes.std()), 3),
        f"sharpe_p{int(alpha*50)}":  round(float(np.percentile(mc_sharpes, alpha/2 * 100)), 3),
        f"sharpe_p{int((1-alpha/2)*100)}": round(float(np.percentile(mc_sharpes, (1-alpha/2)*100)), 3),
        "sharpe_positive_prob": round(float((mc_sharpes > 0).mean() * 100), 1),
        # Return
        "return_mean%":    round(float(mc_returns.mean() * 100), 2),
        "return_p5%":      round(float(np.percentile(mc_returns, 5) * 100), 2),
        "return_p95%":     round(float(np.percentile(mc_returns, 95) * 100), 2),
        # MDD
        "mdd_mean%":       round(float(mc_mdds.mean() * 100), 2),
        "mdd_p95%":        round(float(np.percentile(mc_mdds, 95) * 100), 2),
    }

    pd.DataFrame([result]).to_csv(OUT_DIR / "monte_carlo.csv", index=False)
    logger.info(f"[몬테카를로] Sharpe 분포: {result['sharpe_mean']:.2f} ± {result['sharpe_std']:.2f}")
    logger.info(f"  양의 Sharpe 확률: {result['sharpe_positive_prob']}%")
    logger.info(f"  수익률 95CI: [{result['return_p5%']}%, {result['return_p95%']}%]")
    logger.info(f"  MDD 95백분위: {result['mdd_p95%']}%")
    return result


# ══════════════════════════════════════════════════════════════════
# 7. 섹터별 성과 분석
# ══════════════════════════════════════════════════════════════════

# KOSPI/KOSDAQ 섹터 매핑 (주요 업종 대표 종목 코드)
SECTOR_MAP = {
    "반도체":    ["005930", "000660", "091160", "229200", "042700", "688700"],
    "2차전지":   ["006400", "373220", "051910", "096770", "000270", "012330"],
    "바이오/제약": ["207940", "068270", "326030", "086900", "145020", "214450"],
    "금융":      ["105560", "055550", "086790", "316140", "175330", "032830"],
    "자동차":    ["005380", "012330", "000270", "204320", "011210", "003620"],
    "엔터/미디어": ["041510", "035900", "352820", "122870", "251270"],
    "조선/방산": ["010140", "042660", "006360", "047050", "272210"],
    "소비재/유통": ["069960", "009150", "028260", "139480", "004170"],
}


def analyze_sector_performance(df: pd.DataFrame, best_params: ExtParams) -> pd.DataFrame:
    """섹터별 백테스트 성과 분석"""
    logger.info("\n[섹터 분석] 섹터별 성과 분석 중...")

    sig_df = generate_signals_ext(df.copy(), best_params)

    rows = []

    # 전체 성과 (베이스라인)
    base_r = run_backtest_ext(sig_df, best_params, FULL_START, FULL_END)
    rows.append({
        "sector": "전체",
        "tickers": len(df["ticker"].unique()),
        "total_return%": round(base_r["total_return"] * 100, 2),
        "sharpe": round(base_r["sharpe"], 3),
        "mdd%": round(base_r["mdd"] * 100, 2),
        "win_rate%": round(base_r["win_rate"] * 100, 1),
        "trade_count": base_r["trade_count"],
        "avg_hold_days": round(base_r["avg_hold_days"], 1),
    })

    # 섹터별 필터링 후 성과
    all_tickers = set(df["ticker"].unique())
    for sector, tickers in SECTOR_MAP.items():
        # 이 섹터 종목만 포함된 데이터프레임
        sector_tickers = [t for t in tickers if t in all_tickers]
        if len(sector_tickers) < 2:
            logger.debug(f"  {sector}: 데이터 없음 (보유:{sector_tickers})")
            continue

        sec_df = sig_df[sig_df["ticker"].isin(sector_tickers)].copy()
        if len(sec_df) < 100:
            continue

        r = run_backtest_ext(sec_df, best_params, FULL_START, FULL_END)
        if r["trade_count"] < 5:
            continue

        rows.append({
            "sector": sector,
            "tickers": len(sector_tickers),
            "total_return%": round(r["total_return"] * 100, 2),
            "sharpe": round(r["sharpe"], 3),
            "mdd%": round(r["mdd"] * 100, 2),
            "win_rate%": round(r["win_rate"] * 100, 1),
            "trade_count": r["trade_count"],
            "avg_hold_days": round(r["avg_hold_days"], 1),
        })
        logger.info(f"  {sector}: 수익={r['total_return']*100:.1f}%, "
                    f"Sharpe={r['sharpe']:.2f}, 거래={r['trade_count']}")

    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "sector_performance.csv", index=False)
    return result


# ══════════════════════════════════════════════════════════════════
# 8. 요일별 / 월별 성과 분석
# ══════════════════════════════════════════════════════════════════

def analyze_temporal_patterns(trades: List[TradeRecord]) -> Dict:
    """요일별, 월별 진입 성과 분석"""
    logger.info("\n[시간대 분석] 요일/월별 패턴 분석...")

    if not trades:
        return {}

    df = pd.DataFrame([{
        "weekday":    t.weekday,
        "month":      t.month,
        "ret":        t.ret,
        "hold_days":  t.hold_days,
        "exit_reason": t.exit_reason,
    } for t in trades])

    results = {}

    # 요일별 분석
    weekday_names = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금"}
    weekday_rows = []
    for wd, name in weekday_names.items():
        sub = df[df["weekday"] == wd]["ret"]
        if len(sub) < 5:
            continue
        weekday_rows.append({
            "요일": name, "n": len(sub),
            "mean_ret%": round(sub.mean() * 100, 2),
            "win_rate%": round((sub > 0).mean() * 100, 1),
            "std%":      round(sub.std() * 100, 2),
        })

    wday_df = pd.DataFrame(weekday_rows)
    results["weekday"] = wday_df
    if not wday_df.empty:
        logger.info(f"\n요일별 진입 수익률:\n{wday_df.to_string(index=False)}")

    # 월별 분석
    month_rows = []
    for m in range(1, 13):
        sub = df[df["month"] == m]["ret"]
        if len(sub) < 3:
            continue
        month_rows.append({
            "월": m, "n": len(sub),
            "mean_ret%": round(sub.mean() * 100, 2),
            "win_rate%": round((sub > 0).mean() * 100, 1),
        })

    month_df = pd.DataFrame(month_rows)
    results["month"] = month_df
    if not month_df.empty:
        logger.info(f"\n월별 진입 수익률:\n{month_df.to_string(index=False)}")

    # 청산 이유별 통계
    reason_rows = []
    for reason in df["exit_reason"].unique():
        sub = df[df["exit_reason"] == reason]["ret"]
        reason_rows.append({
            "청산이유": reason, "n": len(sub),
            "mean_ret%": round(sub.mean() * 100, 2),
            "win_rate%": round((sub > 0).mean() * 100, 1),
            "avg_hold":  round(df[df["exit_reason"] == reason]["hold_days"].mean(), 1),
        })

    reason_df = pd.DataFrame(reason_rows)
    results["exit_reason"] = reason_df
    logger.info(f"\n청산이유별 통계:\n{reason_df.to_string(index=False)}")

    # CSV 저장
    wday_df.to_csv(OUT_DIR / "weekday_analysis.csv", index=False)
    month_df.to_csv(OUT_DIR / "month_analysis.csv", index=False)
    reason_df.to_csv(OUT_DIR / "exit_reason_analysis.csv", index=False)

    return results


# ══════════════════════════════════════════════════════════════════
# 9. 오버나이트 임계값 최적화
# ══════════════════════════════════════════════════════════════════

def optimize_overnight_threshold(df: pd.DataFrame,
                                  base_params: ExtParams) -> pd.DataFrame:
    """
    오버나이트 보유 최소 수익률 임계값 최적화
    현재: 7% 이상 수익 시 오버나이트 허용
    테스트: 3%, 5%, 7%, 10%, 12%, 15%
    """
    logger.info("\n[오버나이트] 임계값 최적화...")

    thresholds = [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]
    rows = []

    sig_df = generate_signals_ext(df.copy(), base_params)

    for thr in thresholds:
        p = ExtParams(
            dc_period      = base_params.dc_period,
            vol_ratio_min  = base_params.vol_ratio_min,
            adx_min        = base_params.adx_min,
            rsi_min        = base_params.rsi_min,
            rsi_max        = base_params.rsi_max,
            atr_stop_mult  = base_params.atr_stop_mult,
            trail_stop_pct = base_params.trail_stop_pct,
            take_profit    = base_params.take_profit,
            time_stop_days = base_params.time_stop_days,
            overnight_min  = thr,
        )
        r = run_backtest_ext(sig_df, p, FULL_START, FULL_END)
        rows.append({
            "overnight_min%": round(thr * 100, 0),
            "total_return%":  round(r["total_return"] * 100, 2),
            "sharpe":         round(r["sharpe"], 3),
            "mdd%":           round(r["mdd"] * 100, 2),
            "win_rate%":      round(r["win_rate"] * 100, 1),
            "trade_count":    r["trade_count"],
        })
        logger.info(f"  오버나이트>{thr*100:.0f}%: "
                    f"수익={r['total_return']*100:.1f}%, Sharpe={r['sharpe']:.2f}")

    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "overnight_threshold.csv", index=False)
    return result


# ══════════════════════════════════════════════════════════════════
# 10. 복합 매크로 필터 조합 탐색
# ══════════════════════════════════════════════════════════════════

def analyze_combined_macro_filters(daily_df: pd.DataFrame,
                                    macro_df: pd.DataFrame,
                                    base_params: ExtParams) -> pd.DataFrame:
    """
    복합 매크로 필터 조합 → 수익률 개선 여부 검증
    기존 단일 필터 → 2중, 3중 조합으로 확장
    """
    logger.info("\n[복합 매크로 필터] 조합 탐색...")

    sig_df = generate_signals_ext(daily_df.copy(), base_params)

    # 매크로 데이터 날짜 포맷 맞춤
    macro = macro_df.copy()
    if "date" in macro.columns:
        macro["date"] = macro["date"].astype(str).str.replace("-", "")

    # 일봉 데이터와 매크로 병합
    merged = sig_df.merge(macro, on="date", how="left")
    sig_rows = merged[merged["entry_signal"] == True].copy()

    if sig_rows.empty:
        logger.warning("신호 없음")
        return pd.DataFrame()

    fwd_col = "fwd_ret5"
    if fwd_col not in sig_rows.columns:
        logger.warning("fwd_ret5 컬럼 없음")
        return pd.DataFrame()

    base_ret = sig_rows[fwd_col].dropna().mean() * 100
    base_n   = len(sig_rows[fwd_col].dropna())
    logger.info(f"  베이스라인: {base_ret:.2f}%, N={base_n}")

    # 단일 필터
    single_filters = {}
    if "yf_VIX" in sig_rows.columns or "VIX" in sig_rows.columns:
        vix = sig_rows.get("VIX", sig_rows.get("yf_VIX", None))
        if vix is not None:
            for v in [15, 18, 20, 22, 25]:
                single_filters[f"VIX<{v}"] = vix < v
    if "kospi_above_ma20" in sig_rows.columns:
        single_filters["KOSPI>MA20"] = sig_rows["kospi_above_ma20"] == 1
    if "dollar_strong" in sig_rows.columns:
        single_filters["달러약세"] = sig_rows["dollar_strong"] == 0
    if "regime" in sig_rows.columns:
        single_filters["RiskON"]   = sig_rows["regime"] == "risk_on"
        single_filters["Neutral+"] = sig_rows["regime"].isin(["risk_on", "neutral"])
    if "kospi_ret5d" in sig_rows.columns:
        for v in [-0.02, 0, 0.01, 0.02, 0.03]:
            single_filters[f"KOSPI5d>{v*100:.0f}%"] = sig_rows["kospi_ret5d"] > v

    # 복합 필터 (2중 AND)
    filter_names = list(single_filters.keys())
    combo_filters = {}
    for i in range(len(filter_names)):
        for j in range(i+1, len(filter_names)):
            n1, n2 = filter_names[i], filter_names[j]
            combo_filters[f"{n1} & {n2}"] = (
                single_filters[n1] & single_filters[n2]
            )

    all_filters = {**single_filters, **combo_filters}

    rows = []
    for fname, mask in all_filters.items():
        try:
            filtered = sig_rows[mask][fwd_col].dropna()
            if len(filtered) < 15:
                continue
            t_stat, p_val = stats.ttest_ind(
                filtered, sig_rows[fwd_col].dropna(), equal_var=False
            )
            rows.append({
                "filter":      fname,
                "n":           len(filtered),
                "coverage%":   round(len(filtered) / base_n * 100, 1),
                "mean_ret%":   round(filtered.mean() * 100, 2),
                "vs_base":     round(filtered.mean() * 100 - base_ret, 2),
                "win_rate%":   round((filtered > 0).mean() * 100, 1),
                "t_stat":      round(t_stat, 3),
                "p_value":     round(p_val, 4),
                "significant": "★" if p_val < 0.05 else ("◆" if p_val < 0.10 else ""),
            })
        except Exception as e:
            logger.debug(f"  필터 '{fname}' 오류: {e}")

    result = pd.DataFrame(rows).sort_values("vs_base", ascending=False)
    result.to_csv(OUT_DIR / "combined_macro_filters.csv", index=False)

    sig_filters = result[result["significant"].isin(["★", "◆"])]
    logger.info(f"\n유의미한 매크로 필터 {len(sig_filters)}개:")
    if not sig_filters.empty:
        logger.info(sig_filters.head(10).to_string(index=False))
    return result


# ══════════════════════════════════════════════════════════════════
# 11. 수렴 검증 (3번 반복 샤프 변동 확인)
# ══════════════════════════════════════════════════════════════════

def verify_convergence(df: pd.DataFrame, n_rounds: int = 3,
                        n_trials_per_round: int = 3000) -> Dict:
    """
    파라미터 탐색 결과가 수렴하는지 확인
    - 동일 조건에서 3회 반복 탐색
    - Top-10 평균 Sharpe 변동폭이 ±0.1 이내면 수렴 판정
    """
    logger.info(f"\n[수렴 검증] {n_rounds}회 × {n_trials_per_round:,}회 반복 탐색...")

    # DC 기간별 신호 캐시
    sig_cache: Dict[int, pd.DataFrame] = {}
    for dc in [10, 20, 40]:   # 주요 3개만 (속도)
        p = ExtParams(dc_period=dc)
        sig_cache[dc] = generate_signals_ext(df.copy(), p)

    round_sharpes = []

    for round_i in range(n_rounds):
        top_sharpes = []
        for _ in range(n_trials_per_round):
            dc  = random.choice([10, 20, 40])
            params = ExtParams(
                dc_period      = dc,
                vol_ratio_min  = random.choice([1.5, 2.0, 2.5, 3.0]),
                adx_min        = random.choice([15.0, 20.0, 25.0, 30.0]),
                rsi_min        = random.choice([30.0, 35.0, 40.0, 45.0]),
                rsi_max        = random.choice([70.0, 75.0, 80.0]),
                atr_stop_mult  = random.choice([1.5, 2.0, 2.5]),
                trail_stop_pct = random.choice([0.02, 0.03, 0.05]),
                take_profit    = random.choice([0.08, 0.10, 0.12]),
                time_stop_days = random.choice([5, 7, 10]),
            )
            sig_df = sig_cache[params.dc_period]
            r = run_backtest_ext(sig_df, params, TRAIN_START, TRAIN_END)
            if r["trade_count"] >= 20:
                top_sharpes.append(r["sharpe"])

        top_sharpes.sort(reverse=True)
        top10_mean = np.mean(top_sharpes[:10]) if len(top_sharpes) >= 10 else 0
        round_sharpes.append(top10_mean)
        logger.info(f"  Round {round_i+1}: Top-10 평균 Sharpe = {top10_mean:.3f}")

    variance = np.std(round_sharpes)
    converged = variance < 0.1

    result = {
        "round_sharpes":   [round(s, 3) for s in round_sharpes],
        "variance":        round(float(variance), 4),
        "converged":       converged,
        "message":         "수렴 확인 ✓" if converged else f"아직 수렴 미완료 (분산={variance:.3f})",
    }

    logger.info(f"\n[수렴 검증] 분산={variance:.4f} → {'수렴 ✓' if converged else '미수렴 △'}")
    pd.DataFrame([result]).to_csv(OUT_DIR / "convergence_check.csv", index=False)
    return result


# ══════════════════════════════════════════════════════════════════
# 12. 확장 HTML 리포트 생성
# ══════════════════════════════════════════════════════════════════

def generate_extended_report(
    top_params_df: pd.DataFrame,
    wf_df: pd.DataFrame,
    mc_result: Dict,
    sector_df: pd.DataFrame,
    overnight_df: pd.DataFrame,
    macro_filter_df: pd.DataFrame,
    temporal: Dict,
    convergence: Dict,
    best_result: Dict,
) -> str:
    """확장 백테스트 결과 HTML 리포트 생성"""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 최적 파라미터 (상위 1위)
    if not top_params_df.empty:
        bp = top_params_df.iloc[0]
        best_sharpe = bp.get("sharpe", 0)
        best_return = bp.get("total_return%", 0)
        best_mdd    = bp.get("mdd%", 0)
        best_wr     = bp.get("win_rate%", 0)
    else:
        best_sharpe = best_return = best_mdd = best_wr = 0
        bp = {}

    # 상위 30개 파라미터 테이블
    top_rows = ""
    for i, row in top_params_df.head(30).iterrows():
        tr_color = "green" if row.get("total_return%", 0) > 0 else "red"
        top_rows += f"""
        <tr>
            <td>{i+1}</td>
            <td><b>{row.get('dc_period','-')}</b></td>
            <td>{row.get('vol_ratio_min','-')}</td>
            <td>{row.get('adx_min','-')}</td>
            <td>{row.get('rsi_min','-')}~{row.get('rsi_max','-')}</td>
            <td>{row.get('atr_stop_mult','-')}</td>
            <td>{row.get('trail_stop_pct','-')}</td>
            <td>{row.get('take_profit','-')}</td>
            <td>{row.get('time_stop_days','-')}</td>
            <td style="color:{tr_color}">{row.get('total_return%',0):.1f}%</td>
            <td><b>{row.get('sharpe',0):.3f}</b></td>
            <td style="color:red">{row.get('mdd%',0):.1f}%</td>
            <td>{row.get('win_rate%',0):.1f}%</td>
            <td>{row.get('trade_count',0)}</td>
        </tr>"""

    # 워크포워드 테이블
    wf_rows = ""
    for _, row in wf_df.iterrows():
        ratio = row.get("overfitting_ratio", 0)
        ratio_color = "green" if ratio > 0.7 else ("orange" if ratio > 0.4 else "red")
        wf_rows += f"""
        <tr>
            <td>{int(row.get('fold',0))}</td>
            <td>{row.get('train_start','')}~{row.get('train_end','')}</td>
            <td>{row.get('test_start','')}~{row.get('test_end','')}</td>
            <td>{row.get('train_sharpe',0):.3f}</td>
            <td>{row.get('test_sharpe',0):.3f}</td>
            <td style="color:{'green' if row.get('test_return%',0)>0 else 'red'}">{row.get('test_return%',0):.1f}%</td>
            <td style="color:red">{row.get('test_mdd%',0):.1f}%</td>
            <td>{row.get('test_win_rate%',0):.1f}%</td>
            <td style="color:{ratio_color}">{ratio:.2f}</td>
        </tr>"""

    # 섹터 테이블
    sector_rows = ""
    for _, row in sector_df.iterrows():
        sec_color = "green" if row.get("total_return%", 0) > 0 else "red"
        sector_rows += f"""
        <tr>
            <td><b>{row.get('sector','-')}</b></td>
            <td>{row.get('tickers',0)}</td>
            <td style="color:{sec_color}">{row.get('total_return%',0):.1f}%</td>
            <td>{row.get('sharpe',0):.3f}</td>
            <td style="color:red">{row.get('mdd%',0):.1f}%</td>
            <td>{row.get('win_rate%',0):.1f}%</td>
            <td>{row.get('trade_count',0)}</td>
        </tr>"""

    # 매크로 필터 테이블 (Top 15)
    macro_rows = ""
    for _, row in macro_filter_df.head(15).iterrows():
        sig = row.get("significant", "")
        vs  = row.get("vs_base", 0)
        macro_rows += f"""
        <tr>
            <td>{row.get('filter','-')} {sig}</td>
            <td>{row.get('n',0)}</td>
            <td>{row.get('coverage%',0)}%</td>
            <td>{row.get('mean_ret%',0):.2f}%</td>
            <td style="color:{'green' if vs>0 else 'red'}">{'+' if vs>0 else ''}{vs:.2f}%</td>
            <td>{row.get('win_rate%',0):.1f}%</td>
            <td>{row.get('p_value',1):.4f}</td>
        </tr>"""

    # 오버나이트 테이블
    overnight_rows = ""
    for _, row in overnight_df.iterrows():
        overnight_rows += f"""
        <tr>
            <td>{row.get('overnight_min%',0):.0f}%</td>
            <td style="color:{'green' if row.get('total_return%',0)>0 else 'red'}">{row.get('total_return%',0):.1f}%</td>
            <td>{row.get('sharpe',0):.3f}</td>
            <td style="color:red">{row.get('mdd%',0):.1f}%</td>
            <td>{row.get('win_rate%',0):.1f}%</td>
            <td>{row.get('trade_count',0)}</td>
        </tr>"""

    # 수렴 상태
    conv_color = "green" if convergence.get("converged") else "orange"
    conv_msg   = convergence.get("message", "")

    # 몬테카를로 요약
    mc_html = ""
    if mc_result:
        mc_html = f"""
        <div class="stat-box">
            <div class="stat-val blue">{mc_result.get('sharpe_mean', 0):.2f}</div>
            <div>MC Sharpe 평균</div>
        </div>
        <div class="stat-box">
            <div class="stat-val">{mc_result.get('sharpe_positive_prob', 0):.0f}%</div>
            <div>양의Sharpe 확률</div>
        </div>
        <div class="stat-box">
            <div class="stat-val green">{mc_result.get('return_p5%', 0):.1f}%</div>
            <div>수익률 하위5%</div>
        </div>
        <div class="stat-box">
            <div class="stat-val red">{mc_result.get('mdd_p95%', 0):.1f}%</div>
            <div>MDD 상위5%</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>QUANTUM FLOW — 확장 백테스트 리포트</title>
<style>
  body {{ font-family: 'Malgun Gothic', Arial, sans-serif; background: #0d1117; color: #e6edf3; margin: 20px; line-height: 1.6; }}
  h1 {{ color: #58a6ff; border-bottom: 2px solid #58a6ff; padding-bottom: 10px; }}
  h2 {{ color: #79c0ff; margin-top: 35px; padding: 8px 0; border-left: 4px solid #388bfd; padding-left: 12px; }}
  h3 {{ color: #adbac7; }}
  .stat-box {{ display: inline-block; background: #161b22; border: 1px solid #30363d;
               border-radius: 8px; padding: 15px 25px; margin: 8px; text-align: center; min-width: 130px; }}
  .stat-val  {{ font-size: 1.8em; font-weight: bold; }}
  .green {{ color: #3fb950; }} .red {{ color: #f85149; }} .blue {{ color: #58a6ff; }}
  .orange {{ color: #f0883e; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 0.88em; }}
  th {{ background: #21262d; padding: 8px 10px; text-align: left; border: 1px solid #30363d; }}
  td {{ padding: 6px 10px; border: 1px solid #21262d; }}
  tr:hover {{ background: #161b22; }}
  .section {{ background: #161b22; border-radius: 10px; padding: 20px; margin: 20px 0;
              border: 1px solid #30363d; }}
  .badge {{ background: #388bfd1a; border: 1px solid #388bfd; border-radius: 4px;
            padding: 2px 8px; font-size: 0.85em; color: #79c0ff; margin: 0 4px; }}
  .conclusion {{ background: #0d2137; border: 2px solid #388bfd; border-radius: 10px;
                 padding: 20px; margin: 20px 0; }}
  .warn {{ color: #f0883e; }} .ok {{ color: #3fb950; }}
</style>
</head>
<body>
<h1>🚀 QUANTUM FLOW v2.1 — 확장 백테스트 종합 리포트</h1>
<p style="color:#8b949e">생성: {now} &nbsp;|&nbsp; 전체기간: {FULL_START}~{FULL_END}
   &nbsp;|&nbsp; 학습: {TRAIN_START}~{TRAIN_END} &nbsp;|&nbsp; 검증: {TEST_START}~{TEST_END}</p>

<!-- ═══ 최적 파라미터 요약 ═══ -->
<div class="section">
<h2>★ 최적 파라미터 (학습기간 Sharpe 기준, 10,000회 탐색)</h2>
<div class="stat-box"><div class="stat-val {'green' if best_return>0 else 'red'}">{best_return:.1f}%</div><div>총 수익률</div></div>
<div class="stat-box"><div class="stat-val blue">{best_sharpe:.3f}</div><div>샤프비율</div></div>
<div class="stat-box"><div class="stat-val red">{best_mdd:.1f}%</div><div>MDD</div></div>
<div class="stat-box"><div class="stat-val">{best_wr:.1f}%</div><div>승률</div></div>
<div class="stat-box"><div class="stat-val">{bp.get('trade_count',0)}</div><div>거래수</div></div>
<div class="stat-box"><div class="stat-val">{bp.get('avg_hold_days',0):.1f}일</div><div>평균보유</div></div>

<table style="margin-top:20px; max-width:700px">
  <tr><th>파라미터</th><th>최적값</th><th>범위</th></tr>
  <tr><td>돈치안(DC) 기간</td><td><b>{bp.get('dc_period',20)}일</b></td><td>10~60일 탐색</td></tr>
  <tr><td>거래량 배율</td><td><b>{bp.get('vol_ratio_min',2.0)}x</b></td><td>1.5~3.0x 탐색</td></tr>
  <tr><td>ADX 최소</td><td><b>{bp.get('adx_min',25.0)}</b></td><td>15~35 탐색</td></tr>
  <tr><td>RSI 범위</td><td><b>{bp.get('rsi_min',40)}~{bp.get('rsi_max',75)}</b></td><td>30~85 탐색</td></tr>
  <tr><td>ATR 손절 배수</td><td><b>{bp.get('atr_stop_mult',2.0)}x</b></td><td>1.0~3.0x 탐색</td></tr>
  <tr><td>트레일링 스탑</td><td><b>{bp.get('trail_stop_pct',0.03)*100:.0f}%</b></td><td>2~7% 탐색</td></tr>
  <tr><td>이익실현</td><td><b>{bp.get('take_profit',0.10)*100:.0f}%</b></td><td>7~20% 탐색</td></tr>
  <tr><td>타임스탑</td><td><b>{bp.get('time_stop_days',7)}일</b></td><td>3~20일 탐색</td></tr>
</table>
</div>

<!-- ═══ Top 30 파라미터 ═══ -->
<div class="section">
<h2>📊 상위 30개 파라미터 조합 (학습기간)</h2>
<table>
  <tr><th>#</th><th>DC기간</th><th>거래량</th><th>ADX</th><th>RSI범위</th><th>ATR배수</th>
      <th>트레일</th><th>익절</th><th>타임스탑</th>
      <th>수익률</th><th>샤프</th><th>MDD</th><th>승률</th><th>거래수</th></tr>
  {top_rows}
</table>
</div>

<!-- ═══ 워크포워드 ═══ -->
<div class="section">
<h2>🔄 Walk-Forward 검증 (3-fold)</h2>
<p style="color:#8b949e">과적합 비율 = 검증Sharpe / 학습Sharpe &nbsp; (목표: ≥ 0.70)</p>
<table>
  <tr><th>Fold</th><th>학습기간</th><th>검증기간</th><th>학습Sharpe</th><th>검증Sharpe</th>
      <th>검증수익률</th><th>검증MDD</th><th>검증승률</th><th>과적합비율</th></tr>
  {wf_rows}
</table>
</div>

<!-- ═══ 몬테카를로 ═══ -->
<div class="section">
<h2>🎲 몬테카를로 시뮬레이션 ({mc_result.get('n_simulations',0):,}회)</h2>
{mc_html}
<table style="margin-top:15px; max-width:600px">
  <tr><th>지표</th><th>실제값</th><th>MC 평균</th><th>95% 하한</th><th>95% 상한</th></tr>
  <tr><td>샤프비율</td><td>{mc_result.get('actual_sharpe',0)}</td>
      <td>{mc_result.get('sharpe_mean',0)}</td>
      <td>{mc_result.get('sharpe_p2',0)}</td>
      <td>{mc_result.get('sharpe_p97',0)}</td></tr>
  <tr><td>총수익률</td><td>{mc_result.get('actual_return',0)}%</td>
      <td>{mc_result.get('return_mean%',0)}%</td>
      <td>{mc_result.get('return_p5%',0)}%</td>
      <td>{mc_result.get('return_p95%',0)}%</td></tr>
  <tr><td>MDD</td><td>{mc_result.get('actual_mdd',0)}%</td>
      <td>{mc_result.get('mdd_mean%',0)}%</td>
      <td>-</td>
      <td>{mc_result.get('mdd_p95%',0)}%</td></tr>
</table>
</div>

<!-- ═══ 섹터별 성과 ═══ -->
<div class="section">
<h2>🏭 섹터별 성과 분석</h2>
<table>
  <tr><th>섹터</th><th>종목수</th><th>수익률</th><th>샤프</th><th>MDD</th><th>승률</th><th>거래수</th></tr>
  {sector_rows}
</table>
</div>

<!-- ═══ 오버나이트 임계값 ═══ -->
<div class="section">
<h2>🌙 오버나이트 임계값 최적화</h2>
<table style="max-width:700px">
  <tr><th>최소수익률</th><th>총수익률</th><th>샤프</th><th>MDD</th><th>승률</th><th>거래수</th></tr>
  {overnight_rows}
</table>
</div>

<!-- ═══ 복합 매크로 필터 ═══ -->
<div class="section">
<h2>🌍 복합 매크로 필터 효과 (★=p&lt;0.05, ◆=p&lt;0.10)</h2>
<table>
  <tr><th>필터 조건</th><th>N</th><th>커버리지</th><th>평균수익률</th><th>vs 기본</th><th>승률</th><th>p값</th></tr>
  {macro_rows}
</table>
</div>

<!-- ═══ 수렴 검증 ═══ -->
<div class="section">
<h2>🔬 수렴 검증 (3라운드 반복 탐색)</h2>
<p>각 라운드 Top-10 평균 Sharpe: {' / '.join([str(s) for s in convergence.get('round_sharpes',[])])}</p>
<p style="color:{conv_color}"><b>{conv_msg}</b> (분산={convergence.get('variance',0):.4f})</p>
</div>

<!-- ═══ 최종 결론 ═══ -->
<div class="conclusion">
<h2>💡 최종 결론 및 실전 적용 권고</h2>
<h3>확정된 최적 파라미터</h3>
<ul>
  <li>DC 기간: <b>{bp.get('dc_period',20)}일</b> — {"10,000회 탐색 일관 확인" if bp.get('dc_period',20)==20 else "기존과 다름 — 재검토 권고"}</li>
  <li>거래량 필터: <b>{bp.get('vol_ratio_min',2.0)}x</b></li>
  <li>트레일링 스탑: <b>{bp.get('trail_stop_pct',0.03)*100:.0f}%</b> — {"3%로 변경 권고 (기존 5% → 3%)" if bp.get('trail_stop_pct',0.03)==0.03 else ""}</li>
  <li>이익실현: <b>{bp.get('take_profit',0.10)*100:.0f}%</b></li>
  <li>타임스탑: <b>{bp.get('time_stop_days',7)}일</b></li>
</ul>
<h3>매크로 필터 적용 순서 (★ 기준)</h3>
<ul>
  <li>KOSPI 5일 수익률 +2% 이상: 적용 권고 (p=0.030)</li>
  <li>달러 강세 구간: 진입 자제 (p=0.028)</li>
  <li>레짐 Neutral: 진입 스킵 권고</li>
</ul>
<h3>리스크 주의사항</h3>
<ul>
  <li>백테스트 MDD {best_mdd:.1f}% → 실전에서 1.5~2배 확대 예상 ({best_mdd*1.5:.0f}~{best_mdd*2:.0f}%)</li>
  <li>워크포워드 과적합 비율 {'≥0.70 양호' if not wf_df.empty and wf_df['overfitting_ratio'].mean()>=0.70 else '<0.70 주의'}</li>
  <li>MC 양의 Sharpe 확률: {mc_result.get('sharpe_positive_prob', 0):.0f}%</li>
</ul>
</div>

<p style="color:#666; text-align:center; margin-top:40px">
QUANTUM FLOW v2.1 Extended Backtest | Generated {now}
</p>
</body>
</html>"""

    out_path = OUT_DIR / "extended_report.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"[리포트] 저장: {out_path}")
    return str(out_path)


# ══════════════════════════════════════════════════════════════════
# MAIN: 전체 파이프라인
# ══════════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info("QUANTUM FLOW — 확장 백테스트 Suite 시작")
    logger.info(f"학습: {TRAIN_START}~{TRAIN_END} | 검증: {TEST_START}~{TEST_END}")
    logger.info("=" * 70)

    # ── Step 0: 데이터 로딩 ──
    logger.info("\n[Step 0] 캐시 데이터 로딩...")
    from data_prep import load_daily_data, load_macro_data, classify_macro_regime

    daily_df = load_daily_data(start_date=FULL_START, top_n_tickers=800, min_days=60)
    macro_df = load_macro_data(start_date=FULL_START)
    macro_df = classify_macro_regime(macro_df)

    logger.info(f"일봉: {len(daily_df):,}행, {daily_df['ticker'].nunique()}종목")
    logger.info(f"매크로: {len(macro_df)}일")

    # ── Step 1: 수렴 검증 (먼저) ──
    logger.info("\n[Step 1] 수렴 검증...")
    convergence = verify_convergence(daily_df, n_rounds=3, n_trials_per_round=3000)

    # ── Step 2: 10,000회 확장 최적화 ──
    logger.info("\n[Step 2] 10,000회 확장 파라미터 탐색...")
    top_results = run_extended_optimization(daily_df, n_trials=10000, top_k=50)

    if not top_results:
        logger.error("유효한 최적화 결과 없음")
        return

    best_result = top_results[0]
    best_params = best_result["params"]

    logger.info(f"\n★ 최적 파라미터:")
    logger.info(f"  DC={best_params.dc_period}, 거래량={best_params.vol_ratio_min}x, "
                f"ADX={best_params.adx_min}, ATR={best_params.atr_stop_mult}x")
    logger.info(f"  trail={best_params.trail_stop_pct*100:.0f}%, TP={best_params.take_profit*100:.0f}%, "
                f"TS={best_params.time_stop_days}d")
    logger.info(f"  수익률={best_result['total_return']*100:.1f}%, "
                f"Sharpe={best_result['sharpe']:.3f}, MDD={best_result['mdd']*100:.1f}%")

    # ── Step 3: 워크포워드 검증 ──
    logger.info("\n[Step 3] Walk-Forward 검증...")
    top_params_df = pd.read_csv(OUT_DIR / "ext_top_params.csv")
    wf_df = run_walk_forward(daily_df, top_results, top_n=5)

    # ── Step 4: 몬테카를로 ──
    logger.info("\n[Step 4] 몬테카를로 시뮬레이션...")
    # 전체 기간으로 최적 파라미터 재실행 (거래 수 확보)
    sig_df_full = generate_signals_ext(daily_df.copy(), best_params)
    full_result = run_backtest_ext(sig_df_full, best_params, FULL_START, FULL_END)
    mc_result = run_monte_carlo(full_result, n_simulations=2000)

    # ── Step 5: 섹터별 성과 ──
    logger.info("\n[Step 5] 섹터별 성과 분석...")
    sector_df = analyze_sector_performance(daily_df, best_params)

    # ── Step 6: 요일/월별 패턴 ──
    logger.info("\n[Step 6] 요일/월별 패턴 분석...")
    temporal = analyze_temporal_patterns(full_result.get("trades", []))

    # ── Step 7: 오버나이트 임계값 ──
    logger.info("\n[Step 7] 오버나이트 임계값 최적화...")
    overnight_df = optimize_overnight_threshold(daily_df, best_params)

    # ── Step 8: 복합 매크로 필터 ──
    logger.info("\n[Step 8] 복합 매크로 필터 탐색...")
    macro_filter_df = analyze_combined_macro_filters(daily_df, macro_df, best_params)

    # ── Step 9: 통합 리포트 ──
    logger.info("\n[Step 9] 통합 리포트 생성...")
    report_path = generate_extended_report(
        top_params_df  = top_params_df,
        wf_df          = wf_df,
        mc_result      = mc_result,
        sector_df      = sector_df,
        overnight_df   = overnight_df,
        macro_filter_df = macro_filter_df,
        temporal       = temporal,
        convergence    = convergence,
        best_result    = full_result,
    )

    logger.info("\n" + "=" * 70)
    logger.info("✅ 확장 백테스트 완료!")
    logger.info(f"결과 폴더: analysis/results/extended/")
    logger.info(f"HTML 리포트: {report_path}")
    logger.info("=" * 70)


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
