# agents/market_scanner.py — 종목 스캐닝 에이전트 (Agent 2)
# Phase 8: 거래량 상위 종목 조회 → 기술적 사전필터 → LLM 최종 선정
# 하루 2회 실행: 08:30 (전일 기준), 11:30 (장중 재스크리닝)
# 결과 → shared_state.watch_list 저장 + websocket_feeder 구독 요청

import os
import json
import asyncio
import requests
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, date
from dotenv import load_dotenv
import logging

load_dotenv()

logger = logging.getLogger(__name__)

# ── HTTP 세션 (KIS 스캔 API, TCP 재사용 + 자동 재시도) ──────────
_SCAN_RETRY = Retry(total=3, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503])
_SCAN_SESSION = requests.Session()
_SCAN_SESSION.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10, max_retries=_SCAN_RETRY))

# ── 의존성 ────────────────────────────────────────────────────
try:
    from config.settings import (
        MAX_WATCH_STOCKS, DONCHIAN_PERIOD, RSI_LOWER, RSI_UPPER,
        SECTOR_DELTA_BONUS_MAX, SECTOR_DELTA_BONUS_MIN,
        SECTOR_MORNING_TIME, SECTOR_MIDDAY_TIME,
    )
    from shared_state import set_state, get_state
    from tools.scanner_tools import calc_donchian, calc_rsi
    from tools.token_manager import ensure_token
    from tools.notifier_tools import notify_error
except ImportError:
    MAX_WATCH_STOCKS = 30
    DONCHIAN_PERIOD  = 20
    RSI_LOWER        = 50
    RSI_UPPER        = 70
    SECTOR_DELTA_BONUS_MAX = 6
    SECTOR_DELTA_BONUS_MIN = 2
    SECTOR_MORNING_TIME    = "09:20"
    SECTOR_MIDDAY_TIME     = "11:30"
    def set_state(k, v): pass
    def get_state(k): return None
    def calc_donchian(df, **k): return {"upper": 0, "lower": 0}
    def calc_rsi(df, **k): return 50.0
    def ensure_token(): return ""
    def notify_error(s, e, m): pass

try:
    from tools.stock_eval_tools import evaluate_stock, evaluate_multiple
except ImportError:
    def evaluate_stock(code, macro_sectors=None):
        return {"code": code, "grade": "C", "total_score": 0, "position_pct": 0.5, "action": "평가불가"}
    def evaluate_multiple(codes, macro_sectors=None):
        return [evaluate_stock(c) for c in codes]


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_PAPER      = os.getenv("USE_PAPER", "true").lower() == "true"
MODE_LABEL     = "모의투자" if USE_PAPER else "실전투자"

if USE_PAPER:
    BASE_URL   = "https://openapivts.koreainvestment.com:29443"
    APP_KEY    = os.getenv("KIS_PAPER_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_PAPER_APP_SECRET", "")
else:
    BASE_URL   = "https://openapi.koreainvestment.com:9443"
    APP_KEY    = os.getenv("KIS_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_APP_SECRET", "")

_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "reports",
)

# ── 백업 감시 풀 (KIS API 실패 시) ────────────────────────────
DEFAULT_WATCH_POOL = [
    "005930", "000660", "035420", "035720", "051910",
    "006400", "028260", "066570", "323410", "207940",
    "068270", "105560", "055550", "086790", "003550",
    "015760", "012330", "000270", "005380", "096770",
]


# ── 공통 헤더 ────────────────────────────────────────────────

def _headers(tr_id: str) -> dict:
    return {
        "Content-Type":  "application/json",
        "authorization": f"Bearer {ensure_token()}",
        "appkey":        APP_KEY,
        "appsecret":     APP_SECRET,
        "tr_id":         tr_id,
    }


# ── 1. 거래량 상위 종목 조회 ────────────────────────────────

