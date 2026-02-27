# agents/market_watcher.py — 시장 감시 에이전트 (Agent 4)
# Phase 6 구현: 거시 지표 모니터링, Risk-Off 이중 검증, 파라미터 조정

import os
import time
import threading

from datetime import datetime
from dotenv import load_dotenv
import logging
from tools.utils import safe_float

def safe_yf_download(ticker, period="5d", interval="1d", retries=3, **kwargs):
    """yfinance download with retry logic — yf.download 래퍼"""
    import yfinance as _yf
    for attempt in range(retries):
        try:
            data = _yf.download(ticker, period=period, interval=interval,
                                progress=False, timeout=10, **kwargs)
            if data is not None and not data.empty:
                return data
        except Exception as e:
            if attempt < retries - 1:
                print(f"    yf.download({ticker}) retry {attempt+1}/{retries}: {e}")
                time.sleep(2 * (attempt + 1))
            else:
                print(f"    yf.download({ticker}) failed after {retries} retries: {e}")
    return None

try:
    import yfinance as yf
except ImportError:
    yf = None

load_dotenv()

logger = logging.getLogger(__name__)

# ── config 참조 ───────────────────────────────────────────────
try:
    from config.settings import (
        VIX_SURGE_THRESHOLD, KOSPI_DROP_THRESHOLD, FX_CHANGE_THRESHOLD,
        MARKET_DROP_COUNT, RISK_OFF_TRIGGER_MIN, RISK_OFF_CONFIRM_WAIT,
        NEWS_CHECK_INTERVAL,
        INITIAL_STOP_ATR, TRAILING_STOP_ATR,
        RECOVERY_MIN_WAIT, RECOVERY_MAX_REENTRY, RECOVERY_POSITION_RATIO,
        STOCK_RAPID_CHANGE_PCT, STOCK_RAPID_ALERT_PCT,
        TRACK1_FORCE_CLOSE, TRACK2_EVAL_TIME, TRACK2_DECISION_TIME,
    )
    from shared_state import (
        get_state, set_state, update_risk_params, get_positions,
        set_tf15_trend, set_chg_strength, get_track_info,
    )
    from tools.notifier_tools import notify_risk_off, notify_error
    from tools.news_tools import build_news_context
    from tools.timeframe_tools import update_tf15, push_min1_bar, clear_buffers
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

    # 장중 감시 강화 기본값
    VIX_CAUTION_THRESHOLD    = 0.10
    KOSPI_CAUTION_THRESHOLD  = -0.01
    FX_CAUTION_THRESHOLD     = 10
    SP500_CAUTION_THRESHOLD  = -0.01
    SP500_ALERT_THRESHOLD    = -0.025
    STOCK_RAPID_CHANGE_PCT   = 0.03
    STOCK_RAPID_ALERT_PCT    = 0.05
    VOLUME_SPIKE_CAUTION     = 3.0
    VOLUME_SPIKE_ALERT       = 5.0

    def get_state(k): return None
    def set_state(k, v): pass
    def update_risk_params(p): pass
    def get_positions(): return {}
    def notify_risk_off(t, a, m="모의투자"): pass
    def notify_error(s, e, m="모의투자"): pass
    def build_news_context(c): return ""

