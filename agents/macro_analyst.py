# agents/macro_analyst.py — 거시경제 분석 에이전트 (Agent 1)
# Phase 9: FRED API + yfinance + 뉴스 수집 + GPT-4o-mini 종합 분석
# 3페이지 일일 거시경제 보고서 생성
# 긴급 뉴스 감지 시 HeadStrategist에 알림

import os
import json
import asyncio
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

# ── 의존성 ────────────────────────────────────────────────
try:
    from shared_state import set_state, update_risk_params
    from tools.notifier_tools import notify_error
except ImportError:
    def set_state(k, v): pass
    def update_risk_params(p): pass
    def notify_error(s, e, m): pass

try:
    from tools.macro_data_tools import (
        collect_all_macro_data, check_urgent_news
    )
except ImportError:
    def collect_all_macro_data():
        return {"macro_data": {}, "news": [], "urgent": {"level": "LOW"}}
    def check_urgent_news(n=None):
        return {"level": "LOW", "total_score": 0}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_PAPER      = os.getenv("USE_PAPER", "true").lower() == "true"
MODE_LABEL     = "모의투자" if USE_PAPER else "실전투자"

_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "reports",
)


# ── GPT 분석 프롬프트 ────────────────────────────────────

SYSTEM_PROMPT = """당신은 한국 주식시장 전문 거시경제 분석가입니다.
주어진 거시 지표와 뉴스를 바탕으로 종합 분석 보고서를 작성합니다.

반드시 아래 JSON 형식으로 응답하세요:
{
  "risk": "ON" 또는 "OFF",
  "confidence": 0~100 (판단 확신도),
  "sectors": ["추천섹터1", "추천섹터2"],
  "avoid_sectors": ["회피섹터1"],
  "sector_multipliers": {
    "반도체": 1.0,
    "2차전지": 1.0,
    "바이오": 1.0,
    "자동차": 1.0,
    "금융": 1.0,
    "철강": 1.0,
    "IT": 1.0,
    "화학": 1.0,
    "건설": 1.0,
    "에너지": 1.0
  },
  "report": "3페이지 분량의 상세 보고서 (마크다운 형식)",
  "summary": "3줄 요약",
  "urgent_action": "NONE" 또는 "REDUCE" 또는 "EXIT_ALL"
}

sector_multipliers 작성 규칙:
- 각 섹터의 가중치를 0.5 ~ 1.5 범위에서 결정하세요.
- 기본값은 1.0이며, 거시경제 상황에 따라 조정합니다.
- 기본 규칙 (USD/KRW 기반):
  * USD/KRW >= 1400원: 수출주(반도체,자동차,IT) 1.2, 내수주(건설,금융) 0.8
  * USD/KRW >= 1350원: 수출주 1.1, 내수주 0.9
  * USD/KRW <= 1250원: 수출주 0.9, 내수주 1.1
- 위 기본 규칙에서 ±0.1 범위로 미세조정할 수 있습니다.
- 글로벌 유가 급등 시 에너지/화학 상향, 바이오는 거시에 덜 민감하므로 1.0 유지.

보고서(report)는 반드시 다음 구조를 따르세요:

## 1. 글로벌 매크로 환경
- 미국 경제 상황 (S&P500, 금리, 달러)
- 글로벌 리스크 요인

## 2. 한국 시장 분석
- 코스피/코스닥 동향
- 원/달러 환율 영향
- 섹터별 전망

## 3. 투자 전략 및 리스크 관리
- 오늘의 매매 전략 (공격적/보수적/방어적)
- 주의해야 할 리스크
- 추천 섹터와 근거
- 회피해야 할 섹터와 근거

판단 기준:
- VIX > 25: Risk-OFF 고려
- VIX > 30: 강한 Risk-OFF
- DXY 급등 + 원화 약세: Risk-OFF
- S&P500 -2% 이상 하락: Risk-OFF
- 긴급 뉴스(전쟁/서킷브레이커 등): urgent_action="EXIT_ALL"
"""


# ── 1. GPT 분석 요청 ─────────────────────────────────────

