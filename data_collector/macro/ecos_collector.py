"""
ecos_collector.py - 한국은행 ECOS 거시경제 지표 수집
Bank of Korea Economic Statistics System Open API
"""
import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ── 설정 ──────────────────────────────────────────────
ECOS_API_KEY = os.getenv("ECOS_API_KEY", "")
BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
OUTPUT_DIR = Path("outputs/data/macro")

# 수집 대상 지표
INDICATORS = {
    "base_rate": {
        "stat_code": "722Y001",
        "item_code": "0101000",
        "cycle": "M",
        "name": "기준금리",
    },
    "cpi": {
        "stat_code": "901Y009",
        "item_code": "0",
        "cycle": "M",
        "name": "소비자물가지수(전년동월비)",
    },
    "unemployment": {
        "stat_code": "901Y027",
        "item_code": "3130000",
        "cycle": "M",
        "name": "실업률",
    },
    "gdp_growth": {
        "stat_code": "200Y002",
        "item_code": "10111",
        "cycle": "Q",
        "name": "GDP성장률(전기비)",
    },
    "current_account": {
        "stat_code": "301Y013",
        "item_code": "000000",
        "cycle": "M",
        "name": "경상수지",
    },
    "export_growth": {
        "stat_code": "403Y003",
        "item_code": "000000",
        "cycle": "M",
        "name": "수출증가율",
    },
    "m2": {
        "stat_code": "101Y003",
        "item_code": "BBGA00",
        "cycle": "M",
        "name": "M2(광의통화)",
    },
}


def fetch_ecos_data(stat_code: str, item_code: str, cycle: str,
                    start_date: str, end_date: str) -> list:
    """ECOS API에서 특정 통계 데이터를 조회합니다."""
    if not ECOS_API_KEY:
        print("  ⚠️ ECOS_API_KEY 미설정 - 스킵")
        return []

    url = (
        f"{BASE_URL}/{ECOS_API_KEY}/json/kr/1/100"
        f"/{stat_code}/{cycle}/{start_date}/{end_date}/{item_code}"
    )

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if "StatisticSearch" in data:
            return data["StatisticSearch"]["row"]
        elif "RESULT" in data:
            print(f"  ⚠️ API 응답: {data['RESULT']['MESSAGE']}")
            return []
        else:
            print(f"  ⚠️ 예상치 못한 응답 형식")
            return []

    except requests.exceptions.RequestException as e:
        print(f"  ❌ API 요청 실패: {e}")
        return []
    except (KeyError, json.JSONDecodeError) as e:
        print(f"  ❌ 응답 파싱 실패: {e}")
        return []


def collect_all(months_back: int = 24) -> dict:
    """모든 ECOS 지표를 수집합니다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=months_back * 30)
    start_date = start_dt.strftime("%Y%m")
    end_date = end_dt.strftime("%Y%m")

    results = {}

    print(f"\n{'='*50}")
    print(f"  ECOS 거시경제 지표 수집")
    print(f"  기간: {start_date} ~ {end_date}")
    print(f"{'='*50}")

    for key, cfg in INDICATORS.items():
        print(f"\n📊 {cfg['name']} ({key})...")
        rows = fetch_ecos_data(
            cfg["stat_code"], cfg["item_code"], cfg["cycle"],
            start_date, end_date
        )

        if rows:
            df = pd.DataFrame(rows)
            results[key] = {
                "name": cfg["name"],
                "count": len(rows),
                "latest_date": rows[-1].get("TIME", ""),
                "latest_value": rows[-1].get("DATA_VALUE", ""),
                "data": rows,
            }
            print(f"  ✅ {len(rows)}건 수집 | 최신: {results[key]['latest_date']} = {results[key]['latest_value']}")
        else:
            results[key] = {"name": cfg["name"], "count": 0, "data": []}
            print(f"  ⚠️ 데이터 없음")

    # 결과 저장
    output_file = OUTPUT_DIR / "ecos_macro.json"
    save_data = {
        "collected_at": datetime.now().isoformat(),
        "period": f"{start_date}~{end_date}",
        "indicators": {
            k: {kk: vv for kk, vv in v.items() if kk != "data"}
            for k, v in results.items()
        },
        "raw_data": {k: v["data"] for k, v in results.items()},
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

    print(f"\n💾 저장 완료: {output_file}")
    print(f"{'='*50}")

    return results


if __name__ == "__main__":
    collect_all()
