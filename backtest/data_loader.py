"""
data_loader.py — 키움 CSV + FRED 매크로 과거 데이터 로딩
"""

import os
import json
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("backtest.data_loader")

# ── FRED / yfinance 시리즈 정의 ──
FRED_SERIES = {
    "VIX": "VIXCLS", "DXY": "DTWEXBGS", "TNX": "DGS10",
    "SP500": "SP500", "USDKRW": "DEXKOUS", "FEDFUNDS": "FEDFUNDS",
    "T10Y2Y": "T10Y2Y", "T10YIE": "T10YIE",
    "CPI": "CPIAUCSL", "CORE_CPI": "CPILFESL", "PCE": "PCEPI",
    "M2": "WM2NS", "FED_ASSETS": "WALCL",
    "ICSA": "ICSA", "UNRATE": "UNRATE", "UMCSENT": "UMCSENT",
}

YF_SYMBOLS = {
    "VIX": "^VIX", "DXY": "DX-Y.NYB", "TNX": "^TNX",
    "SP500": "^GSPC", "USDKRW": "USDKRW=X", "KOSPI": "^KS11",
}


# ══════════════════════════════════════════════════════════════
# 1. 키움 CSV 로딩
# ══════════════════════════════════════════════════════════════

def load_all_daily_csv(csv_dir: str) -> Dict[str, pd.DataFrame]:
    """collected_data/daily/ 전체 CSV 로딩 → {ticker: DataFrame}"""
    daily_dir = os.path.join(csv_dir, "daily")
    if not os.path.isdir(daily_dir):
        logger.error(f"일봉 폴더 없음: {daily_dir}")
        return {}

    all_data = {}
    csv_files = sorted(f for f in os.listdir(daily_dir) if f.endswith(".csv"))
    logger.info(f"일봉 CSV 로딩: {len(csv_files)}개 파일")

    for fname in csv_files:
        ticker = fname.replace(".csv", "")
        try:
            df = pd.read_csv(os.path.join(daily_dir, fname), encoding="utf-8-sig")
            col_map = {}
            for c in df.columns:
                cl = c.lower().strip()
                if cl in ("date", "날짜", "일자"):   col_map[c] = "date"
                elif cl in ("open", "시가"):         col_map[c] = "open"
                elif cl in ("high", "고가"):         col_map[c] = "high"
                elif cl in ("low", "저가"):          col_map[c] = "low"
                elif cl in ("close", "종가", "현재가"): col_map[c] = "close"
                elif cl in ("volume", "거래량"):     col_map[c] = "volume"
            df = df.rename(columns=col_map)

            if not {"date", "open", "high", "low", "close", "volume"}.issubset(df.columns):
                continue

            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
            df = df.dropna(subset=["date"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce").abs()
            df = df.sort_values("date").reset_index(drop=True)

            if len(df) >= 30:
                all_data[ticker] = df
        except Exception as e:
            logger.warning(f"  {fname} 로딩 실패: {e}")

    logger.info(f"로딩 완료: {len(all_data)}개 종목")
    return all_data


def get_trading_dates(all_data: Dict[str, pd.DataFrame]) -> List[pd.Timestamp]:
    """거래일 목록 추출"""
    longest = max(all_data, key=lambda t: len(all_data[t]))
    return sorted(all_data[longest]["date"].unique())


# ══════════════════════════════════════════════════════════════
# 2. FRED / yfinance 과거 데이터 (캐싱)
# ══════════════════════════════════════════════════════════════

def download_fred_history(start_date: str, end_date: str,
                          cache_path: str = "backtest/cache/fred_history.json") -> Dict:
    """FRED 전체 시리즈 기간 다운로드 + JSON 캐싱"""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            cached = json.load(f)
        if cached.get("start") == start_date and cached.get("end") == end_date:
            logger.info("FRED 데이터 캐시 로드")
            return cached["data"]

    import requests
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        logger.error("FRED_API_KEY 미설정")
        return {}

    result = {}
    for name, sid in FRED_SERIES.items():
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={sid}&api_key={fred_key}&file_type=json"
               f"&observation_start={start_date}&observation_end={end_date}")
        try:
            resp = requests.get(url, timeout=15)
            obs = resp.json().get("observations", [])
            result[name] = {o["date"]: float(o["value"])
                            for o in obs if o["value"] != "."}
            logger.info(f"  FRED {name}: {len(result[name])}건")
        except Exception as e:
            logger.warning(f"  FRED {name} 실패: {e}")
            result[name] = {}

    with open(cache_path, "w") as f:
        json.dump({"start": start_date, "end": end_date, "data": result}, f)
    return result


def download_yf_history(start_date: str, end_date: str,
                        cache_path: str = "backtest/cache/yf_history.json") -> Dict:
    """yfinance 시장 지표 기간 다운로드 + 캐싱"""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            cached = json.load(f)
        if cached.get("start") == start_date and cached.get("end") == end_date:
            logger.info("yfinance 데이터 캐시 로드")
            return cached["data"]

    import yfinance as yf
    result = {}
    for name, symbol in YF_SYMBOLS.items():
        try:
            df = yf.download(symbol, start=start_date, end=end_date,
                             progress=False)
            if df is not None and not df.empty:
                series = {}
                for idx, row in df.iterrows():
                    close = row["Close"]
                    if hasattr(close, "iloc"):
                        close = close.iloc[0]
                    series[str(idx.date())] = round(float(close), 4)
                result[name] = series
                logger.info(f"  yfinance {name}: {len(series)}건")
        except Exception as e:
            logger.warning(f"  yfinance {name} 실패: {e}")
            result[name] = {}

    with open(cache_path, "w") as f:
        json.dump({"start": start_date, "end": end_date, "data": result}, f)
    return result


# ══════════════════════════════════════════════════════════════
# 3. 날짜 기준 데이터 추출
# ══════════════════════════════════════════════════════════════

def _nearest(series: Dict[str, float], target: str, offset_days: int = 0):
    """시계열에서 target 이전 가장 가까운 값"""
    if not series:
        return 0.0, ""
    target_dt = datetime.strptime(target, "%Y-%m-%d").date() - timedelta(days=offset_days)
    best_d, best_v = "", 0.0
    for d_str, val in series.items():
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        if d <= target_dt and (not best_d or d > datetime.strptime(best_d, "%Y-%m-%d").date()):
            best_d, best_v = d_str, val
    return best_v, best_d


def get_macro_on_date(fred_data: Dict, yf_data: Dict, target_date: str) -> Dict:
    """특정 날짜 기준 매크로 데이터 → Agent1 입력 형태"""
    result = {}
    for name, series in {**fred_data, **yf_data}.items():
        val, d = _nearest(series, target_date)
        prev, _ = _nearest(series, target_date, offset_days=7)
        chg = ((val - prev) / prev * 100) if prev else 0
        result[name] = {"value": round(val, 4), "date": d, "change_pct": round(chg, 2)}
    return result


def get_volume_top_on_date(all_data: Dict[str, pd.DataFrame],
                           target_date: pd.Timestamp, n: int = 50) -> List[dict]:
    """특정 날짜 거래량 상위 N종목"""
    results = []
    for ticker, df in all_data.items():
        row = df[df["date"] == target_date]
        if row.empty:
            continue
        r = row.iloc[0]
        if r["close"] < 2000 or r["volume"] < 500000:
            continue
        idx = row.index[0]
        prev_close = df.iloc[idx - 1]["close"] if idx > 0 else r["close"]
        chg = ((r["close"] - prev_close) / prev_close * 100) if prev_close else 0
        results.append({
            "code": ticker, "name": ticker,
            "volume": int(r["volume"]), "price": int(r["close"]),
            "change_pct": round(chg, 2),
        })
    results.sort(key=lambda x: x["volume"], reverse=True)
    return results[:n]


def get_stock_ohlcv(all_data: Dict[str, pd.DataFrame], ticker: str,
                    target_date: pd.Timestamp, lookback: int = 25):
    """종목의 target_date 기준 과거 lookback일 OHLCV"""
    if ticker not in all_data:
        return None
    sub = all_data[ticker][all_data[ticker]["date"] <= target_date].tail(lookback)
    return sub.reset_index(drop=True) if len(sub) >= 5 else None


def get_forward_return(all_data: Dict[str, pd.DataFrame], ticker: str,
                       entry_date: pd.Timestamp, days: int = 5):
    """entry_date 이후 days 거래일 수익률(%)"""
    if ticker not in all_data:
        return None
    df = all_data[ticker]
    entry = df[df["date"] == entry_date]
    if entry.empty:
        return None
    future = df[df["date"] > entry_date].head(days)
    if future.empty:
        return None
    entry_close = entry.iloc[0]["close"]
    exit_close = future.iloc[-1]["close"]
    return round((exit_close - entry_close) / entry_close * 100, 2) if entry_close else None
