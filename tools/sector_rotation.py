# tools/sector_rotation.py
# Cross-sector fund flow and rotation detection
# Compares momentum, volume, and foreign/institutional flow across sectors

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("sector_rotation")
KST = timezone(timedelta(hours=9))

# Korean market sector ETF mapping for rotation analysis
SECTOR_ETFS = {
    "semiconductor": {"etf": "091160", "name": "KODEX Semiconductor"},
    "battery": {"etf": "305720", "name": "KODEX 2nd Battery Industry"},
    "bio": {"etf": "244580", "name": "KODEX Bio"},
    "auto": {"etf": "091170", "name": "KODEX Auto"},
    "steel": {"etf": "117680", "name": "KODEX Steel"},
    "construction": {"etf": "117700", "name": "KODEX Construction"},
    "finance": {"etf": "091180", "name": "KODEX Finance"},
    "it": {"etf": "098560", "name": "KODEX IT"},
    "chemical": {"etf": "117690", "name": "KODEX Chemical"},
    "energy": {"etf": "117460", "name": "KODEX Energy & Chemical"},
    "shipbuilding": {"etf": "140710", "name": "KODEX Transport"},
    "media": {"etf": "098560", "name": "KODEX Media & Telecom"},
}

# Stock-to-sector mapping (representative stocks)
STOCK_SECTOR_MAP = {
    "005930": "semiconductor", "000660": "semiconductor",  # Samsung, SK Hynix
    "051910": "chemical", "006400": "battery",              # LG Chem, Samsung SDI
    "035420": "it", "035720": "it",                         # NAVER, Kakao
    "005380": "auto", "000270": "auto",                     # Hyundai Motor, Kia
    "105560": "finance", "055550": "finance",               # KB Financial, Shinhan
    "068270": "bio", "207940": "bio",                       # Celltrion, Samsung Bio
    "005490": "steel", "004020": "steel",                   # POSCO, Hyundai Steel
    "034730": "it", "028260": "it",                         # SK, Samsung C&T
    "009150": "construction", "000720": "construction",     # Samsung Electro, Hyundai E&C
    "010130": "chemical", "011170": "chemical",             # Korea Zinc, Lotte Chemical
    "373220": "battery", "247540": "battery",               # LG Energy Sol, ECOPRO BM
}


def get_sector_for_stock(code: str) -> Optional[str]:
    """Get sector classification for a given stock code."""
    return STOCK_SECTOR_MAP.get(code)


def calc_sector_scores(price_data_func) -> dict:
    """
    Calculate momentum score for each sector using ETF price data.

    Parameters:
        price_data_func: Callable that takes (code, period) and returns DataFrame
                        (use stock_eval_tools.fetch_price_data)

    Returns:
        dict of {sector: score_dict} with momentum metrics
    """
    sector_scores = {}

    for sector, info in SECTOR_ETFS.items():
        etf_code = info["etf"]
        try:
            df = price_data_func(etf_code, period="3mo")
            if df is None or len(df) < 20:
                continue

            close = df["Close"]

            # Calculate multiple timeframe returns
            ret_5d = ((close.iloc[-1] / close.iloc[-5]) - 1) * 100 if len(close) >= 5 else 0
            ret_20d = ((close.iloc[-1] / close.iloc[-20]) - 1) * 100 if len(close) >= 20 else 0
            ret_60d = ((close.iloc[-1] / close.iloc[0]) - 1) * 100

            # Volume trend (recent 5d avg vs 20d avg)
            vol = df["Volume"]
            vol_5d_avg = vol.iloc[-5:].mean() if len(vol) >= 5 else vol.mean()
            vol_20d_avg = vol.iloc[-20:].mean() if len(vol) >= 20 else vol.mean()
            vol_ratio = vol_5d_avg / vol_20d_avg if vol_20d_avg > 0 else 1.0

            # Moving average alignment
            ma5 = close.rolling(5).mean().iloc[-1] if len(close) >= 5 else close.iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else close.iloc[-1]
            ma_aligned = ma5 > ma20

            # Composite momentum score
            # Weight: 5d return (40%), 20d return (30%), vol ratio (20%), MA alignment (10%)
            momentum_score = (
                ret_5d * 0.4 +
                ret_20d * 0.3 +
                (vol_ratio - 1.0) * 20 * 0.2 +  # Normalize vol ratio
                (10 if ma_aligned else -10) * 0.1
            )

            sector_scores[sector] = {
                "name": info["name"],
                "etf_code": etf_code,
                "return_5d_pct": round(ret_5d, 2),
                "return_20d_pct": round(ret_20d, 2),
                "return_60d_pct": round(ret_60d, 2),
                "volume_ratio": round(vol_ratio, 2),
                "ma_aligned": ma_aligned,
                "momentum_score": round(momentum_score, 2),
                "current_price": round(float(close.iloc[-1]), 0),
            }

        except Exception as e:
            logger.warning(f"Failed to calc sector score for {sector}: {e}")
            continue

    return sector_scores


