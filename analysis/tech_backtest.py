"""
tech_backtest.py — Track 2: 기술적 매매로직 파라미터 최적화 백테스트
현재 로직: 돈치안 채널 돌파 + 거래량 필터 + ADX + ATR 손절 + 트레일링 스탑
일봉 데이터로 신호 근사, 수천 회 파라미터 탐색
"""
import logging, itertools, random
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
from pathlib import Path

logger = logging.getLogger("analysis.tech_backtest")

# ══════════════════════════════════════════════════════════════
# 파라미터 정의
# ══════════════════════════════════════════════════════════════

@dataclass
class TechParams:
    """매매 로직 파라미터 세트"""
    # 돈치안 채널
    dc_period: int = 20          # N일 고점 돌파 기간

    # 거래량 필터
    vol_ratio_min: float = 2.0   # 거래량/20일평균 최소 배율

    # ADX 필터
    adx_min: float = 25.0        # ADX 최소값

    # RSI 필터
    rsi_min: float = 40.0        # RSI 최소값 (과매도 회피)
    rsi_max: float = 75.0        # RSI 최대값 (과매수 진입 금지)

    # 손절 (ATR 배수)
    atr_stop_mult: float = 2.0   # ATR × mult = 손절폭

    # 트레일링 스탑 / 오버나이트
    overnight_min_ret: float = 0.07   # 오버나이트 보유 최소 수익률
    trail_stop_pct: float = 0.05      # 트레일링 스탑 %

    # 이익실현 / 타임스탑
    take_profit: float = 0.15    # 이익실현 기준 (15%)
    time_stop_days: int = 10     # N일 미진입 → 기계적 매도

    # 포트폴리오
    max_positions: int = 5
    position_size: float = 0.20  # 종목당 비중

    def key(self) -> str:
        return (f"dc{self.dc_period}_vol{self.vol_ratio_min}"
                f"_adx{self.adx_min}_rsi{self.rsi_min}-{self.rsi_max}"
                f"_atr{self.atr_stop_mult}_trail{self.trail_stop_pct}"
                f"_tp{self.take_profit}_ts{self.time_stop_days}")


# ══════════════════════════════════════════════════════════════
# 신호 생성
# ══════════════════════════════════════════════════════════════

def generate_signals(df: pd.DataFrame, params: TechParams) -> pd.DataFrame:
    """
    종목별 매수 신호 생성
    df: ticker별 일봉 + 지표 (data_prep.load_daily_data 결과)
    반환: 신호 컬럼이 추가된 DataFrame
    """
    dc_col = f"dc_high{params.dc_period}"
    if dc_col not in df.columns:
        # 지표 없으면 즉석 계산
        df = df.copy()
        df[dc_col] = df.groupby("ticker")["high"].transform(
            lambda x: x.shift(1).rolling(params.dc_period).max()
        )

    # 돈치안 돌파
    df["sig_dc"]  = df["close"] > df[dc_col]

    # 거래량 필터
    df["sig_vol"] = df["vol_ratio"] >= params.vol_ratio_min

    # ADX 필터
    df["sig_adx"] = df["adx14"] >= params.adx_min

    # RSI 필터
    df["sig_rsi"] = (df["rsi14"] >= params.rsi_min) & (df["rsi14"] <= params.rsi_max)

    # MA 상향 필터 (60일선 위)
    df["sig_ma"]  = df["close"] > df["ma60"]

    # 종합 진입 신호
    df["entry_signal"] = (
        df["sig_dc"] & df["sig_vol"] & df["sig_adx"] & df["sig_rsi"] & df["sig_ma"]
    )

    return df


# ══════════════════════════════════════════════════════════════
# 백테스트 엔진
# ══════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker: str
    entry_date: str
    entry_price: float
    stop_price: float      # ATR 기반 초기 손절
    peak_price: float      # 트레일링 스탑 기준 최고가
    allocated: float = 0.0 # 실제 투입 금액 (진입 시 cash에서 차감)
    hold_days: int = 0


@dataclass
class BacktestResult:
    params: TechParams
    total_return: float = 0.0
    sharpe: float = 0.0
    mdd: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    avg_hold_days: float = 0.0
    profit_factor: float = 0.0
    trades: List[dict] = field(default_factory=list)