async def analyze_with_gpt(macro_data: dict, news_list: list, urgent_info: dict) -> dict:
    """GPT-4o-mini에게 거시 데이터 + 뉴스를 전달하여 종합 분석"""
    
    if not OPENAI_API_KEY:
        print("  ⚠ OPENAI_API_KEY 없음 → 기본값 Risk-ON 반환")
        return _default_analysis("OPENAI_API_KEY 미설정으로 기본값 사용")
    
    # 뉴스 헤드라인 정리 (토큰 절약)
    news_text = ""
    for i, n in enumerate(news_list[:15], 1):
        title = n.get("title", "")
        source = n.get("source", "")
        news_text += f"{i}. {title}"
        if source:
            news_text += f" ({source})"
        news_text += "\n"
    
    # 거시 지표 정리
    indicators_text = ""
    for k, v in macro_data.items():
        val = v.get("value", 0)
        chg = v.get("change_pct", "")
        src = v.get("source", "")
        dt = v.get("date", "")
        line = f"- {k}: {val}"
        if chg:
            line += f" ({chg:+.2f}%)" if isinstance(chg, (int, float)) else f" ({chg})"
        if dt:
            line += f" [{dt}]"
        if src:
            line += f" (출처: {src})"
        indicators_text += line + "\n"
    
    user_msg = f"""## 오늘 날짜: {date.today().isoformat()}
## 모드: {MODE_LABEL}

## 거시경제 지표:
{indicators_text}

## 최신 경제/증시 뉴스:
{news_text}

## 긴급 뉴스 상태: {urgent_info.get('level', 'LOW')} (점수: {urgent_info.get('total_score', 0)})
{_format_urgent(urgent_info)}

위 데이터를 바탕으로 오늘의 한국 주식시장 투자 전략을 분석해주세요.
반드시 지정된 JSON 형식으로 응답하세요."""
    
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=3000,
        )
        
        text = resp.choices[0].message.content.strip()
        
        # JSON 파싱
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        
        analysis = json.loads(text)
        
        # 필수 필드 확인
        if "risk" not in analysis:
            analysis["risk"] = "ON"
        if "sectors" not in analysis:
            analysis["sectors"] = ["반도체", "2차전지"]
        if "report" not in analysis:
            analysis["report"] = "보고서 생성 실패"
        if "urgent_action" not in analysis:
            analysis["urgent_action"] = "NONE"
        
        return analysis
        
    except json.JSONDecodeError as e:
        print(f"  ⚠ GPT 응답 JSON 파싱 실패: {e}")
        # 텍스트 응답이라도 활용
        return _default_analysis(f"JSON 파싱 실패, GPT 원문 참고: {text[:200]}")
    except Exception as e:
        reason = f"LLM 오류 ({e}) → 보수적 기본값 사용"
        print(f"  ⚠ {reason}")
        return _default_analysis(reason)


def _validate_sector_multipliers(raw: dict) -> dict:
    """
    [기능6] LLM이 생성한 섹터 멀티플라이어를 검증하고 클리핑.
    - dict가 아니면 빈 dict 반환
    - 값이 0.5~1.5 범위를 벗어나면 클리핑
    - 숫자가 아닌 값은 기본값 1.0으로 대체
    """
    if not isinstance(raw, dict):
        return {}

    try:
        from config.settings import (
            SECTOR_MULTIPLIER_MIN, SECTOR_MULTIPLIER_MAX, SECTOR_MULTIPLIER_DEFAULT,
        )
    except ImportError:
        SECTOR_MULTIPLIER_MIN = 0.5
        SECTOR_MULTIPLIER_MAX = 1.5
        SECTOR_MULTIPLIER_DEFAULT = 1.0

    validated = {}
    for sector, mult in raw.items():
        try:
            val = float(mult)
            val = max(SECTOR_MULTIPLIER_MIN, min(val, SECTOR_MULTIPLIER_MAX))
            validated[sector] = round(val, 2)
        except (ValueError, TypeError):
            validated[sector] = SECTOR_MULTIPLIER_DEFAULT
    return validated