def detect_rotation(sector_scores: dict) -> dict:
    """
    Detect sector rotation patterns from sector scores.

    Parameters:
        sector_scores: Output from calc_sector_scores()

    Returns:
        dict with rotation analysis
    """
    if len(sector_scores) < 3:
        return {"status": "insufficient_data", "sectors_analyzed": len(sector_scores)}

    # Sort sectors by momentum score
    ranked = sorted(
        sector_scores.items(),
        key=lambda x: x[1]["momentum_score"],
        reverse=True
    )

    # Identify leaders and laggards
    leaders = [(s, d) for s, d in ranked[:3]]
    laggards = [(s, d) for s, d in ranked[-3:]]

    # Detect rotation signals
    rotation_signals = []

    for sector, data in ranked:
        ret_5d = data["return_5d_pct"]
        ret_20d = data["return_20d_pct"]

        # Accelerating sector: short-term >> long-term
        if ret_5d > 3.0 and ret_5d > ret_20d * 1.5:
            rotation_signals.append({
                "type": "ACCELERATING",
                "sector": sector,
                "detail": f"{sector} accelerating: 5d={ret_5d:+.1f}% vs 20d={ret_20d:+.1f}%"
            })

        # Decelerating sector: was strong but weakening
        if ret_20d > 5.0 and ret_5d < 0:
            rotation_signals.append({
                "type": "DECELERATING",
                "sector": sector,
                "detail": f"{sector} decelerating: 20d={ret_20d:+.1f}% but 5d={ret_5d:+.1f}%"
            })

        # Volume surge with price move = new interest
        if data["volume_ratio"] > 1.5 and abs(ret_5d) > 2.0:
            direction = "inflow" if ret_5d > 0 else "outflow"
            rotation_signals.append({
                "type": f"VOLUME_{direction.upper()}",
                "sector": sector,
                "detail": f"{sector}: volume {data['volume_ratio']:.1f}x with {direction}"
            })

    # Overall rotation strength
    leader_avg = sum(d["momentum_score"] for _, d in leaders) / len(leaders)
    laggard_avg = sum(d["momentum_score"] for _, d in laggards) / len(laggards)
    rotation_strength = leader_avg - laggard_avg

    return {
        "status": "analyzed",
        "timestamp": datetime.now(KST).isoformat(),
        "sectors_analyzed": len(sector_scores),
        "leaders": [
            {"sector": s, "score": d["momentum_score"], "return_5d": d["return_5d_pct"]}
            for s, d in leaders
        ],
        "laggards": [
            {"sector": s, "score": d["momentum_score"], "return_5d": d["return_5d_pct"]}
            for s, d in laggards
        ],
        "rotation_signals": rotation_signals,
        "rotation_strength": round(rotation_strength, 2),
        "strong_rotation": rotation_strength > 10.0,
    }


def get_sector_recommendation(rotation_data: dict) -> dict:
    """
    Generate sector-based trading recommendations from rotation analysis.

    Parameters:
        rotation_data: Output from detect_rotation()

    Returns:
        dict with sector preferences for trading
    """
    if rotation_data.get("status") != "analyzed":
        return {"prefer_sectors": [], "avoid_sectors": [], "reason": "Insufficient data"}

    prefer = []
    avoid = []

    # Prefer leaders with positive momentum
    for leader in rotation_data.get("leaders", []):
        if leader["score"] > 0 and leader["return_5d"] > 0:
            prefer.append(leader["sector"])

    # Avoid laggards with negative momentum
    for laggard in rotation_data.get("laggards", []):
        if laggard["score"] < 0 and laggard["return_5d"] < 0:
            avoid.append(laggard["sector"])

    # Check for accelerating signals
    for signal in rotation_data.get("rotation_signals", []):
        if signal["type"] == "ACCELERATING":
            sector = signal["sector"]
            if sector not in prefer:
                prefer.append(sector)

    return {
        "prefer_sectors": prefer,
        "avoid_sectors": avoid,
        "rotation_strength": rotation_data.get("rotation_strength", 0),
        "strong_rotation": rotation_data.get("strong_rotation", False),
        "signal_count": len(rotation_data.get("rotation_signals", [])),
    }


def format_rotation_text(rotation_data: dict) -> str:
    """Format rotation analysis for Telegram notification."""
    if rotation_data.get("status") != "analyzed":
        return "Sector rotation: insufficient data"

    lines = ["=== SECTOR ROTATION ==="]

    # Leaders
    lines.append("Top sectors:")
    for l in rotation_data.get("leaders", []):
        lines.append(f"  + {l['sector']}: score={l['score']:.1f}, 5d={l['return_5d']:+.1f}%")

    # Laggards
    lines.append("Bottom sectors:")
    for l in rotation_data.get("laggards", []):
        lines.append(f"  - {l['sector']}: score={l['score']:.1f}, 5d={l['return_5d']:+.1f}%")

    # Signals
    signals = rotation_data.get("rotation_signals", [])
    if signals:
        lines.append(f"Rotation signals ({len(signals)}):")
        for s in signals[:5]:
            lines.append(f"  > {s['detail']}")

    strength = rotation_data.get("rotation_strength", 0)
    strong = rotation_data.get("strong_rotation", False)
    lines.append(f"Rotation strength: {strength:.1f} {'(STRONG)' if strong else '(moderate)'}")

    return "\n".join(lines)