from tools.llm_client import get_llm_client

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # 레거시: API 키 존재 여부 체크용
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
        self._ws_feeder = None  # KISWebSocketFeeder 참조

        # 이전 지표 저장 (변동률 계산용)
        self._prev: dict = {
            "vix":    None,
            "kospi":  None,
            "usdkrw": None,
        }

    def attach_ws_feeder(self, feeder):
        """
        웹소켓 피더를 연결하고 체결강도 콜백을 등록한다.
        main.py 또는 watcher_task.py에서 호출.

        사용법:
            feeder = KISWebSocketFeeder(stock_codes)
            watcher = MarketWatcher()
            watcher.attach_ws_feeder(feeder)
        """
        self._ws_feeder = feeder
        feeder.register_chg_callback(self._update_chg_strength_from_ws)
        print(f"  🔗 [{MODE_LABEL}] 웹소켓 피더 ↔ MarketWatcher 체결강도 콜백 연결 완료")

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
                except Exception as e:
                    logger.debug(f"agents/market_watcher.py: {type(e).__name__}: {e}")
                    pass
            time.sleep(self.check_interval)

    # ── 1. 단일 감시 주기 실행 ───────────────────────────────

    def check_cycle(self):
        """한 번의 감시 주기를 실행한다."""
        now_str = datetime.now().strftime("%H:%M:%S")
        now_hm = datetime.now().strftime("%H:%M")
        print(f"\n  🔭 [{MODE_LABEL}] 시장 감시 주기 시작 ({now_str})")

        # ── 장중 시간대: 15분봉/체결강도 실시간 갱신 (09:00~15:30) ──
        if "09:00" <= now_hm <= "15:30":
            self._update_timeframe_trends()

        # ── 14:30 Track 2 오버나이트 전환 판정 ──
        if now_hm == TRACK2_EVAL_TIME:
            self._trigger_track2_evaluation()

        # ── 15:10 Track 1 강제 청산 ──
        if now_hm == TRACK1_FORCE_CLOSE:
            self._trigger_track1_force_close()

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
                    except (ValueError, TypeError) as e:
                        logger.debug(f"agents/market_watcher.py: {type(e).__name__}: {e}")
                        pass
                return

            elif recovery_state == "WATCHING":
                self._check_recovery()
                return

            # RECOVERED 상태면 이미 해제됨 → 아래 정상 루프로 진행
            print("  ℹ️  Risk-Off 상태 유지 중 — 추가 점검 스킵")
            return

        # 정량 트리거 확인 (VIX 값도 함께 반환받아 중복 API 호출 방지)
        triggered, trigger_details, vix_now = self.check_quantitative_triggers()

        # ── [추가 리스크] VIX 레벨 기반 동적 파라미터 조정 ──
        self._adjust_by_vix_level(vix_now=vix_now)

        if len(triggered) >= RISK_OFF_TRIGGER_MIN:
            print(f"  ⚠️  트리거 {len(triggered)}개 발동: {trigger_details}")
            print(f"  ⏳ {RISK_OFF_CONFIRM_WAIT}초 유예 후 LLM 이중 검증...")
            time.sleep(RISK_OFF_CONFIRM_WAIT)

            # 유예 후 재확인 (일시적 노이즈 필터링)
            triggered2, _, _ = self.check_quantitative_triggers()
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
        (triggered: list, details: list[str], vix_now: float|None)
        """
        triggered = []
        details = []
        vix_now = None  # VIX 현재값 (중복 API 호출 방지용)

        # ─ VIX 급등 ───────────────────────────────────────
        try:
            vix_data = yf.download(VIX_TICKER, period="5d", interval="1d",
                                   progress=False)
            if len(vix_data) >= 2:
                vix_prev  = safe_float(vix_data["Close"].iloc[-2])
                vix_today = safe_float(vix_data["Close"].iloc[-1])
                vix_now = vix_today  # 저장하여 _adjust_by_vix_level에 전달
                vix_chg   = (vix_today - vix_prev) / (vix_prev or 1)

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
                                  progress=False)
            if len(ks_data) >= 2:
                ks_prev  = safe_float(ks_data["Close"].iloc[-2])
                ks_today = safe_float(ks_data["Close"].iloc[-1])
                ks_chg   = (ks_today - ks_prev) / (ks_prev or 1)

                print(f"    KOSPI: {ks_today:,.0f}  (전일대비 {ks_chg:+.2%})")

                if ks_chg <= KOSPI_DROP_THRESHOLD:
                    triggered.append("KOSPI_DROP")
                    details.append(f"KOSPI {ks_chg:+.2%} (기준 {KOSPI_DROP_THRESHOLD:.0%})")
        except Exception as e:
            print(f"    ⚠️  KOSPI 조회 실패: {e}")

        # ─ 달러/원 급변 ──────────────────────────────────
        try:
            fx_data = yf.download(USDKRW_TICKER, period="5d", interval="1d",
                                  progress=False)
            if len(fx_data) >= 2:
                fx_prev  = safe_float(fx_data["Close"].iloc[-2])
                fx_today = safe_float(fx_data["Close"].iloc[-1])
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
                    d = safe_yf_download(ticker, period="5d", interval="1d",
                                    progress=False)
                    if len(d) >= 2:
                        chg = (safe_float(d["Close"].iloc[-1]) - safe_float(d["Close"].iloc[-2])) / safe_float(d["Close"].iloc[-2])
                        if chg < 0:
                            drop_count += 1
                except Exception as e:
                    logger.debug(f"agents/market_watcher.py: {type(e).__name__}: {e}")
                    pass

            print(f"    시총상위5 하락: {drop_count}종목")

            # 비율로 환산 (5개 중 → 10개 기준 추정: 비율 유지)
            estimated_drop = int(drop_count / 5 * 10)
            if estimated_drop >= MARKET_DROP_COUNT:
                triggered.append("MARKET_DROP")
                details.append(f"시총상위 하락 ~{estimated_drop}종목 (기준 {MARKET_DROP_COUNT}종목)")

        except Exception as e:
            print(f"    ⚠️  시총 상위 조회 실패: {e}")

        return triggered, details, vix_now

    # ── 2.5 다중 타임프레임 갱신 + Track 관리 ─────────────────

    def _update_timeframe_trends(self):
        """
        감시 종목 + 보유 종목에 대해 15분봉 추세를 갱신한다.
        매 check_cycle()마다 호출 (장중 09:00~15:30).
        갱신된 추세 정보는 shared_state에 저장되어 Agent 3이 참조.
        """
        # 갱신 대상: 보유 종목 + 감시 리스트
        positions = get_positions()
        watch_list = get_state("watch_list") or []

        # 중복 제거하여 타겟 코드 수집
        target_codes = set(positions.keys())
        for item in watch_list:
            if isinstance(item, dict):
                target_codes.add(item.get("code", ""))
            elif isinstance(item, str):
                target_codes.add(item)
        target_codes.discard("")

        if not target_codes:
            return

        updated = 0
        for code in target_codes:
            try:
                # 15분봉 추세 갱신 (1분봉 버퍼 기반 리샘플링)
                trend_data = update_tf15(code)
                set_tf15_trend(code, trend_data)
                updated += 1
            except Exception as e:
                logger.debug(f"_update_timeframe_trends({code}): {e}")

        if updated > 0:
            logger.info(f"15분봉 추세 갱신: {updated}종목")

    def _update_chg_strength_from_ws(self, code: str, strength: float):
        """
        웹소켓 피더에서 체결강도를 수신받아 shared_state에 저장.
        websocket_feeder.py의 콜백으로 등록하여 사용.

        Parameters:
            code:     종목코드 (예: "005930")
            strength: 체결강도 (누적매수/누적매도 비율)
        """
        set_chg_strength(code, strength)

    def _trigger_track2_evaluation(self):
        """
        14:30 Track 2 오버나이트 전환 판정.
        HeadStrategist.evaluate_track2_transition()을 호출하여
        보유 종목의 오버나이트 보유 여부를 결정한다.
        """
        print(f"\n  🌙 [{MODE_LABEL}] 14:30 Track 2 오버나이트 판정 트리거")
        try:
            from agents.head_strategist import HeadStrategist
            strategist = HeadStrategist()
            decisions = strategist.evaluate_track2_transition()

            # 텔레그램 알림
            hold_count = sum(1 for d in decisions if d["action"] == "HOLD_OVERNIGHT")
            close_count = sum(1 for d in decisions if d["action"] == "CLOSE")
            if decisions:
                try:
                    from tools.notifier_tools import _send
                    msg = (f"🌙 <b>Track 2 오버나이트 판정</b>\n"
                           f"보유: {hold_count}건 | 청산: {close_count}건")
                    for d in decisions:
                        action_emoji = "🌙" if d["action"] == "HOLD_OVERNIGHT" else "💰"
                        msg += f"\n  {action_emoji} {d['code']}: {d['reason'][:40]}"
                    _send(msg)
                except Exception:
                    pass

        except ImportError:
            print("  ⚠️  HeadStrategist import 실패 — Track 2 판정 스킵")
        except Exception as e:
            print(f"  ❌ Track 2 판정 오류: {e}")
            try:
                notify_error("MarketWatcher.Track2Eval", str(e), MODE_LABEL)
            except Exception:
                pass

    def _trigger_track1_force_close(self):
        """
        15:10 Track 1 장중 포지션 강제 청산.
        HeadStrategist.get_track1_close_list()로 청산 대상을 받아
        주문 실행기에 전달한다.
        """
        print(f"\n  🔒 [{MODE_LABEL}] 15:10 Track 1 강제 청산 트리거")
        try:
            from agents.head_strategist import HeadStrategist
            from shared_state import remove_position, add_to_blacklist

            strategist = HeadStrategist()
            close_codes = strategist.get_track1_close_list()

            if not close_codes:
                print("  ✅ Track 1 청산 대상 없음 (전부 Track 2 또는 빈 포지션)")
                return

            print(f"  🔒 Track 1 강제 청산 대상: {close_codes}")
            for code in close_codes:
                try:
                    # 시장가 청산 주문 (dry_run 포함)
                    from tools.order_tools import place_market_sell
                    pos = get_positions().get(code, {})
                    qty = pos.get("quantity", 0)
                    if qty > 0:
                        place_market_sell(code, qty, reason="Track1 15:10 강제청산")
                    remove_position(code)
                    add_to_blacklist(code)
                    print(f"    ✅ {code}: 청산 완료 (수량 {qty})")
                except ImportError:
                    print(f"    ⚠️ {code}: order_tools 미구현 — 로그만 기록")
                    remove_position(code)
                except Exception as e:
                    print(f"    ❌ {code}: 청산 실패 — {e}")

            # 텔레그램 알림
            try:
                from tools.notifier_tools import _send
                msg = (f"🔒 <b>Track 1 강제청산 ({TRACK1_FORCE_CLOSE})</b>\n"
                       f"대상: {len(close_codes)}건\n"
                       f"종목: {', '.join(close_codes)}")
                _send(msg)
            except Exception:
                pass

        except ImportError as e:
            print(f"  ⚠️  import 실패: {e}")
        except Exception as e:
            print(f"  ❌ Track 1 강제 청산 오류: {e}")
            try:
                notify_error("MarketWatcher.Track1Close", str(e), MODE_LABEL)
            except Exception:
                pass

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
        try:
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

            prompt = f"""당신은 한국 주식 시장 Risk 관리 전문가입니다.