def _default_analysis(reason: str) -> dict:
    """기본 분석 결과 (폴백)"""
    return {
        "risk": "ON",
        "confidence": 50,
        "sectors": ["반도체", "2차전지", "바이오"],
        "avoid_sectors": [],
        "report": f"## 자동 기본값 보고서\n\nGPT 분석을 수행할 수 없어 기본값을 사용합니다.\n\n사유: {reason}",
        "summary": f"기본값 사용 (Risk-ON). 사유: {reason}",
        "urgent_action": "NONE",
        "reason": reason,
    }


def _format_urgent(urgent_info: dict) -> str:
    """긴급 뉴스 정보를 텍스트로 포맷"""
    items = urgent_info.get("urgent_items", [])
    if not items:
        return "긴급 뉴스 없음"
    lines = []
    for item in items:
        lines.append(f"  - [{', '.join(item.get('keywords', []))}] {item.get('title', '')}")
    return "\n".join(lines)


# ── 2. 보고서 저장 ───────────────────────────────────────

def save_report(result: dict) -> str:
    """분석 결과를 JSON + 마크다운 보고서로 저장"""
    os.makedirs(_REPORT_DIR, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    
    # JSON 저장
    json_path = os.path.join(_REPORT_DIR, f"macro_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    # 마크다운 보고서 저장
    report_text = result.get("analysis", {}).get("report", "")
    if report_text:
        md_path = os.path.join(_REPORT_DIR, f"macro_report_{today}.md")
        header = f"""# 거시경제 일일 보고서
**날짜**: {date.today().isoformat()}
**분석 시각**: {result.get('timestamp', '')}
**판정**: Risk-{result.get('analysis', {}).get('risk', 'ON')}
**확신도**: {result.get('analysis', {}).get('confidence', 0)}%
**추천 섹터**: {', '.join(result.get('analysis', {}).get('sectors', []))}
**긴급 조치**: {result.get('analysis', {}).get('urgent_action', 'NONE')}

---

"""
        summary = result.get('analysis', {}).get('summary', '')
        if summary:
            header += f"### 요약\n{summary}\n\n---\n\n"
        
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(header + report_text)
        print(f"  📄 보고서 저장: {md_path}")
    
    return json_path


# ── 3. 전체 파이프라인 ───────────────────────────────────

async def run_macro_analysis() -> dict:
    """
    거시 분석 전체 파이프라인을 실행한다.
    1) FRED + yfinance + 뉴스 데이터 수집 (주말에도 동작)
    2) GPT-4o-mini로 종합 분석 + 3페이지 보고서 생성
    3) 긴급 뉴스 감지 시 즉시 알림
    4) JSON + MD 파일 저장
    """
    print(f"\n{'='*55}")
    print(f"  [STEP 1] 거시경제 분석 시작 ({MODE_LABEL})")
    print(f"{'='*55}")
    
    # 1) 데이터 수집
    print("\n📡 다중 소스 데이터 수집...")
    raw_data = collect_all_macro_data()
    
    macro_data = raw_data.get("macro_data", {})
    news_list = raw_data.get("news", [])
    urgent_info = raw_data.get("urgent", {"level": "LOW"})
    
    # 2) 긴급 뉴스 사전 체크
    if urgent_info.get("level") == "CRITICAL":
        print("  🚨 긴급 뉴스 감지! 즉시 분석 진행...")
    
    # 3) GPT 분석
    print("\n🤖 GPT-4o-mini 종합 분석 중...")
    analysis = await analyze_with_gpt(macro_data, news_list, urgent_info)
    
    # 4) shared_state 업데이트
    risk_label = analysis.get("risk", "ON")
    set_state("macro_risk", risk_label)
    set_state("macro_sectors", analysis.get("sectors", []))
    set_state("macro_urgent", analysis.get("urgent_action", "NONE"))
    set_state("macro_confidence", analysis.get("confidence", 50))

    # [기능6] 섹터 멀티플라이어 저장 (검증 + 클리핑)
    raw_multipliers = analysis.get("sector_multipliers", {})
    validated_multipliers = _validate_sector_multipliers(raw_multipliers)
    set_state("sector_multipliers", validated_multipliers)
    if validated_multipliers:
        non_default = {k: v for k, v in validated_multipliers.items() if v != 1.0}
        if non_default:
            print(f"  📊 섹터 멀티플라이어: {non_default}")
    
    if risk_label == "OFF":
        set_state("risk_off", True)
        update_risk_params({
            "risk_level": "HIGH",
            "position_pct": 0.5,
            "pyramiding_allowed": False,
        })

    if analysis.get("urgent_action") == "EXIT_ALL":
        set_state("risk_off", True)
        set_state("force_exit", True)
        update_risk_params({
            "risk_level": "CRITICAL",
            "emergency_liquidate": True,
            "pyramiding_allowed": False,
        })
        print("  🚨🚨 긴급 전량 청산 시그널 발생!")
    elif analysis.get("urgent_action") == "REDUCE":
        update_risk_params({
            "risk_level": "HIGH",
            "position_pct": 0.3,
            "pyramiding_allowed": False,
        })
        print("  ⚠ 포지션 축소 시그널 발생")
    
    # 5) 결과 조립
    result = {
        "timestamp": raw_data.get("timestamp", datetime.now().isoformat()),
        "macro_data": macro_data,
        "news_count": len(news_list),
        "news_headlines": [n.get("title", "") for n in news_list[:10]],
        "urgent": urgent_info,
        "analysis": analysis,
    }
    
    # 6) 저장
    json_path = save_report(result)
    print(f"\n  💾 JSON 저장: {json_path}")
    
    # 7) 결과 요약 출력
    print(f"\n{'='*55}")
    print(f"  ✅ 거시 분석 완료")
    print(f"  판정: Risk-{risk_label} (확신도 {analysis.get('confidence', 0)}%)")
    print(f"  추천: {', '.join(analysis.get('sectors', []))}")
    print(f"  회피: {', '.join(analysis.get('avoid_sectors', []))}")
    print(f"  긴급: {analysis.get('urgent_action', 'NONE')}")
    print(f"  뉴스: {len(news_list)}건 수집")
    summary = analysis.get("summary", "")
    if summary:
        print(f"  요약: {summary[:100]}")
    print(f"{'='*55}")
    
    return result


# ── main.py 진입점 ────────────────────────────────────────

async def macro_analyst_run() -> dict:
    """
    main.py에서 호출하는 거시경제 분석 진입점.
    run_macro_analysis()를 실행하고 main.py가 기대하는 형식으로 반환.
    """
    result = await run_macro_analysis()
    analysis = result.get("analysis", {})
    return {
        "risk_status": analysis.get("risk", "ON"),
        "confidence": analysis.get("confidence", 50),
        "sectors": analysis.get("sectors", []),
        "avoid_sectors": analysis.get("avoid_sectors", []),
        "urgent_action": analysis.get("urgent_action", "NONE"),
        "summary": analysis.get("summary", ""),
        "raw": result,
    }


# ── 테스트 블록 ──────────────────────────────────────────

async def test():
    try:
        print("\n[1] 데이터 수집 테스트...")
        data = collect_all_macro_data()
        print(f"  지표: {list(data.get('macro_data', {}).keys())}")
        print(f"  뉴스: {data.get('news_count', 0)}건")
        print(f"  긴급: {data.get('urgent', {}).get('level', 'N/A')}")
    except Exception as e:
        print(f"  ❌ 수집 오류: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"\n[2] 전체 분석 파이프라인 실행...")
    try:
        result = await run_macro_analysis()
        a = result.get("analysis", {})
        print(f"\n 최종 판정: Risk-{a.get('risk', '?')}")
        print(f"  섹터: {a.get('sectors', [])}")
        print(f"  긴급조치: {a.get('urgent_action', 'NONE')}")
    except Exception as e:
        print(f"  ❌ 파이프라인 오류: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("  ✅ MacroAnalyst 테스트 완료!")
    print(f"  💡 OPENAI_API_KEY 없으면 기본값(Risk-ON) 반환됨")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test())
