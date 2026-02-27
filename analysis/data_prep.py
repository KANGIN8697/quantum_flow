"""
data_prep.py — 일봉 CSV 로딩 + 지표 계산 + 매크로 다운로드
모든 분석 모듈의 공통 전처리 레이어
"""
import os, json, logging
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger("analysis.data_prep")

# ── 경로 설정 ──
BASE_DIR    = Path(__file__).parent.parent.parent  # collected_data 위 폴더
DATA_DIR    = BASE_DIR / "collected_data"
ALL_DAILY   = DATA_DIR / "ALL_daily.csv"
CACHE_DIR   = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# ── FRED 시리즈 ──
FRED_SERIES = {
    "VIX":      "VIXCLS",
    "DXY":      "DTWEXBGS",
    "TNX":      "DGS10",
    "SP500":    "SP500",
    "USDKRW":  "DEXKOUS",
    "FEDFUNDS": "FEDFUNDS",
    "T10Y2Y":  "T10Y2Y",
    "T10YIE":  "T10YIE",
    "CPI":     "CPIAUCSL",
    "PCE":     "PCEPI",
}

# ── yfinance 보완 시리즈 ──
YF_SERIES = {
    "VIX":    "^VIX",
    "SP500":  "^GSPC",
    "USDKRW": "KRW=X",
    "KOSPI":  "^KS11",
    "KOSDAQ": "^KQ11",
    "WTI":    "CL=F",
    "GOLD":   "GC=F",
}

# ══════════════════════════════════════════════════════════════
# 1. 일봉 데이터 로딩 + 기술지표 계산
# ══════════════════════════════════════════════════════════════

def load_daily_data(min_days: int = 120, start_date: str = "20200101",
                    top_n_tickers: int = 500) -> pd.DataFrame:
    """
    ALL_daily.csv 로딩 → 기술지표 계산
    - min_days: 지표 계산에 필요한 최소 거래일 수
    - start_date: 분석 시작일 (YYYYMMDD)
    - top_n_tickers: 거래대금 상위 N종목만 사용 (메모리 절약)
    반환: ticker별 OHLCV + 지표 포함 DataFrame
    """
    cache_file = CACHE_DIR / f"daily_prepared_{start_date}_top{top_n_tickers}.parquet"
    if cache_file.exists():
        logger.info(f"일봉 캐시 로드: {cache_file}")
        return pd.read_parquet(cache_file)

    logger.info("ALL_daily.csv 로딩 중...")
    df = pd.read_csv(ALL_DAILY, low_memory=False)

    # 날짜/ticker 정제
    df = df.dropna(subset=["date", "close", "open", "high", "low", "volume"])
    df["date"] = df["date"].astype(int).astype(str)
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)

    # 필수 컬럼만
    cols = ["ticker", "date", "open", "high", "low", "close", "volume", "거래대금"]
    avail = [c for c in cols if c in df.columns]
    df = df[avail].copy()
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    df = df[df["volume"] > 0]
    df = df[df["close"] > 500]   # 500원 미만 제거 (저가주)

    # 시작일 필터
    df = df[df["date"] >= start_date].copy()
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    logger.info(f"기본 정제 완료: {len(df):,}행, {df['ticker'].nunique()}종목")

    # ── 거래대금 상위 N종목 선택 (최근 1년 평균 기준) ──
    recent_cutoff = start_date  # 전체 기간 평균으로 상위 종목 선택
    recent = df[df["date"] >= recent_cutoff]
    if "거래대금" in recent.columns:
        top_tickers = (
            recent.groupby("ticker")["거래대금"]
            .mean().nlargest(top_n_tickers).index.tolist()
        )
    else:
        top_tickers = (
            recent.groupby("ticker")["volume"]
            .mean().nlargest(top_n_tickers).index.tolist()
        )
    df = df[df["ticker"].isin(top_tickers)].copy()
    logger.info(f"거래대금 상위 {top_n_tickers}종목 선택: {len(df):,}행")

    # 기술지표 계산 (ticker별 순차)
    logger.info("기술지표 계산 중 (ATR/ADX/Donchian/볼린저/RSI)...")
    result_chunks = []
    tickers = df["ticker"].unique()
    for i, ticker in enumerate(tickers):
        if i % 100 == 0:
            logger.info(f"  {i}/{len(tickers)} 종목 처리 중...")
        g = df[df["ticker"] == ticker].copy().reset_index(drop=True)
        if len(g) < min_days:
            continue
        g = _calc_indicators(g)
        result_chunks.append(g)

    out = pd.concat(result_chunks, ignore_index=True)
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    out.to_parquet(cache_file, index=False)
    logger.info(f"지표 계산 완료 → {len(out):,}행, {out['ticker'].nunique()}종목")
    return out


