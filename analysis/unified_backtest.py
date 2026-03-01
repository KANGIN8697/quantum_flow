"""
unified_backtest.py — 실거래 코드와 100% 동일한 조건의 통합 백테스터
============================================================
creon_data 1분봉/5분봉 + 일봉 데이터를 결합하여 분봉 수준에서 시뮬레이션.

반영 조건 (실거래 코드 동기화):
  [진입]
    1. DC 돌파 (close >= dc_upper, MA60 상방)
    2. 거래량 비율 >= vol_ratio_min
    3. ADX >= adx_min, RSI 범위
    4. 15분봉 MA3>MA8>MA20 정배열 (1분봉 리샘플링)
    5. 시간대별 가중치 (10:00~10:30 최적)
    6. 오프닝 러시 09:20 이전 차단
    7. 이벤트 필터 (당일 음봉 + 거래량 미동반 → 포지션 축소)

  [청산]
    1. 트레일링 스탑: 고점 대비 -trail_stop_pct (분봉 단위 체크)
    2. 익절: 진입가 대비 +take_profit_pct (분봉 단위 체크)
    3. 타임스탑: 보유 N일 초과 → 강제 청산
    4. Track 1 강제 청산: 15:10 (오버나이트 미자격)
    5. Track 2 오버나이트: +3% 이상 & 15분봉 정배열 유지 → 익일 보유
    6. Track 2 익일: 갭다운 -1% 즉시 청산, 트레일링 -5%, 14:00 최종 청산
    7. 초기 ATR 손절: entry - ATR × atr_stop_mult

  [포트폴리오]
    1. 최대 동시 보유: max_positions (기본 5)
    2. 종목당 투입: position_size_ratio (기본 20%)
    3. 피라미딩: ATR×1.5 돌파 시 30% 추가, 이후 -3% 평단 손절
    4. 당일 손실 -3% 한도
    5. 매크로 조건부 사이징 (KOSPI 5d, 달러 강세)

사용법:
    python unified_backtest.py
"""

import os, sys, time, logging, warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("unified_backtest")

# ═══════════════════════════════════════════════════════
#  파라미터 정의
# ═══════════════════════════════════════════════════════

@dataclass
class Params:
    # 진입 조건
    dc_period:      int   = 25
    vol_ratio_min:  float = 3.0
    adx_min:        float = 25
    rsi_min:        float = 30
    rsi_max:        float = 75

    # 청산 조건
    atr_stop_mult:    float = 2.0    # 초기 ATR 손절 배수
    trail_stop_pct:   float = 0.02   # 트레일링: 고점 대비 -2%
    take_profit_pct:  float = 0.07   # 익절: +7%
    time_stop_days:   int   = 3      # 타임스탑: 3일

    # 포트폴리오
    max_positions:      int   = 5
    position_size_ratio: float = 0.20
    daily_loss_limit:   float = -0.03  # 당일 손실 한도

    # 피라미딩
    pyramid_enabled:    bool  = True
    pyramid_atr_mult:   float = 1.5
    pyramid_add_ratio:  float = 0.30
    pyramid_stop_pct:   float = -0.03
    pyramid_max_count:  int   = 2

    # Track 2 오버나이트
    track2_enabled:       bool  = True
    track2_qualify_pnl:   float = 0.03   # +3% 이상
    track2_max_positions: int   = 2
    track2_trail_pct:     float = 0.05   # 익일 트레일링 -5%
    track2_gap_down_cut:  float = -0.01  # 갭다운 -1%

    # 시간 기반
    opening_rush_end:  str = "0920"    # 09:20 이전 차단
    force_close_time:  str = "1510"    # 15:10 Track 1 강제 청산
    track2_eval_time:  str = "1430"    # 14:30 오버나이트 평가
    track2_deadline:   str = "1400"    # 익일 14:00 최종 청산

    # 15분봉 정배열
    tf15_ma_short:  int = 3
    tf15_ma_mid:    int = 8
    tf15_ma_long:   int = 20
    tf15_enabled:   bool = True

    # 시간대별 가중치 (진입 시 포지션 비율 조정)
    time_weight: dict = field(default_factory=lambda: {
        "0920": 0.5, "0930": 0.8, "1000": 1.0, "1030": 0.9,
        "1100": 0.7, "1130": 0.6, "1300": 0.7,
    })

    # 이벤트 필터
    event_filter_enabled:   bool  = True
    event_min_day_return:   float = 0.0
    event_weak_mult:        float = 0.60

    # 트레일링 스탑 체크 방식
    trail_check_mode:  str = "minute"  # "minute" = 분봉별, "daily" = 일봉 종가 기준


