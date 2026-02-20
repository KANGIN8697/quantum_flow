# agents/macro_analyst.py — 거시경제 분석 에이전트 (Agent 1)
# Phase 8: yfinance로 거시 지표 수집 + GPT-4o-mini로 Risk-On/Off 판정
# 하루 1~2회 실행 (06:00, 필요 시 추가 실행)
# JSON 결과를 outputs/reports/macro_YYYYMMDD.json 에 저장

import os
import json
import asyncio
import yfinance as yf
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

# ── 의존성 ────────────────────────────────────────────────────
try:
    from shared_state import set_state, update_risk_params
    from tools.notifier_tools import notify_error
except ImportError:
    def set_state(k, v): pass
    def update_risk_params(p): pass
    def notify_error(s, e, m): pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_PAPER      = os.getenv("USE_PAPER", "true").lower() == "true"
MODE_LABEL     = "모의투자" if USE_PAPER else "실전투자"

# ── 심볼 정의 ────────────────────────────────────────────────
SYMBOLS = {
    "VIX":    "^VIX",          # 공포 지수
    "DXY":    "DX-Y.NYB",      # 달러 인덱스
    "TNX":    "^TNX",           # 미국채 10년 금리
    "SP500":  "^GSPC",          # S&P 500
    "USDKRW": "USDKRW=X",      # 달러/원
    "KOSPI":  "^KS11",          # 코스피
}

_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "reports",
)


# ── 1. 거시 지표 수집 ────────────────────────────────────────

def fetch_macro_data() -> dict:
    """
    yfinance로 6개 거시 지표를 수집한다.

    Returns
    -------
    dict: {심볼: {value, change_pct}}
    """
    result = {}
    for name, ticker in SYMBOLS.items():
        try:
            # period="5d"로 주말/공휴일 데이터 누락 대비
            data = yf.download(
                ticker, period="5d", interval="1d",
                progress=False, auto_adjust=True,
            )
            if len(data) == 0:
                raise ValueError("빈 데이터")

            curr = float(data["Close"].iloc[-1])
            if len(data) >= 2:
                prev     = float(data["Close"].iloc[-2])
                chg_pct  = (curr - prev) / prev * 100 if prev > 0 else 0.0
            else:
                chg_pct = 0.0

            result[name] = {
                "value":      round(curr, 4),
                "change_pct": round(chg_pct, 2),
            }
        except Exception as e:
            print(f"  ⚠️  {name}({ticker}) 조회 실패: {e}")
            result[name] = {"value": 0.0, "change_pct": 0.0}

    return result


# ── 2. LLM 분석 ───────────────────────────────────────────────