def _calc_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """단일 종목 DataFrame에 기술지표 추가"""
    c = g["close"]
    h = g["high"]
    l = g["low"]
    v = g["volume"]

    # ── 이동평균 ──
    for n in [5, 10, 20, 60, 120]:
        g[f"ma{n}"] = c.rolling(n).mean()

    # ── ATR ──
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    g["atr14"] = tr.rolling(14).mean()
    g["atr_pct"] = g["atr14"] / c  # ATR/종가 비율

    # ── 돈치안 채널 (진입: 상단, 청산: 하단) ── 대표 3개만
    for n in [20, 40, 60]:
        g[f"dc_high{n}"] = h.shift(1).rolling(n).max()   # 전일까지 n일 고가 (당일 돌파 판단)
        g[f"dc_low{n}"]  = l.shift(1).rolling(n).min()

    # ── 볼린저밴드 (20일) ──
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    g["bb_upper"] = bb_mid + 2 * bb_std
    g["bb_lower"] = bb_mid - 2 * bb_std
    g["bb_pct"]   = (c - bb_mid) / (2 * bb_std + 1e-9)  # -1~+1

    # ── RSI ──
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    g["rsi14"] = 100 - 100 / (1 + gain / (loss + 1e-9))

    # ── ADX (직접 계산) ──
    up   = h.diff()
    down = -l.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr14 = g["atr14"]
    plus_di  = 100 * pd.Series(plus_dm,  index=g.index).rolling(14).sum() / (atr14 * 14 + 1e-9)
    minus_di = 100 * pd.Series(minus_dm, index=g.index).rolling(14).sum() / (atr14 * 14 + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    g["adx14"] = dx.rolling(14).mean()
    g["plus_di"]  = plus_di
    g["minus_di"] = minus_di

    # ── 거래량 지표 ──
    g["vol_ma20"] = v.rolling(20).mean()
    g["vol_ratio"] = v / (g["vol_ma20"] + 1e-9)  # 거래량/20일평균

    # ── 수익률 (미래 N일 전진수익 — 레이블용) ──
    for fwd in [1, 3, 5, 10]:
        g[f"fwd_ret{fwd}"] = c.shift(-fwd) / c - 1  # 미래 수익률

    # ── 당일 수익률 ──
    g["ret1d"] = c.pct_change()
    g["ret5d"] = c.pct_change(5)
    g["ret20d"] = c.pct_change(20)

    return g


# ══════════════════════════════════════════════════════════════
# 2. 매크로 데이터 다운로드 (FRED + yfinance)
# ══════════════════════════════════════════════════════════════

def load_macro_data(start_date: str = "20200101", end_date: str = None) -> pd.DataFrame:
    """
    FRED + yfinance 매크로 시계열 → 날짜별 DataFrame
    캐시 존재 시 재사용
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    cache_file = CACHE_DIR / f"macro_{start_date}_{end_date}.parquet"
    if cache_file.exists():
        logger.info(f"매크로 캐시 로드: {cache_file}")
        return pd.read_parquet(cache_file)

    # 날짜 변환
    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

    fred_key = os.getenv("FRED_API_KEY", "")
    frames = {}

    # ── FRED ──
    if fred_key:
        logger.info("FRED 데이터 다운로드 중...")
        for name, sid in FRED_SERIES.items():
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={sid}&api_key={fred_key}&file_type=json"
                   f"&observation_start={sd}&observation_end={ed}")
            try:
                resp = requests.get(url, timeout=15)
                obs  = resp.json().get("observations", [])
                s = pd.Series(
                    {o["date"]: float(o["value"]) for o in obs if o["value"] != "."},
                    name=name
                )
                frames[name] = s
                logger.info(f"  FRED {name}: {len(s)}건")
            except Exception as e:
                logger.warning(f"  FRED {name} 실패: {e}")
    else:
        logger.warning("FRED_API_KEY 없음 — yfinance만 사용")

    # ── yfinance ──
    logger.info("yfinance 다운로드 중...")
    for name, sym in YF_SERIES.items():
        try:
            yf_df = yf.download(sym, start=sd, end=ed, progress=False)
            if yf_df.empty:
                continue
            s = yf_df["Close"].squeeze()
            s.index = s.index.strftime("%Y-%m-%d")
            s.name = f"yf_{name}"
            frames[f"yf_{name}"] = s
            logger.info(f"  yfinance {name}: {len(s)}건")
        except Exception as e:
            logger.warning(f"  yfinance {name} 실패: {e}")

    # ── 날짜 인덱스 합치기 ──
    all_dates = pd.date_range(start=sd, end=ed, freq="B")  # 영업일
    date_idx  = all_dates.strftime("%Y-%m-%d")
    macro = pd.DataFrame(index=date_idx)
    for name, s in frames.items():
        macro[name] = s.reindex(date_idx).ffill()  # 결측 전진채우기

    # 날짜 컬럼 (YYYYMMDD 형식)
    macro["date"] = macro.index.str.replace("-", "")
    macro = macro.dropna(how="all").reset_index(drop=True)

    macro.to_parquet(cache_file, index=False)
    logger.info(f"매크로 완료: {len(macro)}일, {macro.shape[1]-1}개 시리즈")
    return macro


# ══════════════════════════════════════════════════════════════
# 3. 매크로 레짐 분류
# ══════════════════════════════════════════════════════════════

def classify_macro_regime(macro: pd.DataFrame) -> pd.DataFrame:
    """
    각 날짜를 거시경제 레짐으로 분류
    레짐:
      0 = Risk-OFF  (VIX↑ + 달러↑ + 증시↓)
      1 = Neutral
      2 = Risk-ON   (VIX↓ + 달러↓/안정 + 증시↑)
    추가 파생 변수:
      - vix_level: VIX 구간 (low/mid/high/extreme)
      - trend_kospi: KOSPI 20일 추세 (up/flat/down)
      - dollar_strength: DXY 방향
    """
    m = macro.copy()

    # VIX 레벨
    vix = m.get("VIX", m.get("yf_VIX"))
    if vix is not None:
        m["vix_level"] = pd.cut(vix, bins=[0, 15, 20, 28, 999],
                                labels=["low", "mid", "high", "extreme"])
        m["vix_chg5d"] = vix.pct_change(5)

    # KOSPI 추세
    kospi = m.get("yf_KOSPI")
    if kospi is not None:
        ma20 = kospi.rolling(20).mean()
        m["kospi_above_ma20"] = (kospi > ma20).astype(int)
        m["kospi_ret5d"] = kospi.pct_change(5)
        m["kospi_ret20d"] = kospi.pct_change(20)

    # 달러 강도
    dxy = m.get("DXY", m.get("yf_DXY"))
    if dxy is not None:
        m["dxy_chg5d"] = dxy.pct_change(5)
        m["dollar_strong"] = (m["dxy_chg5d"] > 0.01).astype(int)

    # 금리 방향
    tnx = m.get("TNX")
    if tnx is not None:
        m["tnx_chg20d"] = tnx.pct_change(20)
        m["yield_rising"] = (m["tnx_chg20d"] > 0.02).astype(int)

    # ── 레짐 점수 산출 ──
    score = pd.Series(0.0, index=m.index)
    if vix is not None:
        score += np.where(vix < 15, 2,
                 np.where(vix < 20, 1,
                 np.where(vix < 28, -1, -2)))
        score -= m.get("vix_chg5d", pd.Series(0, index=m.index)).fillna(0) * 10
    if kospi is not None and "kospi_ret5d" in m.columns:
        score += m["kospi_ret5d"].fillna(0) * 50
    if dxy is not None and "dxy_chg5d" in m.columns:
        score -= m["dxy_chg5d"].fillna(0) * 20

    m["regime_score"] = score
    m["regime"] = pd.cut(score, bins=[-999, -1, 1, 999],
                         labels=["risk_off", "neutral", "risk_on"])

    return m


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    print("=== 데이터 전처리 테스트 ===")
    # 일봉 로딩 (소규모 테스트)
    df = load_daily_data(start_date="20220101")
    print(f"일봉: {len(df):,}행, {df['ticker'].nunique()}종목")
    print(df[["ticker","date","close","adx14","vol_ratio","fwd_ret5"]].head())

    # 매크로 로딩
    macro = load_macro_data(start_date="20220101")
    print(f"\n매크로: {len(macro)}일, 컬럼: {macro.columns.tolist()}")

    # 레짐 분류
    macro = classify_macro_regime(macro)
    print("\n레짐 분포:")
    print(macro["regime"].value_counts())