아래 상황에서 즉각적인 Risk-Off 선언(전 포지션 청산 + 신규 매수 중단)이 필요한지 판단해주세요.

[발동된 거시 지표 트리거]
{trigger_str}

{pos_summary}

{news_ctx if news_ctx else '최근 관련 뉴스 없음'}

판단 기준:
- YES: 시장 붕괴 위험이 높아 즉각 청산이 필요한 경우
- NO: 일시적 노이즈로 파라미터 조정만으로 충분한 경우

반드시 'YES' 또는 'NO' 한 단어만 첫 줄에 답하고, 그 이유를 한 문장으로 설명하세요."""

            llm = get_llm_client()
            answer = llm.classify(prompt, temperature=0.1, max_tokens=100)
            first_line = answer.split("\n")[0].strip().upper()
            confirm = first_line.startswith("YES")

            print(f"  🤖 [LLM 검증] 답변: {answer[:100]}")
            print(f"  🤖 [LLM 검증] 최종 판단: {'Risk-Off 확정' if confirm else '파라미터 조정만'}")

            return confirm

        except Exception as e:
            print(f"  ⚠️  [LLM] 검증 오류: {e} — 정량 판단 따름")
            return True   # 오류 시 안전을 위해 Risk-Off 선언

    # ── 3.5 장중 감시 강화 메서드들 ────────────────────────

    def _handle_caution(self, triggered: list, details: list):
        """
        주의 단계: GPT-4o-mini로 빠르게 상황 판단.
        결과에 따라 파라미터 조정 또는 거시 재분석 트리거.
        """
        detail_str = "; ".join(details)
        prompt = f"""한국 주식시장 감시 중 다음 지표가 감지되었습니다:
{detail_str}

