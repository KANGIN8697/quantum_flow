# agents/market_watcher.py — 시장 감시 에이전트 (Agent 4)
# Phase 6 구현: 거시 지표 모니터링, Risk-Off 이중 검증, 파라미터 조정

import os
import time
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv

try:
    import yfinance as yf
except ImportError:
    yf = None

load_dotenv()

# ── config 참조 ───────────────────────────────────────────────
try:
    from config.settings import (
        VIX_SURGE_THRESHOLD, KOSPI_DROP_THRESHOLD, FX_CHANGE_THRESHOLD,
        MARKET_DROP_COUNT, RISK_OFF_TRIGGER_MIN, RISK_OFF_CONFIRM_WAIT,
        NEWS_CHECK_INTERVAL,
        INITIAL_STOP_ATR, TRAILING_STOP_ATR,
        RECOVERY_MIN_WAIT, RECOVERY_MAX_REENTRY, RECOVERY_POSITION_RATIO,
    )
    from shared_state import get_state, set_state, update_risk_params, get_positions
    from tools.notifier_tools import notify_risk_off, notify_error
    from tools.news_tools import build_news_context
except ImportError:
    # 독립 실행 시 기본값
    VIX_SURGE_THRESHOLD    = 0.20
    KOSPI_DROP_THRESHOLD   = -0.02
    FX_CHANGE_THRESHOLD    = 15
    MARKET_DROP_COUNT      = 7
    RISK_OFF_TRIGGER_MIN   = 2
    RISK_OFF_CONFIRM_WAIT  = 60
    NEWS_CHECK_INTERVAL    = 20
    INITIAL_STOP_ATR       = 2.0
    TRAILING_STOP_ATR      = 3.0
    RECOVERY_MIN_WAIT      = 1800
    RECOVERY_MAX_REENTRY   = 1
    RECOVERY_POSITION_RATIO = 0.6

    def get_state(k): return None
    def set_state(k, v): pass
    def update_risk_params(p): pass
    def get_positions(): return {}
    def notify_risk_off(t, a, m="모의투자"): pass
    def notify_error(s, e, m="모의투자"): pass
    def build_news_context(c): return ""

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODE_LABEL = "모의투자" if os.getenv("USE_PAPER", "true").lower() == "true" else "실전투자"

# ── 감시 대상 티커 ────────────────────────────────────────────
VIX_TICKER    = "^VIX"
KOSPI_TICKER  = "^KS11"
USDKRW_TICKER = "KRW=X"

# 코스피 시총 상위 10 (ETF/종목코드)
TOP10_TICKERS = [
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    "005380.KS",  # 현대차
    "035420.KS",  # NAVER
    "000270.KS",  # 기아
    "051910.KS",  # LG화학
    "068270.KS",  # 셀트리온
    "105560.KS",  # KB금융
    "055550.KS",  # 신한지주
    "035720.KS",  # 카카오
]