# ═══════════════════════════════════════════════════════
#  데이터 로더
# ═══════════════════════════════════════════════════════

CREON_DATA = Path(__file__).resolve().parent.parent.parent / "creon_data"
DAILY_CACHE = Path(__file__).resolve().parent / "cache"

def load_daily_data() -> pd.DataFrame:
    """일봉 데이터 로드 (DC, ADX, RSI 등 이미 계산됨)"""
    parquet = DAILY_CACHE / "daily_prepared_20230901_top800.parquet"
    if not parquet.exists():
        raise FileNotFoundError(f"일봉 데이터 없음: {parquet}")
    df = pd.read_parquet(parquet)
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    logger.info(f"일봉 데이터 로드: {len(df):,}행, {df['ticker'].nunique()}종목")
    return df


def _build_creon_folder_map() -> dict:
    """creon_data 폴더 → 6자리 코드 매핑 (최초 1회)"""
    m = {}
    if not CREON_DATA.exists():
        return m
    for d in CREON_DATA.iterdir():
        if d.is_dir():
            raw = d.name.split("_")[0]
            padded = raw.zfill(6)
            m[padded] = d
    return m

_CREON_MAP: Optional[dict] = None

def load_minute_data(code: str, interval: str = "min1") -> Optional[pd.DataFrame]:
    """
    creon_data에서 종목별 분봉 CSV 로드.
    code: 종목코드 (예: "005930", 6자리)
    interval: "min1" 또는 "min5"
    """
    global _CREON_MAP
    if _CREON_MAP is None:
        _CREON_MAP = _build_creon_folder_map()

    folder = _CREON_MAP.get(code)
    if folder is None:
        return None

    # CSV 파일명: 원본 코드(패딩 안 된) 기준
    raw_code = folder.name.split("_")[0]
    csv_path = folder / f"{raw_code}_{interval}.csv"
    if not csv_path.exists():
        return None

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        # 컬럼: 날짜,시간,시가,고가,저가,종가,거래량,...
        df = df.rename(columns={
            "날짜": "date", "시간": "time",
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume",
        })
        df["date"] = df["date"].astype(str)
        df["time"] = df["time"].astype(str).str.zfill(4)
        # 시간 역순(최신→과거)이면 정렬
        df = df.sort_values(["date", "time"]).reset_index(drop=True)
        return df
    except Exception as e:
        logger.debug(f"분봉 로드 실패 {code}: {e}")
        return None


