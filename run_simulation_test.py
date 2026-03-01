#!/usr/bin/env python3
# run_simulation_test.py — QUANTUM FLOW v2.1 전체 시뮬레이션 테스트
#
# 실행 방법:
#   cd quantum_flow
#   python run_simulation_test.py
#
# 테스트 항목:
#   [1] 환경변수 + API 키 확인
#   [2] 텔레그램 연결 테스트 (실제 메시지 전송)
#   [3] KIS API 토큰 + 잔고 조회
#   [4] 호가단위(calc_limit_price) 계산 검증
#   [5] buy_with_fallback DRY_RUN 시뮬레이션
#   [6] 병렬 다종목 매수 DRY_RUN 시뮬레이션
#   [7] 매도 DRY_RUN 시뮬레이션
#   [8] 주문 집행 통합 시뮬레이션 (Agent3→Agent4 체인)
#   [9] 텔레그램 최종 결과 리포트 발송

import os
import sys
import asyncio
import traceback
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── 경로 추가 ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
INFO = "ℹ️"

results = {}


def section(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


def ok(tag: str, msg: str):
    results[tag] = True
    print(f"  {PASS} [{tag}] {msg}")


def fail(tag: str, msg: str):
    results[tag] = False
    print(f"  {FAIL} [{tag}] {msg}")


def warn(tag: str, msg: str):
    results[tag] = "WARN"
    print(f"  {WARN} [{tag}] {msg}")


# ══════════════════════════════════════════════════════════════
#  [1] 환경변수 확인
# ══════════════════════════════════════════════════════════════

section("[1] 환경변수 확인")

USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"
MODE_LABEL = "모의투자" if USE_PAPER else "실전투자"
print(f"  {INFO} 운용 모드: {MODE_LABEL}")

env_checks = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", ""),
    "KIS_ACCOUNT_NO":     os.getenv("KIS_ACCOUNT_NO", ""),
}
if USE_PAPER:
    env_checks["KIS_PAPER_APP_KEY"]    = os.getenv("KIS_PAPER_APP_KEY", "")
    env_checks["KIS_PAPER_APP_SECRET"] = os.getenv("KIS_PAPER_APP_SECRET", "")
else:
    env_checks["KIS_APP_KEY"]    = os.getenv("KIS_APP_KEY", "")
    env_checks["KIS_APP_SECRET"] = os.getenv("KIS_APP_SECRET", "")

all_env_ok = True
for k, v in env_checks.items():
    if v:
        masked = v[:4] + "***" + v[-2:] if len(v) > 8 else "***"
        print(f"  {PASS} {k}: {masked}")
    else:
        print(f"  {FAIL} {k}: 미설정")
        all_env_ok = False

if all_env_ok:
    ok("ENV", "모든 필수 환경변수 설정 완료")
else:
    fail("ENV", "일부 환경변수 누락 — .env 파일 확인 필요")


# ══════════════════════════════════════════════════════════════
#  [2] 텔레그램 연결 테스트
# ══════════════════════════════════════════════════════════════

section("[2] 텔레그램 연결 테스트")

try:
    from tools.notifier_tools import _send

    test_msg = (
        f"🤖 <b>QUANTUM FLOW v2.1 — 시뮬레이션 테스트</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"모드: {MODE_LABEL}\n"
        f"상태: 전체 시스템 시뮬레이션 시작\n"
        f"\n"
        f"<b>개선 사항 (v2.1):</b>\n"
        f"• KRX 호가단위 슬리피지 버퍼\n"
        f"• IOC+3틱 → IOC+5틱 → 시장가 3단계 폴백\n"
        f"• Agent3→4 실행 체인 연결\n"
        f"• 병렬 다종목 동시 매수 (asyncio)"
    )

    success = _send(test_msg)
    if success:
        ok("TELEGRAM", "텔레그램 메시지 전송 성공 — 모바일에서 확인하세요")
    else:
        warn("TELEGRAM", "텔레그램 전송 실패 (API키 미설정 또는 봇 오류)")