이것이 1) 일시적 노이즈인지, 2) 파라미터 조정이 필요한 수준인지, 3) 거시 전략 재분석이 필요한 수준인지 판단하세요.

반드시 첫 줄에 NOISE / ADJUST / REANALYZE 중 하나만 답하고, 이유를 한 줄로 설명하세요."""

        try:
            llm = get_llm_client()
            answer = llm.classify(prompt, temperature=0.1, max_tokens=100)
            first_line = answer.split("\n")[0].strip().upper()
            print(f"  🤖 [주의 판단] {answer[:80]}")

            if "REANALYZE" in first_line:
                print("  🔄 거시 재분석 트리거!")
                self._trigger_macro_reanalysis(detail_str)
            elif "ADJUST" in first_line:
                print("  ⚙️ 파라미터 소폭 조정")
                update_risk_params({
                    "risk_level": "MEDIUM",
                    "pyramiding_allowed": False,
                })
            else:
                print("  ✅ 노이즈로 판단 — 유지")
        except Exception as e:
            print(f"  ⚠️ 주의 판단 오류: {e}")

    def _trigger_macro_reanalysis(self, reason: str):
        """
        장중 거시 재분석을 트리거한다.
        macro_analyst의 경량 재분석 함수를 호출.
        """
        try:
            from agents.macro_analyst import run_intraday_reanalysis
            import asyncio

            print("  🔄 장중 경량 거시 재분석 시작...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(run_intraday_reanalysis(reason))
            finally:
                loop.close()

            if result:
                new_strategy = result.get("strategy", "?")
                new_pct = result.get("position_size_pct", 0)
                print(f"  ✅ 재분석 완료: 전략={new_strategy}, 비중={new_pct:.0%}")

                # 텔레그램 알림
                try:
                    from tools.notifier_tools import _send
                    msg = (f"⚡ <b>장중 거시 재분석</b>\n"
                           f"사유: {reason[:60]}\n"
                           f"전략: {new_strategy} | 비중: {new_pct:.0%}")
                    _send(msg)
                except Exception:
                    pass
        except ImportError:
            print("  ⚠️ run_intraday_reanalysis 미구현 — 스킵")
        except Exception as e:
            print(f"  ⚠️ 재분석 실패: {e}")

    def _trigger_emergency_rescan(self, reason: str):
        """
        장중 긴급 재스캔을 트리거한다.
        market_scanner의 경량 재스캔 함수를 호출.
        """
        try:
            from agents.market_scanner import run_emergency_rescan
            import asyncio

            print("  🔍 장중 긴급 재스캔 시작...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(run_emergency_rescan(reason))
            finally:
                loop.close()

            updated = result.get("updated_count", 0)
            print(f"  ✅ 재스캔 완료: {updated}종목 감시 리스트 갱신")
        except ImportError:
            print("  ⚠️ run_emergency_rescan 미구현 — 스킵")
        except Exception as e:
            print(f"  ⚠️ 재스캔 실패: {e}")

    def _check_intraday_news(self):
        """
        장중 뉴스를 주기적으로 체크하여 긴급 뉴스 감지 시 대응.
        """
        try:
            from tools.macro_data_tools import check_urgent_news, collect_macro_news
            news = collect_macro_news()
            if not news:
                return

            urgent = check_urgent_news(news)
            level = urgent.get("level", "LOW")

            if level == "CRITICAL":
                print(f"  🚨 긴급 뉴스 감지! (CRITICAL)")
                self._trigger_macro_reanalysis("긴급 뉴스: " + str(urgent.get("urgent_items", [])[:2]))
            elif level == "HIGH":
                print(f"  ⚠️ 주요 뉴스 감지 (HIGH)")
                # GPT-4o-mini로 보유 종목 연관성 빠른 체크
                self._check_news_impact(urgent)
        except Exception as e:
            logger.debug(f"_check_intraday_news: {e}")

    def _check_news_impact(self, urgent_info: dict):
        """
        HIGH 등급 뉴스가 보유 종목에 영향을 주는지 GPT-4o-mini로 빠르게 판단.
        """
        positions = get_positions()
        if not positions:
            return

        items = urgent_info.get("urgent_items", [])
        headlines = "; ".join([i.get("title", "") for i in items[:3]])
        codes = list(positions.keys())[:5]

        prompt = f"""뉴스: {headlines}