class MarketWatcher:
    """
    거시 지표를 주기적으로 모니터링하고
    Risk-Off 조건 충족 시 이중 검증(정량 + LLM)으로 선언한다.
    """

    def __init__(self, check_interval: int = 60):
        """
        Parameters
        ----------
        check_interval : 감시 주기 (초, 기본 60초)
        """
        self.check_interval = check_interval
        self._running = False
        self._thread: threading.Thread | None = None

        # 이전 지표 저장 (변동률 계산용)
        self._prev: dict = {
            "vix":    None,
            "kospi":  None,
            "usdkrw": None,
        }

    # ── 실행 / 중지 ──────────────────────────────────────────

    def run(self):
        """별도 스레드에서 감시 루프를 시작한다."""
        if self._running:
            print("⚠️  MarketWatcher가 이미 실행 중입니다.")
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"🔭 [{MODE_LABEL}] MarketWatcher 시작 (주기: {self.check_interval}초)")

    def stop(self):
        """감시 루프를 중지한다."""
        self._running = False
        print(f"🛑 [{MODE_LABEL}] MarketWatcher 중지")

    def _loop(self):
        """메인 감시 루프 (별도 스레드)."""
        while self._running:
            try:
                self.check_cycle()
            except Exception as e:
                print(f"  ❌ [MarketWatcher] 주기 오류: {e}")
                try:
                    notify_error("MarketWatcher._loop", str(e), MODE_LABEL)
                except Exception:
                    pass
            time.sleep(self.check_interval)

    # ── 1. 단일 감시 주기 실행 ───────────────────────────────

    def check_cycle(self):
        """한 번의 감시 주기를 실행한다."""
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n  🔭 [{MODE_LABEL}] 시장 감시 주기 시작 ({now})")

        # [기능3] Risk-Off 상태에서 Recovery Watch 체크
        if get_state("risk_off"):
            recovery_state = get_state("recovery_state") or "NONE"
            reentry_count = get_state("reentry_count") or 0

            if reentry_count >= RECOVERY_MAX_REENTRY:
                print("  ℹ️  Risk-Off 유지 (최대 재진입 횟수 도달)")
                return

            # Recovery Watch 상태머신
            if recovery_state == "NONE":
                # Risk-Off 후 최소 대기 시간 경과 확인
                risk_off_time_str = get_state("risk_off_time")
                if risk_off_time_str:
                    try:
                        risk_off_dt = datetime.fromisoformat(risk_off_time_str)
                        elapsed = (datetime.now() - risk_off_dt).total_seconds()
                        if elapsed >= RECOVERY_MIN_WAIT:
                            set_state("recovery_state", "WATCHING")
                            print(f"  🔍 Recovery Watch 시작 ({elapsed/60:.0f}분 경과)")
                        else:
                            remaining = (RECOVERY_MIN_WAIT - elapsed) / 60
                            print(f"  ℹ️  Risk-Off 대기 중 (잔여 {remaining:.0f}분)")
                    except (ValueError, TypeError):
                        pass
                return

            elif recovery_state == "WATCHING":
                self._check_recovery()
                return

            # RECOVERED 상태면 이미 해제됨 → 아래 정상 루프로 진행
            print("  ℹ️  Risk-Off 상태 유지 중 — 추가 점검 스킵")
            return

        # 정량 트리거 확인
        triggered, trigger_details = self.check_quantitative_triggers()

        if len(triggered) >= RISK_OFF_TRIGGER_MIN:
            print(f"  ⚠️  트리거 {len(triggered)}개 발동: {trigger_details}")
            print(f"  ⏳ {RISK_OFF_CONFIRM_WAIT}초 유예 후 LLM 이중 검증...")
            time.sleep(RISK_OFF_CONFIRM_WAIT)

            # 유예 후 재확인 (일시적 노이즈 필터링)
            triggered2, _ = self.check_quantitative_triggers()
            if len(triggered2) >= RISK_OFF_TRIGGER_MIN:
                llm_confirm = self.check_llm_context(trigger_details)
                if llm_confirm:
                    self.declare_risk_off(triggered2, trigger_details)
                else:
                    self.adjust_params_only(triggered2)
            else:
                print("  ✅ 재확인 결과 트리거 해소 — Risk-Off 선언 취소")
        else:
            print(f"  ✅ 정상 범위 (트리거 {len(triggered)}개 / 기준 {RISK_OFF_TRIGGER_MIN}개)")

    # ── 2. 정량 트리거 확인 ──────────────────────────────────

    def check_quantitative_triggers(self) -> tuple:
        """
        4가지 정량 지표를 확인하여 발동된 트리거 목록을 반환한다.

        Returns
        -------
        (triggered: list, details: list[str])
        """
        triggered = []
        details = []

        # ─ VIX 급등 ───────────────────────────────────────
        try:
            vix_data = yf.download(VIX_TICKER, period="5d", interval="1d",
                                   progress=False, auto_adjust=True)
            if len(vix_data) >= 2:
                vix_prev  = float(vix_data["Close"].iloc[-2])
                vix_today = float(vix_data["Close"].iloc[-1])
                vix_chg   = (vix_today - vix_prev) / vix_prev

                if self._prev["vix"] is None:
                    self._prev["vix"] = vix_prev

                print(f"    VIX: {vix_today:.2f}  (전일대비 {vix_chg:+.1%})")

                if vix_chg >= VIX_SURGE_THRESHOLD:
                    triggered.append("VIX_SURGE")
                    details.append(f"VIX +{vix_chg:.1%} (기준 +{VIX_SURGE_THRESHOLD:.0%})")
        except Exception as e:
            print(f"    ⚠️  VIX 조회 실패: {e}")

        # ─ 코스피 급락 ────────────────────────────────────
        try:
            ks_data = yf.download(KOSPI_TICKER, period="5d", interval="1d",
                                  progress=False, auto_adjust=True)
            if len(ks_data) >= 2:
                ks_prev  = float(ks_data["Close"].iloc[-2])
                ks_today = float(ks_data["Close"].iloc[-1])
                ks_chg   = (ks_today - ks_prev) / ks_prev

                print(f"    KOSPI: {ks_today:,.0f}  (전일대비 {ks_chg:+.2%})")

                if ks_chg <= KOSPI_DROP_THRESHOLD:
                    triggered.append("KOSPI_DROP")
                    details.append(f"KOSPI {ks_chg:+.2%} (기준 {KOSPI_DROP_THRESHOLD:.0%})")
        except Exception as e:
            print(f"    ⚠️  KOSPI 조회 실패: {e}")

        # ─ 달러/원 급변 ──────────────────────────────────
        try:
            fx_data = yf.download(USDKRW_TICKER, period="5d", interval="1d",
                                  progress=False, auto_adjust=True)
            if len(fx_data) >= 2:
                fx_prev  = float(fx_data["Close"].iloc[-2])
                fx_today = float(fx_data["Close"].iloc[-1])
                fx_chg   = abs(fx_today - fx_prev)

                print(f"    USD/KRW: {fx_today:.1f}  (전일대비 {fx_today - fx_prev:+.1f}원)")

                if fx_chg >= FX_CHANGE_THRESHOLD:
                    triggered.append("FX_SURGE")
                    details.append(f"USD/KRW ±{fx_chg:.0f}원 (기준 ±{FX_CHANGE_THRESHOLD}원)")
        except Exception as e:
            print(f"    ⚠️  FX 조회 실패: {e}")

        # ─ 시총 상위 10 하락 종목 수 ─────────────────────
        try:
            drop_count = 0
            for ticker in TOP10_TICKERS[:5]:   # API 부하 감소를 위해 5개만
                try:
                    d = yf.download(ticker, period="5d", interval="1d",
                                    progress=False, auto_adjust=True)
                    if len(d) >= 2:
                        chg = (float(d["Close"].iloc[-1]) - float(d["Close"].iloc[-2])) / float(d["Close"].iloc[-2])
                        if chg < 0:
                            drop_count += 1
                except Exception:
                    pass

            print(f"    시총상위5 하락: {drop_count}종목")

            # 비율로 환산 (5개 중 → 10개 기준 추정: 비율 유지)
            estimated_drop = int(drop_count / 5 * 10)
            if estimated_drop >= MARKET_DROP_COUNT:
                triggered.append("MARKET_DROP")
                details.append(f"시총상위 하락 ~{estimated_drop}종목 (기준 {MARKET_DROP_COUNT}종목)")

        except Exception as e:
            print(f"    ⚠️  시총 상위 조회 실패: {e}")

        return triggered, details

    # ── 3. LLM 이중 검증 ────────────────────────────────────

    def check_llm_context(self, trigger_details: list) -> bool:
        """
        OpenAI GPT를 사용하여 Risk-Off 선언의 타당성을 이중 검증한다.

        트리거 상황 + 현재 포지션 + 최근 뉴스를 바탕으로
        GPT가 'YES'/'NO'로 판단.

        Returns
        -------
        bool: True면 Risk-Off 선언 확정
        """
        if not OPENAI_API_KEY:
            print("  ⚠️  [LLM] OPENAI_API_KEY 없음 — 정량 판단만 사용")
            return True   # API 키 없으면 정량 판단 따름

        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)

            # 현재 포지션 정보
            positions = get_positions()
            pos_summary = ""
            if positions:
                pos_lines = []
                for code, data in positions.items():
                    pnl = data.get("pnl_pct", 0)
                    pos_lines.append(f"  - {code}: 수익률 {pnl:+.2f}%")
                pos_summary = "\n현재 보유 포지션:\n" + "\n".join(pos_lines)
            else:
                pos_summary = "\n현재 보유 포지션: 없음"

            # 뉴스 생성텍스트 (보유 종목 첫 번째)
            news_ctx = ""
            if positions:
                first_code = list(positions.keys())[0]
                news_ctx = build_news_context(first_code)

            trigger_str = "\n".join(f"- {t}" for t in trigger_details)

            prompt = f"""
당신은 한국 주식 시장 Risk 관리 전문가입니다.
아래 상황에서 즉각적인 Risk-Off 선언(전 포지션 청산 + 신규 매수 중단)이 필요한지 판단해주세요.

[발동된 거시 지표 트리거]
{trigger_str}

{pos_summary}

{news_ctx if news_ctx else '최근 관련 뉴스 없음'}

판단 기준:
- YES: 시장 붕괴 위험이 높아 즉각 청산이 필요한 경우
- NO: 일시적 노이즈로 파라미터 조정만으로 충분한 경우

반드시 'YES' 또는 'NO' 한 단어만 첫 줄에 답하고, 그 이유를 한 문장으로 설명하세요.
""".strip()

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.1,
            )

            answer = response.choices[0].message.content.strip()
            first_line = answer.split("\n")[0].strip().upper()
            confirm = first_line.startswith("YES")

            print(f"  🤖 [LLM 검증] 답변: {answer[:100]}")
            print(f"  🤖 [LLM 검증] 최종 판단: {'Risk-Off 확정' if confirm else '파라미터 조정만'}")

            return confirm

        except Exception as e:
            print(f"  ⚠️  [LLM] 검증 오류: {e} — 정량 판단 따름")
            return True   # 오류 시 안전을 위해 Risk-Off 선언

    # ── 4. Risk-Off 선언 ────────────────────────────────────

    def declare_risk_off(self, triggered: list, details: list):
        """
        Risk-Off를 선언하고 shared_state를 업데이트한다.
        포지션 청산은 head_strategist가 감지하여 실행.
        """
        print(f"\n  🚨 [{MODE_LABEL}] ⚡ RISK-OFF 선언! 트리거: {triggered}")

        set_state("risk_off", True)
        set_state("risk_off_time", datetime.now().isoformat())
        set_state("recovery_state", "NONE")
        update_risk_params({
            "risk_level":          "CRITICAL",
            "stop_loss_multiplier": 1.5,
            "pyramiding_allowed":  False,
            "emergency_liquidate": True,
        })

        try:
            notify_risk_off(
                triggers=details,
                action="⚡ 신규 매수 중단 + 전 포지션 긴급 청산 신호",
                mode=MODE_LABEL,
            )
        except Exception as e:
            print(f"  ⚠️  [텔레그램] Risk-Off 알림 실패: {e}")

    # ── 5. 파라미터 조정만 (Risk-Off 미선언) ────────────────

    def adjust_params_only(self, triggered: list):
        """
        Risk-Off 선언 없이 리스크 파라미터만 강화한다.
        LLM이 'NO'로 판단한 경우 실행.
        """
        print(f"  ⚡ [{MODE_LABEL}] 파라미터 조정 (HIGH 모드): {triggered}")

        update_risk_params({
            "risk_level":          "HIGH",
            "stop_loss_multiplier": 1.8,
            "pyramiding_allowed":  False,
            "emergency_liquidate": False,
        })

        print("  ✅ 리스크 파라미터 HIGH 모드로 전환 완료")

    # ── 6. [기능3] Recovery Watch (V자 반등 재진입) ─────────

    def _check_recovery(self):
        """
        Risk-Off 상태에서 정량 트리거 해소 여부를 확인하고,
        해소 시 LLM에 재검증하여 매매를 재개한다.
        """
        print("  🔍 Recovery Watch: 정량 트리거 해소 확인 중...")

        triggered, _ = self.check_quantitative_triggers()

        if len(triggered) >= RISK_OFF_TRIGGER_MIN:
            print(f"  ⚠️  트리거 {len(triggered)}개 유지 — Recovery 불가")
            return

        print(f"  ✅ 트리거 해소 ({len(triggered)}개) — LLM 안정화 검증 진행...")

        # LLM에 "시장 안정화" 재질의
        llm_stable = self._check_llm_recovery()

        if llm_stable:
            self._execute_recovery()
        else:
            print("  ⚠️  LLM 판단: 아직 불안정 — Recovery Watch 유지")

    def _check_llm_recovery(self) -> bool:
        """
        LLM에게 '시장이 안정화되었는가?' 재질의.
        """
        if not OPENAI_API_KEY:
            print("  ⚠️  [LLM] OPENAI_API_KEY 없음 — 정량 기준만 사용")
            return True

        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)

            risk_off_time = get_state("risk_off_time") or "불명"

            prompt = f"""당신은 한국 주식 시장 Risk 관리 전문가입니다.
현재 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Risk-Off 선언 시각: {risk_off_time}

아까 Risk-Off를 선언했으나, 현재 정량 지표(VIX, KOSPI, 환율, 대형주)가
모두 정상 범위로 회복되었습니다.

시장이 충분히 안정화되어 보수적 매매 재개가 가능한지 판단해주세요.

판단 기준:
- YES: 시장이 안정화되어 보수적 재진입 가능
- NO: 아직 불확실하므로 Risk-Off 유지 권장

반드시 'YES' 또는 'NO' 한 단어만 첫 줄에 답하세요."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.1,
            )

            answer = response.choices[0].message.content.strip()
            first_line = answer.split("\n")[0].strip().upper()
            stable = first_line.startswith("YES")

            print(f"  🤖 [LLM Recovery] 답변: {answer[:100]}")
            print(f"  🤖 [LLM Recovery] 판단: {'안정화 확인' if stable else '불안정 유지'}")

            return stable

        except Exception as e:
            print(f"  ⚠️  [LLM] Recovery 검증 오류: {e} — 보수적으로 대기 유지")
            return False

    def _execute_recovery(self):
        """
        Recovery 실행: Risk-Off 해제 + 보수적 파라미터로 매매 재개.
        """
        reentry_count = (get_state("reentry_count") or 0) + 1

        set_state("risk_off", False)
        set_state("recovery_state", "RECOVERED")
        set_state("reentry_count", reentry_count)

        update_risk_params({
            "risk_level": "HIGH",
            "stop_loss_multiplier": 1.5,
            "pyramiding_allowed": False,
            "emergency_liquidate": False,
            "position_pct": RECOVERY_POSITION_RATIO,
        })

        print(f"\n  🟢 [{MODE_LABEL}] Recovery 완료! 매매 재개 ({reentry_count}회차)")
        print(f"     포지션 비율: {RECOVERY_POSITION_RATIO*100:.0f}% (보수적)")
        print(f"     피라미딩: 비활성")

        try:
            notify_error("MarketWatcher.Recovery",
                         f"Risk-Off 해제, 보수적 매매 재개 ({reentry_count}회차)",
                         MODE_LABEL)
        except Exception:
            pass


# ── main.py 진입점 ─────────────────────────────────────────────

async def market_watcher_run():
    """
    main.py에서 호출하는 시장 감시 진입점.
    MarketWatcher를 생성하고 asyncio executor에서 블로킹 루프를 실행.
    KeyboardInterrupt가 발생할 때까지 장중 감시를 계속한다.
    """
    import asyncio

    watcher = MarketWatcher(check_interval=60)
    watcher._running = True
    print(f"🔭 [{MODE_LABEL}] MarketWatcher 시작 (주기: {watcher.check_interval}초)")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _watcher_blocking_loop, watcher)


def _watcher_blocking_loop(watcher: MarketWatcher):
    """executor 내에서 실행되는 블로킹 감시 루프"""
    while watcher._running:
        try:
            watcher.check_cycle()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  ❌ [MarketWatcher] 주기 오류: {e}")
            try:
                notify_error("MarketWatcher", str(e), MODE_LABEL)
            except Exception:
                pass
        time.sleep(watcher.check_interval)
    print(f"🛑 [{MODE_LABEL}] MarketWatcher 종료")


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM FLOW — MarketWatcher 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 60)

    watcher = MarketWatcher(check_interval=60)

    print("\n[1] 정량 트리거 즉시 확인 (1회)...")
    print("    (yfinance로 실시간 데이터 조회 — 인터넷 필요)")

    try:
        triggered, details = watcher.check_quantitative_triggers()
        print(f"\n  발동 트리거: {len(triggered)}개 / 기준 {RISK_OFF_TRIGGER_MIN}개")
        for d in details:
            print(f"    • {d}")

        if len(triggered) >= RISK_OFF_TRIGGER_MIN:
            print(f"\n  ⚠️  기준 충족! LLM 검증 대상")
        else:
            print(f"\n  ✅ 정상 범위")

    except Exception as e:
        print(f"\n  ❌ 오류: {e}")
        print("  💡 인터넷 연결 및 yfinance 설치 확인하세요.")
        print("     pip install yfinance --break-system-packages")

    print("\n[2] 백그라운드 감시 루프 테스트 (5초만 실행)...")
    watcher.run()
    time.sleep(5)
    watcher.stop()

    print("\n" + "=" * 60)
    print("  ✅ MarketWatcher 테스트 완료!")
    print("=" * 60)