except Exception as e:
    fail("TELEGRAM", f"텔레그램 모듈 오류: {e}")


# ══════════════════════════════════════════════════════════════
#  [3] KIS API 토큰 + 잔고 조회
# ══════════════════════════════════════════════════════════════

section("[3] KIS API 잔고 조회")

balance = {"cash": 0, "positions": [], "total_eval": 0}
try:
    from tools.order_executor import get_balance, pre_warm_connection

    print("  연결 프리웜 중...")
    pre_warm_connection()

    balance = get_balance()
    if balance.get("cash", 0) > 0 or balance.get("total_eval", 0) > 0:
        ok("KIS_BALANCE", f"잔고 조회 성공: 예수금 {balance['cash']:,}원 / 총평가 {balance['total_eval']:,}원")
        for pos in balance.get("positions", []):
            print(f"    보유: {pos['name']}({pos['code']}) {pos['qty']}주 @{pos['avg_price']:,}원 "
                  f"{pos['pnl_pct']:+.2f}%")
    else:
        warn("KIS_BALANCE", "잔고 0원 (API 오류 또는 잔고 없음)")

except Exception as e:
    fail("KIS_BALANCE", f"KIS API 오류: {e}")
    print(f"    {traceback.format_exc()}")


# ══════════════════════════════════════════════════════════════
#  [4] 호가단위(calc_limit_price) 계산 검증
# ══════════════════════════════════════════════════════════════

section("[4] 호가단위 계산 검증")

try:
    from tools.order_executor import calc_limit_price, _get_tick_size

    test_cases = [
        # (ask1, n_ticks, expected_tick, description)
        (800,    3, 1,    "800원 → 틱1원"),
        (2500,   3, 5,    "2,500원 → 틱5원"),
        (8000,   3, 10,   "8,000원 → 틱10원"),
        (45000,  3, 50,   "45,000원 → 틱50원"),
        (72000,  3, 100,  "72,000원 → 틱100원 (삼성전자급)"),
        (180000, 3, 500,  "180,000원 → 틱500원"),
        (600000, 3, 1000, "600,000원 → 틱1,000원"),
        (72000,  5, 100,  "72,000원 +5틱 (Stage2 재입찰)"),
    ]

    all_tick_ok = True
    for ask1, n_ticks, expected_tick, desc in test_cases:
        tick = _get_tick_size(ask1)
        price = calc_limit_price(ask1, n_ticks)
        slippage = price - ask1
        expected_slippage = expected_tick * n_ticks
        tick_ok = (tick == expected_tick and slippage == expected_slippage)
        icon = PASS if tick_ok else FAIL
        print(f"  {icon} {desc}: ask1={ask1:,}원 → +{n_ticks}틱={slippage}원 → 주문가={price:,}원")
        if not tick_ok:
            all_tick_ok = False
            print(f"       기대: 틱={expected_tick}원, 슬리피지={expected_slippage}원")

    if all_tick_ok:
        ok("TICK_CALC", "모든 호가단위 계산 정확")
    else:
        fail("TICK_CALC", "일부 호가단위 계산 오류")

except Exception as e:
    fail("TICK_CALC", f"호가단위 모듈 오류: {e}")


# ══════════════════════════════════════════════════════════════
#  [5] buy_with_fallback DRY_RUN 시뮬레이션
# ══════════════════════════════════════════════════════════════

section("[5] buy_with_fallback DRY_RUN 시뮬레이션")

async def test_buy_fallback():
    try:
        from tools.order_executor import buy_with_fallback

        # 삼성전자 가상 ask1으로 테스트
        test_stocks = [
            ("005930", 10, 72000,  "삼성전자"),
            ("000660", 5,  188000, "SK하이닉스"),
            ("035420", 3,  52000,  "NAVER"),
        ]

        all_ok = True
        for code, qty, ask1, name in test_stocks:
            result = await buy_with_fallback(code, qty, ask1, dry_run=True)
            if result.get("success"):
                filled = result.get("filled_qty", 0)
                stage = result.get("stage_used", 0)
                price = result.get("final_price", 0)
                print(f"  {PASS} {name}({code}): {filled}/{qty}주 Stage{stage} @{price:,}원")
            else:
                print(f"  {FAIL} {name}({code}): 폴백 체인 실패")
                all_ok = False

        return all_ok
    except Exception as e:
        print(f"  {FAIL} buy_with_fallback 오류: {e}")
        print(traceback.format_exc())
        return False