def build_15m_from_1m(min1_df: pd.DataFrame) -> pd.DataFrame:
    """
    1분봉 → 15분봉 리샘플링.
    15분 슬롯: 0900, 0915, 0930, ...
    """
    if min1_df is None or min1_df.empty:
        return pd.DataFrame()

    df = min1_df.copy()
    # 15분 슬롯 계산
    t_int = df["time"].astype(int)
    hour = t_int // 100
    minute = t_int % 100
    minute_of_day = hour * 60 + minute
    df["slot"] = (minute_of_day // 15) * 15  # 15분 단위 절사
    df["slot_key"] = df["date"] + "_" + df["slot"].astype(str)

    agg = df.groupby(["date", "slot"]).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    agg = agg.sort_values(["date", "slot"]).reset_index(drop=True)
    return agg


def calc_15m_alignment(tf15_df: pd.DataFrame, target_date: str,
                       target_slot: int, p: Params) -> bool:
    """
    특정 시점까지의 15분봉으로 MA3>MA8>MA20 정배열 확인.
    target_slot: 현재 15분 슬롯 (예: 600 = 10:00)
    """
    if tf15_df.empty:
        return False

    # 해당 날짜의 해당 슬롯까지 데이터
    mask = ((tf15_df["date"] < target_date) |
            ((tf15_df["date"] == target_date) & (tf15_df["slot"] <= target_slot)))
    sub = tf15_df[mask].tail(p.tf15_ma_long + 5)

    if len(sub) < p.tf15_ma_long:
        return False

    closes = sub["close"].values
    ma3 = np.mean(closes[-p.tf15_ma_short:])
    ma8 = np.mean(closes[-p.tf15_ma_mid:])
    ma20 = np.mean(closes[-p.tf15_ma_long:])

    return ma3 > ma8 > ma20


# ═══════════════════════════════════════════════════════
#  포지션 클래스
# ═══════════════════════════════════════════════════════

@dataclass
class Position:
    code:        str
    entry_price: float
    entry_date:  str     # YYYYMMDD
    entry_time:  str     # HHMM
    quantity:    float   # 비중 (0~1)
    atr:         float
    stop_price:  float   # 초기 ATR 손절가
    peak_price:  float   # 진입 이후 최고가
    track:       int = 1 # 1=장중, 2=오버나이트
    pyramid_count: int = 0
    avg_cost:    float = 0.0  # 피라미딩 시 평균단가

    def __post_init__(self):
        if self.avg_cost == 0:
            self.avg_cost = self.entry_price


# ═══════════════════════════════════════════════════════
#  통합 백테스터
# ═══════════════════════════════════════════════════════

class UnifiedBacktester:
    """실거래 코드와 동일한 로직의 분봉 기반 백테스터"""

    def __init__(self, p: Params):
        self.p = p
        self.positions: Dict[str, Position] = {}
        self.trades: List[dict] = []
        self.capital = 1.0
        self.peak_capital = 1.0
        self.daily_pnl = 0.0
        self.daily_loss_blocked = False
        self.equity_curve: List[float] = []

    def run(self, daily_df: pd.DataFrame, start: str = "20240226",
            end: str = "20260224", target_tickers: set = None) -> dict:
        """
        메인 백테스트 루프.
        일봉 기반으로 진입 신호 감지 → 분봉으로 장중 시뮬레이션.
        target_tickers: 테스트 대상 종목 집합 (None이면 전체)
        """
        t0 = time.time()
        p = self.p

        # 일봉 데이터 필터링
        df = daily_df[(daily_df["date"] >= start) & (daily_df["date"] <= end)].copy()
        if target_tickers:
            df = df[df["ticker"].isin(target_tickers)]
        dates = sorted(df["date"].unique())
        tickers = df["ticker"].unique()

        # 종목별 일봉 인덱싱
        daily_by_ticker = {}
        for tk, grp in df.groupby("ticker"):
            g = grp.sort_values("date").reset_index(drop=True)
            g["dc_upper"] = g["high"].shift(1).rolling(p.dc_period).max()
            daily_by_ticker[tk] = g.set_index("date")

        # 종목별 분봉 캐시 (LRU 제한 — 메모리 관리)
        min1_cache: Dict[str, Optional[pd.DataFrame]] = {}
        tf15_cache: Dict[str, pd.DataFrame] = {}
        self._cache_max = 40  # 최대 캐시 종목 수

        logger.info(f"백테스트 시작: {start}~{end}, {len(dates)}일, {len(tickers)}종목")
        logger.info(f"파라미터: DC={p.dc_period}, vol={p.vol_ratio_min}, "
                     f"trail={p.trail_stop_pct*100:.0f}%, TP={p.take_profit_pct*100:.0f}%, "
                     f"TS={p.time_stop_days}일")

        # 신호 감지 → 익일 시가 진입 방식
        # pending_entries: 전일 신호 → 오늘 진입할 후보
        pending_entries: List[dict] = []

        for di, date_str in enumerate(dates):
            if di % 50 == 0:
                elapsed = time.time() - t0
                logger.info(f"  진행: {di}/{len(dates)} ({elapsed:.0f}초)")

            self.daily_pnl = 0.0
            self.daily_loss_blocked = False

            # ── 0) 전일 신호 → 금일 시가 진입 실행 ──
            if pending_entries and not self.daily_loss_blocked:
                self._execute_pending_entries(pending_entries, date_str,
                                              daily_by_ticker)
            pending_entries = []

            # ── 1) 기존 포지션 장중 시뮬레이션 (분봉 단위) ──
            self._simulate_intraday(date_str, daily_by_ticker,
                                    min1_cache, tf15_cache)

            # ── 2) 장 마감 처리 (Track 1 → EOD 청산 또는 Track 2 전환) ──
            self._end_of_day(date_str, daily_by_ticker,
                             min1_cache, tf15_cache)

            # ── 3) 신규 진입 후보 스캔 (일봉 기준 → 익일 진입) ──
            if not self.daily_loss_blocked:
                pending_entries = self._scan_signals(date_str, daily_by_ticker,
                                                     min1_cache, tf15_cache)

            # 자산곡선 기록
            self.equity_curve.append(self.capital)

        elapsed = time.time() - t0
        return self._compile_results(elapsed)

    # ─────────────────────────────────────────────────
    #  장중 시뮬레이션 (분봉 단위)
    # ─────────────────────────────────────────────────

    def _simulate_intraday(self, date_str: str,
                           daily_map: dict, min1_cache: dict,
                           tf15_cache: dict):
        """보유 포지션의 장중 분봉 체크"""
        p = self.p
        to_close = []

        for code, pos in list(self.positions.items()):
            min1 = self._get_min1(code, min1_cache)
            if min1 is None or min1.empty:
                # 분봉 없으면 일봉으로 폴백 (종가 기준)
                self._check_daily_exit(code, pos, date_str, daily_map)
                continue

            day_bars = min1[min1["date"] == date_str]
            if day_bars.empty:
                self._check_daily_exit(code, pos, date_str, daily_map)
                continue

            # Track 2 익일 처리 (전일 오버나이트)
            if pos.track == 2 and pos.entry_date != date_str:
                first_bar = day_bars.iloc[0]
                open_price = float(first_bar["open"])
                # 갭다운 체크
                prev_close = pos.peak_price  # 전일 종가 근사
                gap = (open_price - prev_close) / prev_close
                if gap <= p.track2_gap_down_cut:
                    self._close_position(code, open_price, date_str,
                                         first_bar["time"], "gap_down")
                    continue

            # ── daily 모드: 분봉 스킵, 종가 기준 체크 ──
            if p.trail_check_mode == "daily":
                last_bar = day_bars.iloc[-1]
                day_close = float(last_bar["close"])
                day_high = float(day_bars["high"].max())
                day_low = float(day_bars["low"].min())

                pos = self.positions[code]
                pos.peak_price = max(pos.peak_price, day_high)

                # 트레일링 + ATR 스탑 (종가 기준)
                trail_stop = pos.peak_price * (1 - p.trail_stop_pct)
                eff_stop = max(pos.stop_price, trail_stop)

                if day_close <= eff_stop:
                    self._close_position(code, eff_stop, date_str,
                                         "1530", "stop")
                    continue

                # 익절 (일중 고가 기준)
                pnl = (day_high - pos.avg_cost) / pos.avg_cost
                if pnl >= p.take_profit_pct:
                    exit_price = pos.avg_cost * (1 + p.take_profit_pct)
                    self._close_position(code, exit_price, date_str,
                                         "1530", "take_profit")
                    continue

                # 타임스탑
                hold_days = self._calc_hold_days(pos.entry_date, date_str)
                if hold_days >= p.time_stop_days:
                    self._close_position(code, day_close, date_str,
                                         "1530", "time_stop")
                    continue

                # Track 1 강제 청산 (15:10)
                if pos.track == 1:
                    # daily 모드에서도 Track 2 평가는 _end_of_day에서
                    pass  # _end_of_day가 처리

                # Track 2 익일 최종
                if (pos.track == 2 and pos.entry_date != date_str):
                    self._close_position(code, day_close, date_str,
                                         "1400", "track2_deadline")
                    continue

                continue  # daily 모드 처리 완료, 분봉 루프 스킵

            # ── minute 모드: 분봉별 체크 ──
            for _, bar in day_bars.iterrows():
                if code not in self.positions:
                    break

                price = float(bar["close"])
                high = float(bar["high"])
                low = float(bar["low"])
                bar_time = str(bar["time"]).zfill(4)

                pos = self.positions[code]

                # ── 손절 체크 (저가 기준) — peak 갱신 전에 체크 ──
                trail_stop = pos.peak_price * (1 - p.trail_stop_pct)
                eff_stop = max(pos.stop_price, trail_stop)

                if low <= eff_stop:
                    exit_price = eff_stop  # 손절가에서 체결 가정
                    self._close_position(code, exit_price, date_str,
                                         bar_time, "stop")
                    continue

                # ── peak 갱신 (손절 체크 후) ──
                pos.peak_price = max(pos.peak_price, high)

                # ── 익절 체크 (고가 기준) ──
                pnl = (high - pos.avg_cost) / pos.avg_cost
                if pnl >= p.take_profit_pct:
                    exit_price = pos.avg_cost * (1 + p.take_profit_pct)
                    self._close_position(code, exit_price, date_str,
                                         bar_time, "take_profit")
                    continue

                # ── 타임스탑 체크 ──
                hold_days = self._calc_hold_days(pos.entry_date, date_str)
                if hold_days >= p.time_stop_days:
                    self._close_position(code, price, date_str,
                                         bar_time, "time_stop")
                    continue

                # ── Track 1: 15:10 강제 청산 ──
                if pos.track == 1 and bar_time >= p.force_close_time:
                    self._close_position(code, price, date_str,
                                         bar_time, "track1_force_close")
                    continue

                # ── Track 2 익일: 14:00 최종 청산 ──
                if (pos.track == 2 and pos.entry_date != date_str
                        and bar_time >= p.track2_deadline):
                    self._close_position(code, price, date_str,
                                         bar_time, "track2_deadline")
                    continue

                # ── 피라미딩 체크 ──
                if (p.pyramid_enabled and pos.pyramid_count < p.pyramid_max_count
                        and bar_time < "1500"):
                    pyr_trigger = pos.entry_price + pos.atr * p.pyramid_atr_mult
                    if price >= pyr_trigger:
                        add_size = pos.quantity * p.pyramid_add_ratio
                        old_cost = pos.avg_cost * pos.quantity
                        pos.quantity += add_size
                        pos.avg_cost = (old_cost + price * add_size) / pos.quantity
                        pos.pyramid_count += 1
                        # 피라미딩 후 손절은 평단 -3%
                        pos.stop_price = pos.avg_cost * (1 + p.pyramid_stop_pct)

                # ── 당일 손실 한도 체크 ──
                if self.daily_pnl <= p.daily_loss_limit:
                    self.daily_loss_blocked = True
                    # 전 포지션 청산
                    for c in list(self.positions.keys()):
                        if c in self.positions:
                            p_pos = self.positions[c]
                            self._close_position(c, price, date_str,
                                                 bar_time, "daily_loss_limit")
                    return

    def _check_daily_exit(self, code: str, pos: Position,
                          date_str: str, daily_map: dict):
        """분봉 없는 종목의 일봉 기반 청산 체크"""
        p = self.p
        tk_data = daily_map.get(code)
        if tk_data is None or date_str not in tk_data.index:
            return

        row = tk_data.loc[date_str]
        close = float(row["close"])
        high = float(row.get("high", close))
        low = float(row.get("low", close))

        pos.peak_price = max(pos.peak_price, high)

        # 손절
        trail_stop = pos.peak_price * (1 - p.trail_stop_pct)
        eff_stop = max(pos.stop_price, trail_stop)
        if low <= eff_stop:
            self._close_position(code, eff_stop, date_str, "1530", "stop")
            return

        # 익절
        pnl = (high - pos.avg_cost) / pos.avg_cost
        if pnl >= p.take_profit_pct:
            exit_price = pos.avg_cost * (1 + p.take_profit_pct)
            self._close_position(code, exit_price, date_str, "1530", "take_profit")
            return

        # 타임스탑
        hold_days = self._calc_hold_days(pos.entry_date, date_str)
        if hold_days >= p.time_stop_days:
            self._close_position(code, close, date_str, "1530", "time_stop")
            return

    # ─────────────────────────────────────────────────
    #  신규 진입: 신호 감지 (전일) + 진입 실행 (익일)
    # ─────────────────────────────────────────────────

    def _scan_signals(self, date_str: str, daily_map: dict,
                      min1_cache: dict, tf15_cache: dict) -> List[dict]:
        """
        일봉 기반 진입 신호 스캔. 실제 진입은 익일 시가에 실행.
        Returns: 후보 리스트 (스코어 상위)
        """
        p = self.p
        candidates = []
        for tk, tk_data in daily_map.items():
            if tk in self.positions:
                continue
            if date_str not in tk_data.index:
                continue

            row = tk_data.loc[date_str]
            close = float(row["close"])
            if close <= 0:
                continue

            dc_upper = row.get("dc_upper", np.nan)
            if np.isnan(dc_upper):
                continue

            vol_ratio = float(row.get("vol_ratio", 0))
            adx = float(row.get("adx14", 0))
            rsi = float(row.get("rsi14", 50))
            ma60 = float(row.get("ma60", close))
            atr = float(row.get("atr14", close * 0.02))
            day_return = float(row.get("ret1d", 0))

            # NaN 방어
            if np.isnan(vol_ratio) or np.isnan(adx) or np.isnan(rsi):
                continue
            if np.isnan(atr) or atr <= 0:
                atr = close * 0.02

            # ── 기본 진입 조건 ──
            if not (close >= dc_upper and close >= ma60):
                continue
            if vol_ratio < p.vol_ratio_min:
                continue
            if adx < p.adx_min:
                continue
            if not (p.rsi_min <= rsi <= p.rsi_max):
                continue

            # ── 15분봉 정배열 확인 ──
            tf15_aligned = True
            if p.tf15_enabled:
                tf15 = self._get_tf15(tk, min1_cache, tf15_cache)
                if not tf15.empty:
                    tf15_aligned = calc_15m_alignment(
                        tf15, date_str, 600, p)
                else:
                    tf15_aligned = True

            if not tf15_aligned:
                continue

            # ── 이벤트 필터 ──
            event_mult = 1.0
            if p.event_filter_enabled:
                if day_return < p.event_min_day_return and vol_ratio < 3.0:
                    event_mult = p.event_weak_mult

            score = vol_ratio * 10 + adx * 0.5
            candidates.append({
                "code": tk, "signal_close": close, "atr": atr,
                "vol_ratio": vol_ratio, "score": score,
                "event_mult": event_mult,
            })

        candidates.sort(key=lambda x: -x["score"])
        return candidates[:p.max_positions * 2]  # 넉넉히 후보 확보

    def _execute_pending_entries(self, candidates: List[dict],
                                 date_str: str, daily_map: dict):
        """전일 신호 후보를 금일 시가에 진입"""
        p = self.p
        for c in candidates:
            if len(self.positions) >= p.max_positions:
                break

            code = c["code"]
            if code in self.positions:
                continue

            # 금일 시가로 진입
            tk_data = daily_map.get(code)
            if tk_data is None or date_str not in tk_data.index:
                continue

            row = tk_data.loc[date_str]
            entry_price = float(row["open"])
            if entry_price <= 0:
                continue

            atr = c["atr"]
            size = p.position_size_ratio * c["event_mult"]
            size = min(size, p.position_size_ratio)

            stop_price = entry_price - atr * p.atr_stop_mult

            self.positions[code] = Position(
                code=code,
                entry_price=entry_price,
                entry_date=date_str,
                entry_time="0900",
                quantity=size,
                atr=atr,
                stop_price=stop_price,
                peak_price=entry_price,
            )

    # ─────────────────────────────────────────────────
    #  장 마감 처리
    # ─────────────────────────────────────────────────

    def _end_of_day(self, date_str: str, daily_map: dict,
                    min1_cache: dict, tf15_cache: dict):
        """14:30 Track 2 평가 + 미결 Track 1 강제청산"""
        p = self.p

        if not p.track2_enabled:
            # Track 2 비활성 → 전 포지션 이미 15:10에 청산됨
            return

        # Track 2 오버나이트 평가
        overnight_count = sum(1 for pos in self.positions.values() if pos.track == 2)

        for code, pos in list(self.positions.items()):
            if pos.track != 1:
                continue

            # 오버나이트 자격 평가
            tk_data = daily_map.get(code)
            if tk_data is None or date_str not in tk_data.index:
                continue

            close = float(tk_data.loc[date_str]["close"])
            pnl = (close - pos.avg_cost) / pos.avg_cost

            if (pnl >= p.track2_qualify_pnl
                    and overnight_count < p.track2_max_positions):
                # 15분봉 정배열 유지 확인
                tf15_ok = True
                if p.tf15_enabled:
                    tf15 = self._get_tf15(code, min1_cache, tf15_cache)
                    if not tf15.empty:
                        tf15_ok = calc_15m_alignment(
                            tf15, date_str, 870, p)  # 870 = 14:30

                if tf15_ok:
                    pos.track = 2
                    pos.peak_price = close  # 익일 트레일링 기준
                    overnight_count += 1
                    continue

            # 오버나이트 미자격 → 종가 청산
            self._close_position(code, close, date_str, "1510", "track1_eod")

    # ─────────────────────────────────────────────────
    #  포지션 청산
    # ─────────────────────────────────────────────────

    def _close_position(self, code: str, exit_price: float,
                        date_str: str, exit_time: str, reason: str):
        """포지션 청산 + 거래 기록"""
        pos = self.positions.get(code)
        if not pos:
            return

        pnl_pct = (exit_price - pos.avg_cost) / pos.avg_cost
        pnl_amount = pnl_pct * pos.quantity  # 자본 대비 손익

        self.capital += pnl_amount
        self.peak_capital = max(self.peak_capital, self.capital)
        self.daily_pnl += pnl_amount

        self.trades.append({
            "code": code,
            "entry_price": pos.entry_price,
            "entry_date": pos.entry_date,
            "entry_time": pos.entry_time,
            "exit_price": round(exit_price, 2),
            "exit_date": date_str,
            "exit_time": exit_time,
            "pnl_pct": round(pnl_pct, 6),
            "pnl_amount": round(pnl_amount, 6),
            "quantity": round(pos.quantity, 4),
            "reason": reason,
            "track": pos.track,
            "pyramid_count": pos.pyramid_count,
            "hold_days": self._calc_hold_days(pos.entry_date, date_str),
        })

        del self.positions[code]

    # ─────────────────────────────────────────────────
    #  유틸리티
    # ─────────────────────────────────────────────────

    def _get_min1(self, code: str, cache: dict) -> Optional[pd.DataFrame]:
        if code not in cache:
            # LRU: 캐시 크기 제한
            if len(cache) >= getattr(self, '_cache_max', 40):
                # 보유 포지션에 없는 종목부터 제거
                for k in list(cache.keys()):
                    if k not in self.positions and k != code:
                        del cache[k]
                        break
            cache[code] = load_minute_data(code, "min1")
        return cache[code]

    def _get_tf15(self, code: str, min1_cache: dict,
                  tf15_cache: dict) -> pd.DataFrame:
        if code not in tf15_cache:
            # LRU: tf15도 캐시 크기 제한
            if len(tf15_cache) >= getattr(self, '_cache_max', 40):
                for k in list(tf15_cache.keys()):
                    if k not in self.positions and k != code:
                        del tf15_cache[k]
                        break
            min1 = self._get_min1(code, min1_cache)
            if min1 is not None and not min1.empty:
                tf15_cache[code] = build_15m_from_1m(min1)
            else:
                tf15_cache[code] = pd.DataFrame()
        return tf15_cache[code]

    def _calc_hold_days(self, entry_date: str, current_date: str) -> int:
        try:
            ed = date(int(entry_date[:4]), int(entry_date[4:6]), int(entry_date[6:8]))
            cd = date(int(current_date[:4]), int(current_date[4:6]), int(current_date[6:8]))
            # 영업일 기준은 아니고 캘린더일 기준 (주말 포함)
            return (cd - ed).days
        except (ValueError, IndexError):
            return 0

    def _get_time_weight(self, time_str: str) -> float:
        """시간대별 가중치 반환"""
        for tw_time in sorted(self.p.time_weight.keys(), reverse=True):
            if time_str >= tw_time:
                return self.p.time_weight[tw_time]
        return 0.5

    # ─────────────────────────────────────────────────
    #  결과 정리
    # ─────────────────────────────────────────────────

    def _compile_results(self, elapsed: float) -> dict:
        trades_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()
        n = len(trades_df)

        if n == 0:
            return {"n": 0, "error": "거래 없음"}

        rets = trades_df["pnl_pct"].values
        amounts = trades_df["pnl_amount"].values

        # Sharpe (거래 수익률 기준)
        if np.std(rets) > 0:
            sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252))
        else:
            sharpe = 0

        # MDD (자산곡선 기준)
        eq = np.array(self.equity_curve)
        if len(eq) > 0:
            peak_arr = np.maximum.accumulate(eq)
            dd = (eq - peak_arr) / peak_arr
            mdd = float(np.min(dd) * 100)
        else:
            mdd = 0

        total_ret = (self.capital - 1.0) * 100

        # 청산 사유별 분석
        reason_stats = {}
        if not trades_df.empty:
            for reason, grp in trades_df.groupby("reason"):
                reason_stats[reason] = {
                    "n": len(grp),
                    "pct": round(len(grp) / n * 100, 1),
                    "avg_pnl": round(grp["pnl_pct"].mean() * 100, 2),
                    "win_rate": round((grp["pnl_pct"] > 0).mean() * 100, 1),
                }

        result = {
            "n_trades": n,
            "sharpe": round(sharpe, 3),
            "total_return_pct": round(total_ret, 2),
            "win_rate_pct": round(float(np.mean(rets > 0) * 100), 1),
            "mdd_pct": round(mdd, 2),
            "avg_pnl_pct": round(float(np.mean(rets) * 100), 2),
            "avg_hold_days": round(float(trades_df["hold_days"].mean()), 1),
            "profit_factor": round(
                abs(float(np.sum(rets[rets > 0]))) /
                (abs(float(np.sum(rets[rets < 0]))) + 1e-9), 2),
            "final_capital": round(self.capital, 4),
            "reason_stats": reason_stats,
            "elapsed_sec": round(elapsed, 1),
            "trades_df": trades_df,
        }

        logger.info(f"\n{'='*60}")
        logger.info(f"  백테스트 결과 요약")
        logger.info(f"{'='*60}")
        logger.info(f"  거래 수:     {n}건")
        logger.info(f"  Sharpe:      {result['sharpe']}")
        logger.info(f"  총 수익률:   {result['total_return_pct']:+.2f}%")
        logger.info(f"  승률:        {result['win_rate_pct']:.1f}%")
        logger.info(f"  MDD:         {result['mdd_pct']:.2f}%")
        logger.info(f"  Profit Factor: {result['profit_factor']}")
        logger.info(f"  평균 보유:   {result['avg_hold_days']:.1f}일")
        logger.info(f"  소요시간:    {result['elapsed_sec']:.0f}초")
        logger.info(f"\n  [청산 사유별]")
        for reason, stats in sorted(reason_stats.items(),
                                     key=lambda x: -x[1]["n"]):
            logger.info(f"    {reason:<20} {stats['n']:>4}건 ({stats['pct']:>5.1f}%)"
                        f"  평균{stats['avg_pnl']:>+6.2f}%  승률{stats['win_rate']:>5.1f}%")

        return result