def analyze_with_llm(macro: dict) -> dict:
    """
    GPT-4o-mini로 거시 지표를 분석하여 Risk-On/Off 판정을 반환한다.

    Returns
    -------
    dict: {risk: "ON"|"OFF", sectors: [...], reason: str}
    """
    if not OPENAI_API_KEY:
        print("  ⚠️  OPENAI_API_KEY 없음 → 기본값 Risk-ON 반환")
        return {
            "risk":    "ON",
            "sectors": ["반도체", "2차전지", "바이오"],
            "reason":  "OPENAI_API_KEY 미설정으로 기본값 사용",
        }

    vix_val  = macro.get("VIX",    {}).get("value",      0)
    vix_chg  = macro.get("VIX",    {}).get("change_pct", 0)
    dxy_val  = macro.get("DXY",    {}).get("value",      0)
    tnx_val  = macro.get("TNX",    {}).get("value",      0)
    sp_val   = macro.get("SP500",  {}).get("value",      0)
    sp_chg   = macro.get("SP500",  {}).get("change_pct", 0)
    fx_val   = macro.get("USDKRW", {}).get("value",      0)
    ks_val   = macro.get("KOSPI",  {}).get("value",      0)
    ks_chg   = macro.get("KOSPI",  {}).get("change_pct", 0)

    prompt = f"""당신은 글로벌 매크로 전문 애널리스트입니다.
아래 지표를 분석하여 한국 주식시장 당일 투자 전략을 판단하세요.

[거시 지표]
- VIX: {vix_val:.2f} (전일 대비 {vix_chg:+.2f}%)
- 달러인덱스(DXY): {dxy_val:.2f}
- 미국채 10년(TNX): {tnx_val:.3f}%
- S&P500: {sp_val:,.2f} ({sp_chg:+.2f}%)
- USD/KRW: {fx_val:,.1f}원
- KOSPI: {ks_val:,.2f} ({ks_chg:+.2f}%)

[판단 기준]
- Risk-ON : VIX 안정/하락, S&P 강세, 달러 약세 → 공격적 매수 가능
- Risk-OFF: VIX 급등(+15%↑), S&P 약세, 달러 강세 → 현금 보유 권장

반드시 아래 JSON 형식만 반환하세요 (추가 텍스트 없이):
{{"risk": "ON", "sectors": ["섹터1", "섹터2", "섹터3"], "reason": "판단 근거 2~3줄"}}

Risk-OFF 시 sectors는 빈 배열 [], risk는 반드시 "OFF"로 반환."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result  = json.loads(content)

        # 유효성 검증
        if result.get("risk") not in ("ON", "OFF"):
            result["risk"] = "ON"
        if not isinstance(result.get("sectors"), list):
            result["sectors"] = []
        if not result.get("reason"):
            result["reason"] = "LLM 판단 완료"

        return result

    except Exception as e:
        print(f"  ⚠️  LLM 분석 오류: {e} → 기본값 Risk-ON")
        return {
            "risk":    "ON",
            "sectors": ["반도체"],
            "reason":  f"LLM 오류 ({e}) → 보수적 기본값 사용",
        }


# ── 3. 전체 파이프라인 ────────────────────────────────────────

async def run_macro_analysis() -> dict:
    """
    거시 분석 전체 파이프라인을 실행한다.

    순서:
      1. yfinance로 지표 수집
      2. GPT-4o-mini 판정
      3. shared_state 업데이트
      4. 일별 JSON 저장

    Returns
    -------
    dict: {timestamp, macro_data, analysis}
    """
    print(f"\n  📊 [{MODE_LABEL}] 거시경제 분석 시작: {datetime.now().strftime('%H:%M:%S')}")

    loop = asyncio.get_event_loop()

    # 1. 지표 수집 (동기 → executor)
    macro_data = await loop.run_in_executor(None, fetch_macro_data)

    print("  수집 결과:")
    for name, v in macro_data.items():
        flag = "⬆" if v["change_pct"] > 0 else ("⬇" if v["change_pct"] < 0 else "━")
        print(f"    {flag} {name}: {v['value']}  ({v['change_pct']:+.2f}%)")

    # 2. LLM 판정
    analysis = await loop.run_in_executor(None, analyze_with_llm, macro_data)

    risk_emoji = "🟢" if analysis["risk"] == "ON" else "🔴"
    print(f"\n  {risk_emoji} LLM 판정: Risk-{analysis['risk']}")
    if analysis.get("sectors"):
        print(f"    유망 섹터: {', '.join(analysis['sectors'])}")
    print(f"    근거: {analysis.get('reason', '')[:120]}")

    # 3. shared_state 업데이트
    if analysis["risk"] == "OFF":
        update_risk_params({
            "risk_level":         "HIGH",
            "pyramiding_allowed": False,
            "emergency_liquidate": False,
        })
        set_state("macro_risk_off", True)
        set_state("preferred_sectors", [])
        print("  ⚠️  거시 Risk-OFF → 신규 매수 제한 설정")
    else:
        set_state("macro_risk_off", False)
        set_state("preferred_sectors", analysis.get("sectors", []))
        print("  ✅ 거시 Risk-ON → 정상 매매 진행")

    # 4. 결과 저장
    result = {
        "timestamp":  datetime.now().isoformat(),
        "macro_data": macro_data,
        "analysis":   analysis,
    }
    set_state("macro_analysis", result)

    try:
        os.makedirs(_REPORT_DIR, exist_ok=True)
        report_file = os.path.join(
            _REPORT_DIR, f"macro_{date.today().strftime('%Y%m%d')}.json"
        )
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  💾 저장 완료: macro_{date.today().strftime('%Y%m%d')}.json")
    except Exception as e:
        print(f"  ⚠️  저장 실패: {e}")

    return result


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — MacroAnalyst 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 60)

    async def test():
        print("\n[1] 거시 지표 수집 테스트 (yfinance 인터넷 필요)...")
        try:
            macro = fetch_macro_data()
            for name, v in macro.items():
                print(f"    {name}: {v['value']}  ({v['change_pct']:+.2f}%)")
        except Exception as e:
            print(f"  ❌ 수집 오류: {e}")

        print("\n[2] 전체 분석 파이프라인 실행...")
        try:
            result = await run_macro_analysis()
            a = result.get("analysis", {})
            print(f"\n  최종 판정: Risk-{a.get('risk', '?')}")
            print(f"  섹터: {a.get('sectors', [])}")
        except Exception as e:
            print(f"  ❌ 파이프라인 오류: {e}")
            import traceback
            traceback.print_exc()

        print("\n" + "=" * 60)
        print("  ✅ MacroAnalyst 테스트 완료!")
        print(f"  💡 OPENAI_API_KEY 없으면 기본값(Risk-ON) 반환됨")
        print("=" * 60)

    asyncio.run(test())