fallback_ok = asyncio.run(test_buy_fallback())
if fallback_ok:
    ok("BUY_FALLBACK", "3단계 폴백 체인 DRY_RUN 통과")
else:
    fail("BUY_FALLBACK", "폴백 체인 오류")


# ══════════════════════════════════════════════════════════════
#  [6] 병렬 다종목 매수 DRY_RUN 시뮬레이션
# ══════════════════════════════════════════════════════════════

section("[6] 병렬 다종목 동시 매수 DRY_RUN")

async def test_parallel():
    try:
        from tools.order_executor import buy_parallel_entries

        entries = [
            {"code": "005930", "qty": 10,  "ask1": 72000},
            {"code": "000660", "qty": 5,   "ask1": 188000},
            {"code": "035420", "qty": 8,   "ask1": 52000},
            {"code": "051910", "qty": 2,   "ask1": 440000},
            {"code": "035720", "qty": 15,  "ask1": 45000},
        ]

        start_t = datetime.now()
        results_list = await buy_parallel_entries(entries, dry_run=True)
        elapsed = (datetime.now() - start_t).total_seconds()

        success_cnt = sum(1 for r in results_list if r.get("success"))
        print(f"  {INFO} 5종목 병렬 매수: {success_cnt}/5 성공  소요 {elapsed:.3f}초")

        for e, r in zip(entries, results_list):
            icon = PASS if r.get("success") else FAIL
            print(f"    {icon} {e['code']}: Stage{r.get('stage_used',0)} filled={r.get('filled_qty',0)}")

        return success_cnt == len(entries)
    except Exception as e:
        print(f"  {FAIL} 병렬 매수 오류: {e}")
        return False

parallel_ok = asyncio.run(test_parallel())
if parallel_ok:
    ok("PARALLEL_BUY", "5종목 병렬 DRY_RUN 통과")
else:
    fail("PARALLEL_BUY", "병렬 매수 오류")


# ══════════════════════════════════════════════════════════════
#  [7] 매도 DRY_RUN 시뮬레이션
# ══════════════════════════════════════════════════════════════

section("[7] 시장가 매도 DRY_RUN")

try:
    from tools.order_executor import sell_market

    result = sell_market("005930", qty=10, dry_run=True)
    if result.get("success"):
        ok("SELL_MARKET", "시장가 매도 DRY_RUN 통과")
    else:
        fail("SELL_MARKET", "시장가 매도 실패")
except Exception as e:
    fail("SELL_MARKET", f"오류: {e}")


# ══════════════════════════════════════════════════════════════
#  [8] 주문 집행 통합 시뮬레이션 (Agent3→4 전체 흐름)
# ══════════════════════════════════════════════════════════════

section("[8] Agent3→4 통합 주문 체인 시뮬레이션")