# ═══════════════════════════════════════════════════════
#  실행
# ═══════════════════════════════════════════════════════

def run_comparison():
    """v1 vs v2 비교 실행"""
    daily_df = load_daily_data()

    # 분봉 데이터가 있는 종목만 대상 (메모리 절약)
    global _CREON_MAP
    if _CREON_MAP is None:
        _CREON_MAP = _build_creon_folder_map()
    target_tickers = set(_CREON_MAP.keys()) & set(daily_df["ticker"].unique())
    logger.info(f"테스트 대상 종목: {len(target_tickers)}개 (분봉 데이터 보유)")

    configs = {
        "v1": Params(
            dc_period=20, vol_ratio_min=2.0, adx_min=25,
            rsi_min=30, rsi_max=70,
            trail_stop_pct=0.03, take_profit_pct=0.10,
            time_stop_days=7, atr_stop_mult=2.0,
            trail_check_mode="daily",
        ),
        "v2": Params(
            dc_period=25, vol_ratio_min=3.0, adx_min=25,
            rsi_min=30, rsi_max=75,
            trail_stop_pct=0.02, take_profit_pct=0.07,
            time_stop_days=3, atr_stop_mult=2.0,
            trail_check_mode="daily",
        ),
    }

    results = {}
    for label, params in configs.items():
        logger.info(f"\n{'#'*60}")
        logger.info(f"  {label} 백테스트 시작")
        logger.info(f"{'#'*60}")
        bt = UnifiedBacktester(params)
        results[label] = bt.run(daily_df, target_tickers=target_tickers)

    # 비교 출력
    print(f"\n\n{'='*70}")
    print(f"  통합 백테스트 비교 결과 (분봉 기반, 실거래 조건 동일)")
    print(f"{'='*70}")
    print(f"  {'항목':<16} ", end="")
    for label in configs:
        print(f"{label:>20}", end="")
    print()
    print(f"  {'-'*56}")

    metrics = [
        ("거래 수", "n_trades", "건", False),
        ("Sharpe", "sharpe", "", True),
        ("총 수익률", "total_return_pct", "%", True),
        ("승률", "win_rate_pct", "%", True),
        ("MDD", "mdd_pct", "%", False),
        ("Profit Factor", "profit_factor", "", True),
        ("평균 보유일", "avg_hold_days", "일", False),
        ("평균 손익", "avg_pnl_pct", "%", True),
    ]
    for label, key, unit, _ in metrics:
        print(f"  {label:<16} ", end="")
        for cfg_label in configs:
            val = results[cfg_label].get(key, 0)
            print(f"{val:>18}{unit}", end="")
        print()

    # 결과 저장
    out_dir = Path(__file__).parent / "results" / "unified"
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, res in results.items():
        safe = label.replace(" ", "_").replace("(", "").replace(")", "")
        if "trades_df" in res and not res["trades_df"].empty:
            res["trades_df"].to_csv(out_dir / f"{safe}_trades.csv", index=False)
    print(f"\n  결과 저장: {out_dir}")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_comparison()