보유종목: {codes}
이 뉴스가 보유 종목에 부정적 영향을 줄 가능성이 있나요?
YES 또는 NO로만 답하세요."""

        try:
            llm = get_llm_client()
            answer = llm.classify(prompt, temperature=0.1, max_tokens=50)
            if answer.strip().upper().startswith("YES"):
                print(f"  📰 뉴스→보유종목 영향 있음 → 파라미터 조정")
                update_risk_params({
                    "risk_level": "HIGH",
                    "pyramiding_allowed": False,
                })
        except Exception as e:
            logger.debug(f"_check_news_impact: {e}")

    def _check_position_alerts(self):
        """
        보유 종목의 급변 감지 (가격 급등락, 거래량 폭증).
        """
        positions = get_positions()
        if not positions or not yf:
            return

        for code, data in list(positions.items())[:5]:  # API 부하 제한
            try:
                ticker = f"{code}.KS"
                d = yf.download(ticker, period="1d", interval="5m",
                               progress=False)
                if d is None or len(d) < 2:
                    continue

                # 최근 5분 변동률
                latest = float(d["Close"].iloc[-1])
                prev_5m = float(d["Close"].iloc[-2])
                chg_5m = (latest - prev_5m) / (prev_5m or 1)

                if abs(chg_5m) >= STOCK_RAPID_ALERT_PCT:
                    print(f"  🚨 {code}: 5분내 {chg_5m:+.1%} 급변! (경고)")
                    self._trigger_macro_reanalysis(f"보유종목 {code} 급변 {chg_5m:+.1%}")
                elif abs(chg_5m) >= STOCK_RAPID_CHANGE_PCT:
                    print(f"  ⚡ {code}: 5분내 {chg_5m:+.1%} 변동 (주의)")

            except Exception as e:
                logger.debug(f"_check_position_alerts {code}: {e}")

    # ── 3.6 [추가 리스크] VIX 레벨별 동적 조정 ────────────
    
    def _adjust_by_vix_level(self, vix_now=None):
        """
        VIX 절대 레벨에 따라 리스크 파라미터를 동적으로 조정.
        백테스트 분석 결과 매크로 상관계수가 낮으므로(r=0.058),
        하드 필터 대신 트레일링 스탑 완화/강화로 사용.

        Parameters
        ----------
        vix_now : float or None
            check_quantitative_triggers()에서 이미 조회한 VIX 값.
            None이면 별도 조회 (중복 호출 방지를 위해 가급적 전달).
        """
        try:
            from config.settings import (
                VIX_NORMAL_MAX, VIX_CAUTION_MAX, VIX_HIGH_MAX,
                VIX_TRAIL_ADJUSTMENT,
            )
        except ImportError:
            return  # 설정 미정의 시 스킵

        # vix_now가 전달되지 않은 경우에만 별도 조회 (폴백)
        if vix_now is None:
            if not yf:
                return
            try:
                vix_data = yf.download(VIX_TICKER, period="2d", interval="1d",
                                       progress=False)
                if vix_data is None or len(vix_data) < 1:
                    return
                vix_now = float(vix_data["Close"].iloc[-1])
            except Exception:
                return

        try:

            if vix_now <= VIX_NORMAL_MAX:
                vix_level = "NORMAL"
            elif vix_now <= VIX_CAUTION_MAX:
                vix_level = "CAUTION"
            elif vix_now <= VIX_HIGH_MAX:
                vix_level = "HIGH"
            else:
                vix_level = "EXTREME"

            adjustment = VIX_TRAIL_ADJUSTMENT.get(vix_level, 1.0)

            # shared_state에 VIX 레벨 저장 (다른 에이전트 참조용)
            set_state("vix_level", vix_level)
            set_state("vix_value", round(vix_now, 2))
            set_state("vix_trail_adjustment", adjustment)

            if vix_level != "NORMAL":
                print(f"  📊 [VIX] {vix_now:.1f} ({vix_level}) → 트레일링 조정 x{adjustment}")

                if vix_level == "EXTREME":
                    # VIX 30+ → 신규 진입 사실상 차단
                    update_risk_params({
                        "risk_level": "HIGH",
                        "pyramiding_allowed": False,
                    })
                elif vix_level == "HIGH":
                    update_risk_params({
                        "pyramiding_allowed": False,
                    })

        except Exception as e:
            logger.debug(f"_adjust_by_vix_level: {e}")

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

        triggered, _, _ = self.check_quantitative_triggers()

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
        try:
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

            llm = get_llm_client()
            answer = llm.classify(prompt, temperature=0.1, max_tokens=100)
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
        except Exception as e:
            logger.debug(f"agents/market_watcher.py: {type(e).__name__}: {e}")
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
            except Exception as e:
                logger.debug(f"agents/market_watcher.py: {type(e).__name__}: {e}")
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
        triggered, details, _ = watcher.check_quantitative_triggers()
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