async def test_full_chain():
    """
    실제 장 환경을 시뮬레이션:
    1) 잔고 기반 투입금 계산
    2) ask1 가상 수신 (웹소켓 데이터 시뮬레이션)
    3) buy_with_fallback 실행
    4) 포지션 등록 확인
    5) sell_market 실행
    """
    try:
        from tools.order_executor import buy_with_fallback, sell_market

        # 잔고 시뮬레이션
        total_eval = balance.get("total_eval", 0) or 50_000_000  # 잔고 없으면 5천만원 가정
        position_pct = 0.20  # 20% 투입 (v2 기본)
        invest_amount = int(total_eval * position_pct)

        code = "005930"   # 삼성전자 (테스트용)
        ask1_sim = 72000  # 가상 매도1호가
        qty = max(1, invest_amount // ask1_sim)

        print(f"  {INFO} 총 평가금액(시뮬): {total_eval:,}원")
        print(f"  {INFO} 투입 비중: {position_pct:.0%} → {invest_amount:,}원")
        print(f"  {INFO} 종목: {code}, ask1: {ask1_sim:,}원, 수량: {qty}주")
        print()

        # 매수 실행 (DRY_RUN)
        print("  → 매수 체결 (DRY_RUN)...")
        buy_result = await buy_with_fallback(code, qty, ask1_sim, dry_run=True)

        if buy_result.get("success"):
            filled = buy_result.get("filled_qty", 0)
            stage = buy_result.get("stage_used", 0)
            print(f"  {PASS} 매수 완료: {filled}주 Stage{stage}")

            # 매도 실행 (DRY_RUN)
            print("  → 시장가 청산 (DRY_RUN)...")
            sell_result = sell_market(code, filled, dry_run=True)
            if sell_result.get("success"):
                print(f"  {PASS} 매도 완료: {filled}주 청산")
                return True
            else:
                print(f"  {FAIL} 매도 실패")
                return False
        else:
            print(f"  {FAIL} 매수 체인 실패")
            return False

    except Exception as e:
        print(f"  {FAIL} 통합 체인 오류: {e}")
        print(traceback.format_exc())
        return False

chain_ok = asyncio.run(test_full_chain())
if chain_ok:
    ok("FULL_CHAIN", "Agent3→4 전체 체인 DRY_RUN 통과")
else:
    fail("FULL_CHAIN", "통합 체인 오류")


# ══════════════════════════════════════════════════════════════
#  [9] 최종 결과 텔레그램 리포트
# ══════════════════════════════════════════════════════════════

section("[9] 최종 결과 텔레그램 리포트")

passed  = [k for k, v in results.items() if v is True]
failed  = [k for k, v in results.items() if v is False]
warned  = [k for k, v in results.items() if v == "WARN"]
total   = len(results)

summary_lines = []
for tag, status in results.items():
    icon = PASS if status is True else (FAIL if status is False else WARN)
    summary_lines.append(f"{icon} {tag}")

report_msg = (
    f"🤖 <b>QUANTUM FLOW v2.1 — 시뮬레이션 완료</b>\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"시각: {datetime.now().strftime('%H:%M:%S')}\n"
    f"모드: {MODE_LABEL}\n\n"
    f"<b>테스트 결과:</b>\n"
    f"✅ 통과: {len(passed)}/{total}\n"
    f"❌ 실패: {len(failed)}/{total}\n"
    f"⚠️ 경고: {len(warned)}/{total}\n\n"
    f"<b>상세:</b>\n"
    + "\n".join(summary_lines) +
    f"\n\n"
    f"{'🎉 전체 시뮬레이션 정상!' if not failed else '⚠️ 일부 항목 확인 필요'}"
)

try:
    from tools.notifier_tools import _send
    sent = _send(report_msg)
    if sent:
        ok("FINAL_REPORT", "텔레그램 최종 리포트 전송 완료")
    else:
        warn("FINAL_REPORT", "텔레그램 전송 실패 (설정 확인)")
except Exception as e:
    warn("FINAL_REPORT", f"리포트 전송 오류: {e}")


# ══════════════════════════════════════════════════════════════
#  최종 콘솔 요약
# ══════════════════════════════════════════════════════════════

section("최종 요약")
print(f"\n  총 {total}개 테스트: {PASS} {len(passed)}통과  {FAIL} {len(failed)}실패  {WARN} {len(warned)}경고\n")

for tag, status in results.items():
    icon = PASS if status is True else (FAIL if status is False else WARN)
    print(f"    {icon}  {tag}")

print()
if not failed:
    print("  🎉 전체 시뮬레이션 통과! 실전 운용 준비 완료.")
    sys.exit(0)
else:
    print(f"  ⚠️  {len(failed)}개 항목 실패. 위 로그 확인 후 재시도.")
    sys.exit(1)