def run_backtest(df: pd.DataFrame, params: TechParams,
                 start_date: str = "20230901",
                 end_date: str = "20260224",
                 initial_capital: float = 100_000_000) -> BacktestResult:
    """
    일봉 기반 포트폴리오 백테스트
    - 매일 신호 체크 → 진입/청산
    - 최대 5종목 동시 보유
    - ATR 손절 + 트레일링 스탑 + 이익실현 + 타임스탑
    """
    result = BacktestResult(params=params)

    # 날짜 필터
    data = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
    if data.empty:
        return result

    # ── 속도 최적화: 날짜별 dict 미리 빌드 ──
    # {date: {ticker: {col: val}}} 구조로 인덱싱
    data_dict: Dict[str, Dict] = {}
    needed_cols = ["close", "atr14", "vol_ratio", "entry_signal"]
    needed_cols = [c for c in needed_cols if c in data.columns]
    for date, grp in data.groupby("date"):
        data_dict[date] = grp.set_index("ticker")[needed_cols].to_dict("index")

    # 거래일 목록
    dates = sorted(data_dict.keys())

    # 포지션 딕셔너리 {ticker: Position}
    positions: Dict[str, Position] = {}

    # 자본 추적 — cash(현금) + 보유 포지션 평가액 = 총자산
    cash = initial_capital
    equity_curve = []
    completed_trades = []

    for date in dates:
        day_dict = data_dict.get(date, {})   # {ticker: {col: val}}

        # ── 1. 기존 포지션 청산 체크 ──
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
                completed_trades.append({
                    "ticker": ticker, "entry_date": pos.entry_date,
                    "exit_date": date, "entry_price": pos.entry_price,
                    "exit_price": exit_price, "ret": ret, "pnl": pnl,
                    "hold_days": pos.hold_days, "exit_reason": exit_reason,
                })
                to_close.append(ticker)

        for t in to_close:
            del positions[t]

        # ── 2. 신호 종목 진입 ──
        if len(positions) < params.max_positions:
            # 신호 종목 추출 및 거래량 정렬
            sig_tickers = [
                (t, r.get("vol_ratio", 0))
                for t, r in day_dict.items()
                if r.get("entry_signal") == True and t not in positions
            ]
            sig_tickers.sort(key=lambda x: x[1], reverse=True)

            slots = params.max_positions - len(positions)
            for ticker, _ in sig_tickers[:slots]:
                row   = day_dict[ticker]
                price = float(row["close"])
                # 총자산 기준 position_size 할당
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
                )

        # ── 3. 자본 스냅샷 (현금 + 보유 포지션 평가액) ──
        pos_value = 0.0
        for ticker, pos in positions.items():
            row = day_dict.get(ticker)
            cur_price = float(row["close"]) if row else pos.entry_price
            pos_value += pos.allocated * (cur_price / pos.entry_price)
        total_equity = cash + pos_value
        equity_curve.append({"date": date, "equity": total_equity})

    # ── 성과 지표 계산 ──
    if not completed_trades:
        return result

    eq = pd.DataFrame(equity_curve).set_index("date")["equity"]
    daily_ret = eq.pct_change().dropna()

    result.total_return = float(eq.iloc[-1] / eq.iloc[0] - 1)
    result.mdd = float(_calc_mdd(eq))
    result.sharpe = float(
        daily_ret.mean() / (daily_ret.std() + 1e-9) * np.sqrt(252)
    )

    rets = [t["ret"] for t in completed_trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    result.win_rate = len(wins) / len(rets) if rets else 0
    result.trade_count = len(rets)
    result.avg_hold_days = np.mean([t["hold_days"] for t in completed_trades])
    gross_profit = sum(wins) if wins else 0
    gross_loss   = abs(sum(losses)) if losses else 1e-9
    result.profit_factor = gross_profit / gross_loss
    result.trades = completed_trades

    return result


def _calc_mdd(equity: pd.Series) -> float:
    """최대낙폭 계산"""
    peak  = equity.cummax()
    dd    = (equity - peak) / peak
    return float(dd.min())


# ══════════════════════════════════════════════════════════════
# 파라미터 최적화
# ══════════════════════════════════════════════════════════════

PARAM_GRID = {
    "dc_period":         [20, 40, 60],
    "vol_ratio_min":     [1.5, 2.0, 2.5, 3.0],
    "adx_min":           [20.0, 25.0, 30.0],
    "rsi_min":           [35.0, 40.0, 45.0],
    "rsi_max":           [70.0, 75.0, 80.0],
    "atr_stop_mult":     [1.5, 2.0, 2.5, 3.0],
    "trail_stop_pct":    [0.03, 0.05, 0.07],
    "take_profit":       [0.10, 0.15, 0.20, 0.25],
    "time_stop_days":    [5, 10, 15, 20],
}

def optimize(df: pd.DataFrame,
             n_trials: int = 2000,
             start_date: str = "20220101",
             end_date: str = "20250101",
             mode: str = "random",        # "random" | "grid"
             top_k: int = 20,
             out_dir: Path = None) -> List[BacktestResult]:
    """
    수천 회 파라미터 탐색 → 상위 결과 반환
    mode="random": 랜덤 샘플링 (권장, n_trials 제어)
    mode="grid"  : 전수 탐색 (조합 수: ~10만개 이상)
    """
    out_dir = out_dir or Path("analysis/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"파라미터 최적화 시작: {mode} mode, {n_trials}회")

    # 신호 사전 계산 (dc_period 별로)
    logger.info("신호 사전 계산 중...")
    df_signals = {}
    for dc in PARAM_GRID["dc_period"]:
        p = TechParams(dc_period=dc)
        df_signals[dc] = generate_signals(df.copy(), p)

    if mode == "random":
        param_list = _sample_random(n_trials)
    else:
        param_list = _grid_search(n_trials)

    results = []
    for i, params in enumerate(param_list):
        if i % 100 == 0:
            logger.info(f"  {i}/{len(param_list)} 진행 중...")

        # 해당 dc_period의 신호 df 사용
        sig_df = df_signals.get(params.dc_period, df)
        r = run_backtest(sig_df, params, start_date, end_date)
        if r.trade_count >= 20:  # 최소 거래 수 필터
            results.append(r)

    # 샤프 기준 정렬
    results.sort(key=lambda x: x.sharpe, reverse=True)

    logger.info(f"최적화 완료: {len(results)}개 유효 결과")
    _save_results(results[:top_k], out_dir)
    return results[:top_k]


def _sample_random(n: int) -> List[TechParams]:
    params = []
    for _ in range(n):
        p = TechParams(
            dc_period       = random.choice(PARAM_GRID["dc_period"]),
            vol_ratio_min   = random.choice(PARAM_GRID["vol_ratio_min"]),
            adx_min         = random.choice(PARAM_GRID["adx_min"]),
            rsi_min         = random.choice(PARAM_GRID["rsi_min"]),
            rsi_max         = random.choice(PARAM_GRID["rsi_max"]),
            atr_stop_mult   = random.choice(PARAM_GRID["atr_stop_mult"]),
            trail_stop_pct  = random.choice(PARAM_GRID["trail_stop_pct"]),
            take_profit     = random.choice(PARAM_GRID["take_profit"]),
            time_stop_days  = random.choice(PARAM_GRID["time_stop_days"]),
        )
        params.append(p)
    return params


def _grid_search(max_n: int) -> List[TechParams]:
    keys = list(PARAM_GRID.keys())
    all_combos = list(itertools.product(*PARAM_GRID.values()))
    random.shuffle(all_combos)
    all_combos = all_combos[:max_n]
    result = []
    for combo in all_combos:
        kv = dict(zip(keys, combo))
        result.append(TechParams(**kv))
    return result


def _save_results(results: List[BacktestResult], out_dir: Path):
    rows = []
    for r in results:
        p = r.params
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
            "total_return":    round(r.total_return * 100, 2),
            "sharpe":          round(r.sharpe, 3),
            "mdd":             round(r.mdd * 100, 2),
            "win_rate":        round(r.win_rate * 100, 1),
            "trade_count":     r.trade_count,
            "avg_hold_days":   round(r.avg_hold_days, 1),
            "profit_factor":   round(r.profit_factor, 3),
        })
    df = pd.DataFrame(rows)
    out_path = out_dir / "top_params.csv"
    df.to_csv(out_path, index=False)
    logger.info(f"결과 저장: {out_path}")
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from data_prep import load_daily_data
    print("일봉 데이터 로딩...")
    df = load_daily_data(start_date="20220101")
    print(f"로드 완료: {len(df):,}행")

    # 기본 파라미터 단일 백테스트
    p = TechParams()
    sig_df = generate_signals(df, p)
    print(f"신호 종목: {sig_df['entry_signal'].sum()}건")
    r = run_backtest(sig_df, p, "20220101", "20250101")
    print(f"수익률: {r.total_return*100:.1f}%  샤프: {r.sharpe:.2f}  MDD: {r.mdd*100:.1f}%  거래:{r.trade_count}  승률:{r.win_rate*100:.1f}%")