def fetch_volume_top(top_n: int = 50) -> list:
    """
    KIS API로 당일 거래량 상위 종목을 조회한다.

    Returns
    -------
    list of dict: [{code, name, volume, price, change_pct}, ...]
    """
    try:
        # ranking API는 모의투자 서버 미지원 -> 실서버 사용
        REAL_BASE = "https://openapi.koreainvestment.com:9443"
        url = f"{REAL_BASE}/uapi/domestic-stock/v1/ranking/volume"
        params = {
            "FID_COND_MRKT_DIV_CODE":  "J",
            "FID_COND_SCR_DIV_CODE":   "20171",
            "FID_INPUT_ISCD":          "0000",
            "FID_DIV_CLS_CODE":        "0",
            "FID_BLNG_CLS_CODE":       "0",
            "FID_TRGT_CLS_CODE":       "111111111",
            "FID_TRGT_EXLS_CLS_CODE":  "000000",
            "FID_INPUT_PRICE_1":       "2000",    # 최소 주가 2,000원 (잡주 제외)
            "FID_INPUT_PRICE_2":       "500000",
            "FID_VOL_CNT":             "500000",  # 최소 거래량 50만
            "FID_INPUT_DATE_1":        "",
        }
        resp = _SCAN_SESSION.get(url, headers=_headers("FHPST01710000"),
                            params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = []
        for item in (data.get("output") or [])[:top_n]:
            code = item.get("mksc_shrn_iscd", "").strip()
            if not code or len(code) != 6:
                continue
            result.append({
                "code":       code,
                "name":       item.get("hts_kor_isnm", code),
                "volume":     int(item.get("acml_vol", 0) or 0),
                "price":      int(item.get("stck_prpr", 0) or 0),
                "change_pct": float(item.get("prdy_ctrt", 0) or 0),
            })

        print(f"  KIS 거래량 상위 {len(result)}종목 수집")
        return result

    except Exception as e:
        print(f"  ⚠️  거래량 상위 조회 실패: {e}")
        return []


# ── 2. 일봉 OHLCV 조회 (사전 필터용) ─────────────────────────

def _fetch_ohlcv(code: str, period: int = 25) -> object:
    """KIS API로 일봉 데이터를 조회하여 pandas DataFrame으로 반환한다."""
    import pandas as pd
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = _headers("FHKST03010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         code,
            "FID_INPUT_DATE_1":       "",
            "FID_INPUT_DATE_2":       datetime.now().strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_ORG_ADJ_PRC":        "0",
        }
        resp = _SCAN_SESSION.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        for item in (data.get("output2") or [])[:period]:
            rows.append({
                "open":   float(item.get("stck_oprc", 0) or 0),
                "high":   float(item.get("stck_hgpr", 0) or 0),
                "low":    float(item.get("stck_lwpr", 0) or 0),
                "close":  float(item.get("stck_clpr", 0) or 0),
                "volume": int(item.get("acml_vol", 0) or 0),
            })
        if len(rows) < 5:
            return None
        return pd.DataFrame(rows[::-1])
    except Exception as e:
        logger.debug(f"agents/market_scanner.py: {type(e).__name__}: {e}")
        return None


# ── 3. 기술적 사전 필터 ──────────────────────────────────────

def apply_tech_filter(candidates: list, max_out: int = 40) -> list:
    """
    돈치안 근접(95%) 또는 RSI 50~70 범위 종목만 통과시킨다.
    API 호출이 많으므로 상위 max_out개로 제한.
    """
    passed = []
    for item in candidates:
        if len(passed) >= max_out:
            break

        code  = item.get("code", "")
        price = item.get("price", 0)

        try:
            df = _fetch_ohlcv(code)
            if df is None or len(df) < DONCHIAN_PERIOD:
                continue

            dc    = calc_donchian(df)
            rsi   = calc_rsi(df)
            upper = dc.get("upper", 0)

            near_donchian = upper > 0 and price >= upper * 0.95
            rsi_ok        = RSI_LOWER <= rsi <= RSI_UPPER

            if near_donchian or rsi_ok:
                item = dict(item)
                item["donchian_upper"] = upper
                item["rsi"]            = round(rsi, 1)
                item["near_donchian"]  = near_donchian
                # score calculation
                score = 0
                reasons = []
                if item.get("near_donchian"):
                    score += 40
                    reasons.append("donchian_top(+40)")
                if rsi and RSI_LOWER <= rsi <= RSI_UPPER:
                    score += 30
                    reasons.append(f"RSI_ok({rsi:.0f},+30)")
                if item.get("change_pct", 0) > 0:
                    score += 15
                    reasons.append(f"up({item.get('change_pct',0):+.1f}%,+15)")
                if item.get("volume", 0) > 1000000:
                    score += 15
                    reasons.append("high_vol(+15)")
                item["score"] = score
                item["reasons"] = reasons
                passed.append(item)

        except Exception:
            continue

    return passed


# ── 4. LLM 최종 선정 ─────────────────────────────────────────

def select_with_llm(candidates: list, preferred_sectors: list) -> list:
    """
    GPT-4o-mini로 최종 감시 종목 20~30개를 선정한다.
    API 키 없으면 기술 필터 결과를 그대로 반환.
    """
    if not OPENAI_API_KEY:
        print("  ⚠️  OPENAI_API_KEY 없음 → 기술 필터 결과 사용")
        return [c.get("code") for c in candidates[:MAX_WATCH_STOCKS]]

    if not candidates:
        return []

    sectors_str = ", ".join(preferred_sectors) if preferred_sectors else "전체 섹터"

    stock_lines = "\n".join([
        f"{i+1}. {c['name']}({c['code']}) "
        f"가격:{c['price']:,}원 거래량:{c['volume']:,} "
        f"등락:{c['change_pct']:+.1f}% RSI:{c.get('rsi', 50):.0f} "
        f"돈치안근접:{'Y' if c.get('near_donchian') else 'N'}"
        for i, c in enumerate(candidates[:30])
    ])

    prompt = f"""당신은 한국 주식 단기 트레이더입니다.
오늘 선호 섹터: {sectors_str}

아래 후보 종목 중 당일 단기 매매에 가장 적합한 20~30종목을 선택하세요.

[선택 기준]
1. 선호 섹터와 연관성 (높을수록 우선)
2. 거래량 급증 여부 (변동성 확보)
3. RSI 50~70 범위 (과매수 제외)
4. 돈치안 상단 근접 (돌파 임박)

[후보 종목]
{stock_lines}

반드시 아래 JSON 형식만 반환하세요 (추가 텍스트 없이):
{{"selected": ["코드1", "코드2", ...], "reason": "선택 근거 한 줄"}}

selected 배열에는 6자리 종목코드만 넣으세요. 최대 30개."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        result  = json.loads(content)

        selected = result.get("selected", [])
        reason   = result.get("reason", "")
        # 유효한 6자리 코드만 필터
        selected = [c for c in selected if isinstance(c, str) and len(c) == 6]
        selected = selected[:MAX_WATCH_STOCKS]

        print(f"  🤖 LLM 선정: {len(selected)}종목  근거: {reason[:80]}")
        return selected

    except Exception as e:
        print(f"  ⚠️  LLM 선정 오류: {e} → 기술 필터 결과 사용")
        return [c.get("code") for c in candidates[:MAX_WATCH_STOCKS]]


# ── 5. 전체 파이프라인 ────────────────────────────────────────

def _cache_sector_scores(filtered: list, time_key: str):
    """
    [기능2] 섹터별 평균 eval_score를 계산하여 shared_state에 캐싱.
    time_key: "sector_scores_morning" 또는 "sector_scores_midday"
    """
    from collections import defaultdict
    sector_scores = defaultdict(list)

    try:
        from tools.stock_eval_tools import STOCK_SECTOR_MAP
    except ImportError:
        STOCK_SECTOR_MAP = {}

    for c in filtered:
        code = c.get("code", "")
        score = c.get("eval_score", 0)
        sector = STOCK_SECTOR_MAP.get(code, c.get("sector", ""))
        if sector:
            sector_scores[sector].append(score)

    avg_scores = {}
    for sector, scores in sector_scores.items():
        avg_scores[sector] = round(sum(scores) / len(scores), 2) if scores else 0

    set_state(time_key, avg_scores)
    return avg_scores


def _apply_sector_momentum_delta(filtered: list, round_label: str):
    """
    [기능2] 섹터 Momentum Delta 적용.
    1차(오전): 섹터 점수를 캐싱만 한다.
    2차(오후): 오전 점수와 비교하여 Delta 가산점을 부여한다.
    """
    now_time = datetime.now().strftime("%H:%M")

    if round_label == "1차" or now_time < SECTOR_MIDDAY_TIME:
        # 오전: 캐싱만
        scores = _cache_sector_scores(filtered, "sector_scores_morning")
        if scores:
            print(f"  📊 [Momentum Delta] 오전 섹터 점수 캐시: {len(scores)}개 섹터")
        return

    # 2차(오후): Delta 계산
    morning_scores = get_state("sector_scores_morning") or {}
    if not morning_scores:
        print("  ⚠️  [Momentum Delta] 오전 캐시 없음 → Delta 미적용")
        return

    midday_scores = _cache_sector_scores(filtered, "sector_scores_midday")

    # Delta 계산 및 가산점 부여
    deltas = {}
    for sector in midday_scores:
        if sector in morning_scores:
            delta = midday_scores[sector] - morning_scores[sector]
            deltas[sector] = round(delta, 2)

    if not deltas:
        return

    # 양수 Delta 중 최대/최소로 보너스 스케일링
    positive_deltas = {s: d for s, d in deltas.items() if d > 0}
    if not positive_deltas:
        print(f"  📊 [Momentum Delta] 양수 Delta 없음 → 가산 미적용")
        return

    max_delta = max(positive_deltas.values())
    if max_delta <= 0:
        return

    try:
        from tools.stock_eval_tools import STOCK_SECTOR_MAP
    except ImportError:
        STOCK_SECTOR_MAP = {}

    bonus_applied = 0
    for c in filtered:
        code = c.get("code", "")
        sector = STOCK_SECTOR_MAP.get(code, c.get("sector", ""))
        if sector in positive_deltas:
            # Delta 크기에 비례하여 가산점 (2~6점)
            delta_ratio = positive_deltas[sector] / max_delta
            bonus = round(
                SECTOR_DELTA_BONUS_MIN + delta_ratio * (SECTOR_DELTA_BONUS_MAX - SECTOR_DELTA_BONUS_MIN)
            )
            c["eval_score"] = c.get("eval_score", 0) + bonus
            c["delta_bonus"] = bonus
            bonus_applied += 1

    if bonus_applied > 0:
        top_sectors = sorted(positive_deltas.items(), key=lambda x: x[1], reverse=True)[:3]
        print(f"  📊 [Momentum Delta] {bonus_applied}종목에 가산 적용")
        print(f"     상위 Delta: {top_sectors}")

    # 점수 변경 후 재정렬
    filtered.sort(key=lambda x: x.get("eval_score", 0), reverse=True)


async def run_scanner(round_label: str = "1차") -> list:
    """
    종목 스캐닝 전체 파이프라인을 실행한다.

    순서:
      1. KIS API 거래량 상위 조회
      2. 기술적 사전 필터 (돈치안 / RSI)
      3. LLM 최종 선정
      4. shared_state.watch_list 업데이트

    Parameters
    ----------
    round_label : "1차"(08:30) 또는 "2차"(11:30)

    Returns
    -------
    list[str]: 감시 종목 코드 리스트
    """
    print(f"\n  🔍 [{MODE_LABEL}] {round_label} 스캐닝 시작: "
          f"{datetime.now().strftime('%H:%M:%S')}")

    loop = asyncio.get_running_loop()

    # 1. 거래량 상위 조회
    candidates = await loop.run_in_executor(None, fetch_volume_top, 50)

    if not candidates:
        print("  ⚠️  KIS 조회 실패 → 기본 감시 풀 사용")
        candidates = [
            {"code": c, "name": c, "volume": 0,
             "price": 0, "change_pct": 0.0}
            for c in DEFAULT_WATCH_POOL
        ]

    # 2. 기술적 필터 (API 다중 호출, 시간 소요)
    print(f"  기술적 필터 적용 중 ({len(candidates)}종목)...")
    filtered = await loop.run_in_executor(None, apply_tech_filter, candidates, 40)
    print(f"  기술 필터 통과: {len(filtered)}종목")

    # 결과가 너무 적으면 원본 사용
    if len(filtered) < 10:
        print("  ⚠️  필터 결과 부족 → 원본 상위 30종목 사용")
        filtered = candidates[:30]

    # 2.5 주가 상승 지표 평가 (stock_eval) — 모든 필터 결과에 대해 실행
    print(f"  📊 종목 평가 진행 중 ({len(filtered)}종목)...")
    try:
        macro_sectors_list = get_state("macro_sectors") or []
        avoid_sectors_list = get_state("macro_avoid_sectors") or []
        sector_multipliers = get_state("sector_multipliers") or {}
        macro_sectors = {
            "sectors": macro_sectors_list,
            "avoid_sectors": avoid_sectors_list,
            "sector_multipliers": sector_multipliers,
        }
        eval_codes = [c["code"] for c in filtered]
        eval_results = evaluate_multiple(eval_codes, macro_sectors)
        # 평가 결과를 filtered에 매핑
        eval_map = {r["code"]: r for r in eval_results}
        for c in filtered:
            ev = eval_map.get(c["code"], {})
            c["eval_grade"] = ev.get("grade", "?")
            c["eval_score"] = ev.get("total_score", 0)
            c["eval_action"] = ev.get("action", "")
            c["position_pct"] = ev.get("position_pct", 0.5)
            c["sector"] = ev.get("details", {}).get("sector", {}).get("sector", "")
            c["entry_atr"] = 0  # ATR은 실시간 데이터에서 채워짐
        # D/F 등급 필터링
        before_cnt = len(filtered)
        filtered = [c for c in filtered if c.get("eval_grade") not in ("D", "F")]
        filtered.sort(key=lambda x: x.get("eval_score", 0), reverse=True)
        print(f"  ✅ 평가 완료: {before_cnt}→{len(filtered)}종목 (D/F 제외)")
        for c in filtered[:5]:
            print(f"     {c['code']} [{c.get('eval_grade','?')}] score={c.get('eval_score',0)}")
    except Exception as e:
        print(f"  ⚠️ 종목 평가 스킵: {e}")

    # 2.7 [기능2] 섹터 Momentum Delta 캐싱/적용
    _apply_sector_momentum_delta(filtered, round_label)

    # 3. LLM 최종 선정
    preferred  = get_state("preferred_sectors") or []
    watch_list = await loop.run_in_executor(
        None, select_with_llm, filtered, preferred
    )

    if not watch_list:
        watch_list = [c.get("code") for c in filtered[:MAX_WATCH_STOCKS]]

    # 4. shared_state 업데이트
    set_state("watch_list", watch_list)

    # 4.5 평가 결과를 shared_state에 저장 (head_strategist가 참조)
    filtered_map = {c["code"]: c for c in filtered}
    scanner_selected = []
    for code in watch_list:
        info = filtered_map.get(code, {})
        scanner_selected.append({
            "code": code,
            "eval_grade": info.get("eval_grade", "?"),
            "eval_score": info.get("eval_score", 0),
            "position_pct": info.get("position_pct", 0.5),
            "sector": info.get("sector", ""),
            "entry_atr": info.get("entry_atr", 0),
        })
    set_state("scanner_result", {"selected": scanner_selected})

    preview = watch_list[:5]
    more    = f"... 외 {len(watch_list)-5}개" if len(watch_list) > 5 else ""
    print(f"  ✅ 감시 종목 {len(watch_list)}개 등록: {preview}{more}")

    # 5. 결과 저장
    try:
        os.makedirs(_REPORT_DIR, exist_ok=True)
        safe_label = round_label.replace(" ", "_")
        fname = f"scanner_{date.today().strftime('%Y%m%d')}_{safe_label}.json"
        with open(os.path.join(_REPORT_DIR, fname), "w", encoding="utf-8") as f:
            json.dump({
                "timestamp":       datetime.now().isoformat(),
                "round":           round_label,
                "watch_list":      watch_list,
                "candidates_cnt":  len(candidates),
                "filtered_cnt":    len(filtered),
            }, f, ensure_ascii=False, indent=2)
        print(f"  💾 저장: {fname}")
    except Exception as e:
        print(f"  ⚠️  저장 실패: {e}")

    return watch_list


# ── main.py 진입점 ─────────────────────────────────────────────

async def market_scanner_run() -> dict:
    """
    main.py에서 호출하는 종목 스캐닝 진입점.
    run_scanner()를 실행하고 main.py가 기대하는 형식으로 반환.
    """
    watch_list = await run_scanner("1차")
    return {
        "candidates": len(watch_list),
        "watch_list": watch_list,
    }


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — MarketScanner 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 60)

    async def test():
        if not APP_KEY or not APP_SECRET:
            print("\n⚠️  API 키 없음 → 기본 감시 풀로 테스트")
            print("  (KIS_APP_KEY / KIS_PAPER_APP_KEY를 .env에 설정 후 전체 기능 사용)")
            # 기본 풀 테스트
            set_state("preferred_sectors", ["반도체", "2차전지"])
            candidates = [
                {"code": c, "name": c, "volume": 1_000_000,
                 "price": 50000, "change_pct": 2.0}
                for c in DEFAULT_WATCH_POOL[:10]
            ]
            print(f"\n[LLM 선정 테스트] 후보 {len(candidates)}종목...")
            result = select_with_llm(candidates, ["반도체"])
            print(f"  선정: {result}")
        else:
            print("\n[전체 파이프라인 테스트] (인터넷 + API 키 필요)")
            try:
                watch_list = await run_scanner("테스트")
                print(f"\n  최종 감시 종목 ({len(watch_list)}개): {watch_list[:10]}")
            except Exception as e:
                print(f"  ❌ 오류: {e}")
                import traceback
                traceback.print_exc()

        print("\n" + "=" * 60)
        print("  ✅ MarketScanner 테스트 완료!")
        print("=" * 60)

    asyncio.run(test())


# Wrapper for main.py compatibility
async def market_scanner_run():
    result = await run_scanner()
    return {"candidates": result}
