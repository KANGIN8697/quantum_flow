# head_strategist.py — 헤드 전략가 에이전트 (Agent 3)
# 최종 매매 결정 + 포트폴리오 관리 + 포지션 사이징
# stock_eval의 position_pct + 매크로 전략을 종합하여 최종 주문 결정
# v2.1 개선: Agent3→4 실제 매수 실행 연결 (buy_with_fallback 3단계 폴백)

import os
import logging
from datetime import datetime

from shared_state import (
    get_state, set_state, get_positions,
    add_position, remove_position, add_to_blacklist,
    get_tf15_trend, get_chg_strength, set_track_info,
    get_track_info, update_track_pnl,
)
from config.settings import (
    MAX_POSITIONS, POSITION_SIZE_RATIO,
    DAILY_LOSS_LIMIT, RECOVERY_POSITION_RATIO,
    OPENING_RUSH_END, OPENING_RUSH_POS_MULT,
    INTRADAY_TP_PCT, INTRADAY_TP_ENABLED,
    CHG_STRENGTH_THRESHOLD,
    TRACK1_FORCE_CLOSE,
    TRACK2_QUALIFY_PNL, TRACK2_EVAL_TIME, TRACK2_CHG_MIN,
    TRACK2_MAX_POSITIONS, TRACK2_DECISION_TIME,
    INTRADAY_TIME_WEIGHT,
    # v2 신규 (2026-02-27)
    MACRO_BOOST_ENABLED, KOSPI_STRONG_MOMENTUM_PCT,
    USD_STRONG_MA20_ABOVE, MACRO_BOOST_POSITION_MULT,
    MACRO_BOOST_MAX_POSITIONS,
    EVENT_FILTER_ENABLED, EVENT_MIN_DAY_RETURN,
    EVENT_WEAK_POSITION_MULT,
    TAKE_PROFIT_PCT, TIME_STOP_DAYS, TRAILING_STOP_PCT,
)

from tools.trade_logger import log_trade, log_signal, log_risk_event
from tools.notifier_tools import notify_trade_decision

logger = logging.getLogger("head_strategist")


def _tg_notify(msg: str):
    """텔레그램 알림 헬퍼 (실패 시 조용히 무시)."""
    try:
        from tools.notifier_tools import _send
        _send(msg)
    except Exception:
        pass

class HeadStrategist:
    """헤드 전략가 — 최종 매매 결정 및 포트폴리오 관리"""

    def __init__(self):
        self.name = "Head Strategist"

    async def run(self) -> dict:
        """
        비동기 실행:
        1) Risk-OFF / 일일 손실 한도 체크
        2) watch_list에서 매수 후보 확인
        3) 포지션 사이징 (매크로 + stock_eval 반영)
        4) 기존 포지션 관리 (추가매수 / 청산 판단)
        """
        print(f"\n{'='*50}")
        print(f"  [{self.name}] 전략 분석 시작")
        print(f"{'='*50}")

        actions_taken = []

        # 1) Risk 체크
        risk_off = get_state("risk_off")
        daily_loss = get_state("daily_loss") or 0.0
        risk_params = get_state("risk_params") or {}
        risk_level = risk_params.get("risk_level", "NORMAL")

        if risk_off:
            print(f"  ⛔ Risk-OFF — 전체 매매 중단")
            if risk_params.get("emergency_liquidate"):
                actions = self._emergency_liquidate()
                actions_taken.extend(actions)
            return {
                "status": "risk_off",
                "actions": actions_taken,
                "message": "Risk-OFF: 매매 중단",
            }

        if daily_loss <= DAILY_LOSS_LIMIT:
            print(f"  ⛔ 일일 손실 한도 도달: {daily_loss:.2%}")
            log_risk_event("DAILY_LOSS_LIMIT", level="HIGH",
                           message=f"일일 손실 한도 도달: {daily_loss:.2%}")
            return {
                "status": "loss_limit",
                "actions": [],
                "message": f"일일 손실 한도 {daily_loss:.2%}",
            }

        # 2) 매크로 전략 확인
        macro = get_state("macro_result") or {}
        macro_position_pct = macro.get("position_size_pct", 0.5)
        strategy = macro.get("strategy", "중립")

        # ── [매크로 필터 1] Neutral 레짐 차단 (신뢰도: 높음) ──
        # 백테스트 결과: 1/3/5/10일 전진수익률 전부에서 Neutral이 꼴찌
        # 추세추종 전략은 방향성 없는 시장에서 페이크아웃에 반복 노출
        # macro_analyst에서 명시적으로 분류한 regime 필드를 우선 참조
        macro_regime = macro.get("regime", "")
        if macro_regime == "Neutral" or (not macro_regime and strategy == "중립"):
            print(f"  🚫 [매크로 필터] Neutral 레짐 → 신규 매수 차단 (regime={macro_regime})")
            log_risk_event("NEUTRAL_REGIME_BLOCK", level="MEDIUM",
                           message=f"Neutral 레짐 차단 (regime={macro_regime}, confidence={macro.get('confidence', '?')})")
            return {
                "status": "neutral_blocked",
                "actions": [],
                "message": "Neutral 레짐: 추세추종 신호 신뢰도 낮음 → 매수 대기",
            }

        # ── [매크로 필터 2] 달러 강세 소프트 필터 (N=65, 소표본) ──
        # 하드 차단 대신 포지션 30% 축소
        usdkrw_change = macro.get("usdkrw_change_pct", 0)
        if isinstance(usdkrw_change, (int, float)) and usdkrw_change > 0.5:
            macro_position_pct *= 0.7
            print(f"  ⚠ [매크로 필터] 달러 강세(+{usdkrw_change:.1f}%) → 포지션 30% 축소")

        # ── [매크로 필터 3] KOSPI 5일 모멘텀 가중치 (소프트 필터, p=0.03) ──
        # 하드 레버리지 1.2배 → 보수적 10% 가산으로 대체
        kospi_5d_pct = macro.get("kospi_5d_change_pct", 0)
        if isinstance(kospi_5d_pct, (int, float)) and kospi_5d_pct >= 2.0:
            macro_position_pct *= 1.1
            print(f"  📈 [매크로 필터] KOSPI 5일 +{kospi_5d_pct:.1f}% → 포지션 10% 가산")

        # ── [매크로 필터 4] 매크로 부스트 — KOSPI5d>3% & 달러강세 (v2, p=0.0295 ★) ──
        # 50,000회 백테스트: 해당 국면 평균 수익률 4.26% vs 베이스 2.19% (+94%)
        macro_boost_active = False
        max_positions_effective = MAX_POSITIONS
        if MACRO_BOOST_ENABLED:
            usd_above_ma20 = macro.get("usd_above_ma20", False)
            if (isinstance(kospi_5d_pct, (int, float)) and
                    kospi_5d_pct >= KOSPI_STRONG_MOMENTUM_PCT and usd_above_ma20):
                macro_boost_active = True
                macro_position_pct *= MACRO_BOOST_POSITION_MULT
                max_positions_effective = MACRO_BOOST_MAX_POSITIONS
                print(f"  🚀 [매크로 부스트] KOSPI5d +{kospi_5d_pct:.1f}% & 달러강세"
                      f" → 포지션 +{(MACRO_BOOST_POSITION_MULT-1)*100:.0f}%, 한도 {max_positions_effective}종목")
                log_risk_event("MACRO_BOOST", level="INFO",
                               message=f"매크로 부스트 활성: KOSPI5d={kospi_5d_pct:.1f}%, USD>MA20")

        print(f"\n  매크로 전략: {strategy}")
        print(f"  매크로 포지션 비중: {macro_position_pct*100:.0f}%")
        print(f"  리스크 레벨: {risk_level}")
        if macro_boost_active:
            print(f"  🚀 매크로 부스트: 활성 (최대 {max_positions_effective}종목)")

        # 3) 현재 포지션 확인
        positions = get_positions()
        current_count = len(positions)
        print(f"  현재 보유: {current_count}/{max_positions_effective}종목")

        # 4) 신규 매수 검토
        if current_count < max_positions_effective:
            watch_list = get_state("watch_list") or []
            scanner_result = get_state("scanner_result") or {}
            selected = scanner_result.get("selected") or []

            # selected에서 code → 상세 정보 매핑
            selected_map = {}
            for s in selected:
                if isinstance(s, dict):
                    selected_map[s.get("code", "")] = s

            # ── [백테스트 기반] 오프닝 러시 차단 ──────────────────────────
            # 09:20 이전 진입은 오프닝 러시로 손실 비율 최고 → 차단
            now_time = datetime.now().strftime("%H:%M")
            _opening_blocked = (now_time < OPENING_RUSH_END and OPENING_RUSH_POS_MULT == 0.0)
            if _opening_blocked:
                print(f"  🚫 [오프닝 러시] {now_time} < {OPENING_RUSH_END} → 신규 진입 차단 (백테스트 근거)")
                log_risk_event("OPENING_RUSH_BLOCK", level="LOW",
                               message=f"오프닝 러시 구간 신규 매수 차단 ({now_time})")

            for code in watch_list:
                if current_count >= max_positions_effective:
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="max_positions")
                    break
                if _opening_blocked:
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="opening_rush_block")
                    continue  # 오프닝 러시 차단
                if code in positions:
                    continue  # 이미 보유
                blacklist = get_state("blacklist") or []
                if code in blacklist:
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="blacklisted")
                    continue  # 블랙리스트

                # ── [2트랙] 15분봉 정배열 필터 (핵심: 승률 30%→43%) ──
                tf15 = get_tf15_trend(code)
                if tf15 and not tf15.get("aligned", False):
                    print(f"    {code}: 15분봉 비정배열({tf15.get('trend','?')}) → 진입 차단")
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason=f"tf15_not_aligned({tf15.get('trend','?')})")
                    continue

                # ── [2트랙] 체결강도 필터 (CHG≥0.70) ──
                chg = get_chg_strength(code)
                if chg > 0 and chg < CHG_STRENGTH_THRESHOLD:
                    print(f"    {code}: 체결강도 {chg:.2f} < {CHG_STRENGTH_THRESHOLD} → 진입 차단")
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason=f"chg_str_low({chg:.2f})")
                    continue

                # ── [2트랙] 시간대별 포지션 가중치 적용 ──
                time_weight = 1.0
                for tw_time, tw_val in sorted(INTRADAY_TIME_WEIGHT.items(), reverse=True):
                    if now_time >= tw_time:
                        time_weight = tw_val
                        break

                # 포지션 사이즈 결정
                info = selected_map.get(code, {})
                eval_pct = info.get("position_pct", 0.5)

                # ── [v2 필터5] 이벤트 부재 신호 약화 (p=0.003 ★) ──
                # 뉴스분석: 기술신호+이벤트없음 = 1.06% vs +이벤트 = 2.06%
                # 당일 수익률 < 0% (음봉)인데 돌파 발생 → 신뢰도 낮음
                event_mult = 1.0
                if EVENT_FILTER_ENABLED:
                    day_return = info.get("day_return_pct", None)
                    vol_ratio = info.get("vol_ratio", 0)
                    if day_return is not None and day_return < EVENT_MIN_DAY_RETURN and vol_ratio < 3.0:
                        event_mult = EVENT_WEAK_POSITION_MULT
                        print(f"    {code}: 이벤트 미동반 (당일 {day_return:.1%}, vol {vol_ratio:.1f}x)"
                              f" → 포지션 {EVENT_WEAK_POSITION_MULT:.0%}로 축소")
                        log_signal(code, "EVENT_FILTER", executed=True,
                                   skip_reason=f"event_weak(ret={day_return:.2%},vol={vol_ratio:.1f})")

                # 최종 포지션 = 기본비율 x 매크로비중 x 평가비중 x 시간가중 x 이벤트배수
                final_pct = POSITION_SIZE_RATIO * macro_position_pct * eval_pct * time_weight * event_mult

                # [기능3] Recovery 재진입 시 포지션 축소
                recovery_state = get_state("recovery_state")
                if recovery_state == "RECOVERED":
                    final_pct *= RECOVERY_POSITION_RATIO

                # 방어적 전략이면 추가 축소
                if strategy == "방어적":
                    final_pct *= 0.5
                elif strategy == "공격적":
                    final_pct *= 1.2

                # 상한: 매크로 부스트 시 상한도 비례 확대
                pos_cap = POSITION_SIZE_RATIO * (MACRO_BOOST_POSITION_MULT if macro_boost_active else 1.0)
                final_pct = min(final_pct, pos_cap)

                if final_pct < 0.02:
                    print(f"    {code}: 포지션 너무 작음 ({final_pct:.1%}) — 스킵")
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="position_too_small",
                               eval_grade=info.get("eval_grade", "?"))
                    continue

                action = {
                    "type": "BUY",
                    "code": code,
                    "position_pct": round(final_pct, 3),
                    "eval_grade": info.get("eval_grade", "?"),
                    "reason": f"watch_list 매수 ({strategy}, {final_pct:.1%})",
                    "timestamp": datetime.now().isoformat(),
                }
                actions_taken.append(action)
                print(f"    📈 매수 결정: {code} ({final_pct:.1%})")

                # ── [Agent 3→4] 실제 매수 주문 실행 ──────────────────
                # buy_with_fallback: IOC+3틱 → IOC+5틱 → 시장가 3단계 폴백
                exec_result = None
                try:
                    from tools.order_executor import buy_with_fallback, get_balance
                    from tools.websocket_feeder import get_latest_quote

                    # 1) 잔고 조회로 투입 금액 계산
                    balance = get_balance()
                    total_eval = balance.get("total_eval", 0) or balance.get("cash", 0)
                    invest_amount = int(total_eval * final_pct)

                    # 2) 웹소켓에서 실시간 호가 가져오기
                    quote = get_latest_quote(code)
                    ask1 = quote.get("ask1", 0) if quote else 0

                    if ask1 <= 0 or invest_amount <= 0:
                        logger.warning(f"    {code}: ask1={ask1}, invest={invest_amount} → 매수 건너뜀")
                    else:
                        qty = max(1, invest_amount // ask1)
                        print(f"    💰 {code}: 투입 {invest_amount:,}원 / ask1 {ask1:,}원 / {qty}주 주문")

                        # 3단계 폴백 체인으로 매수
                        exec_result = await buy_with_fallback(
                            code, qty, ask1,
                            dry_run=(os.getenv("DRY_RUN", "false").lower() == "true"),
                            notify_fn=lambda msg: _tg_notify(msg),
                        )

                        if exec_result and exec_result.get("success"):
                            filled = exec_result.get("filled_qty", qty)
                            final_price = exec_result.get("final_price", ask1)
                            stage = exec_result.get("stage_used", 1)
                            print(f"    ✅ {code} 체결 완료: {filled}주 Stage{stage}")
                            action["filled_qty"] = filled
                            action["entry_price"] = final_price
                            action["stage_used"] = stage
                        else:
                            print(f"    ❌ {code} 매수 실패 — 포지션 등록 스킵")
                            actions_taken.remove(action)
                            continue

                except ImportError as e:
                    # 모듈 미로드 시 (테스트/개발 환경) 매수 스킵 후 계속
                    logger.debug(f"order_executor import 실패(개발환경): {e}")
                except Exception as e:
                    logger.error(f"    {code} 매수 주문 오류: {e}")
                    try:
                        from tools.notifier_tools import _send
                        _send(f"⚠️ {code} 매수 주문 오류: {e}")
                    except Exception:
                        pass

                # ── 텔레그램 매매 알림 ───────────────────────────────
                try:
                    notify_trade_decision(
                        "BUY", code, final_pct,
                        info.get("eval_grade", "?"), strategy,
                        action["reason"],
                    )
                except Exception as e:
                    logger.debug(f"agents/head_strategist.py: {type(e).__name__}: {e}")
                    pass

                # 매매 기록 저장
                log_trade("BUY", code,
                          position_pct=round(final_pct, 3),
                          eval_grade=info.get("eval_grade", "?"),
                          eval_score=info.get("eval_score"),
                          sector=info.get("sector", ""),
                          strategy=strategy,
                          reason=action["reason"])
                log_signal(code, "BUY_SIGNAL", executed=True,
                           eval_grade=info.get("eval_grade", "?"),
                           eval_score=info.get("eval_score"))

                # 실제 체결가 기반으로 포지션 등록
                entry_price_actual = (
                    action.get("entry_price", 0)
                    or info.get("entry_price", 0)
                )
                add_position(code, {
                    "entry_pct": final_pct,
                    "eval_grade": info.get("eval_grade", "?"),
                    "sector": info.get("sector", ""),
                    "entry_time": datetime.now().isoformat(),
                    "pyramiding_done": False,
                    "pyramid_count": 0,
                    "entry_atr": info.get("entry_atr", 0),
                    "filled_qty": action.get("filled_qty", 0),
                    "entry_price": entry_price_actual,
                })
                # ── [2트랙] Track 1로 초기 태깅 ──
                set_track_info(code, track=1,
                               entry_price=entry_price_actual,
                               entry_time=datetime.now().strftime("%H:%M:%S"))
                current_count += 1

        # 5) 기존 포지션 관리 (추가매수 판단)
        if risk_params.get("pyramiding_allowed", True):
            for code, pos_data in positions.items():
                if pos_data.get("pyramiding_done"):
                    continue
                # 추가매수 조건은 실시간 가격 필요 — 여기서는 구조만 준비
                # market_watcher에서 실시간 가격 + 트리거 발동 시 호출
        result = {
            "status": "success",
            "actions": actions_taken,
            "positions_count": current_count,
            "strategy": strategy,
            "message": f"{len(actions_taken)}건 매매 결정",
            "timestamp": datetime.now().isoformat(),
        }

        set_state("strategist_result", result)

        print(f"\n  ✅ [{self.name}] 전략 완료")
        print(f"     매매 결정: {len(actions_taken)}건")
        print(f"     보유 종목: {current_count}/{max_positions_effective}")

        return result

    def evaluate_track2_transition(self) -> list:
        """
        14:30 Track 2 오버나이트 전환 판정.
        Agent 4가 14:30에 호출한다.

        전환 조건 (4개 AND):
          1) 장중 미실현 수익 ≥ +3%
          2) 15분봉 여전히 정배열
          3) 체결강도 ≥ 0.60
          4) 정보 카탈리스트 존재 (뉴스/공시/기관매수)

        Returns:
            [{"code": str, "action": "HOLD_OVERNIGHT"|"CLOSE",
              "pnl_pct": float, "reason": str}, ...]
        """
        print(f"\n  🌙 [Track 2] 오버나이트 전환 판정 시작 ({TRACK2_EVAL_TIME})")

        positions = get_positions()
        decisions = []
        overnight_count = 0

        for code, pos in positions.items():
            track = get_track_info(code)
            if track.get("track") == 2:
                overnight_count += 1
                continue  # 이미 Track 2

            # 미실현 수익률 확인
            entry_price = pos.get("entry_price", 0)
            current_price = pos.get("current_price", 0)
            if entry_price <= 0 or current_price <= 0:
                continue
            pnl_pct = (current_price - entry_price) / entry_price

            # 조건 1: 수익률 +3%↑
            if pnl_pct < TRACK2_QUALIFY_PNL:
                decisions.append({
                    "code": code, "action": "CLOSE",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"수익률 {pnl_pct:.1%} < {TRACK2_QUALIFY_PNL:.0%} → Track 1 청산",
                })
                continue

            # 조건 2: 15분봉 정배열 유지
            tf15 = get_tf15_trend(code)
            if not tf15.get("aligned", False):
                decisions.append({
                    "code": code, "action": "CLOSE",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"수익 {pnl_pct:.1%}이나 15분봉 비정배열 → 청산",
                })
                continue

            # 조건 3: 체결강도 유지
            chg = get_chg_strength(code)
            if chg > 0 and chg < TRACK2_CHG_MIN:
                decisions.append({
                    "code": code, "action": "CLOSE",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"체결강도 {chg:.2f} < {TRACK2_CHG_MIN} → 청산",
                })
                continue

            # 조건 4: 카탈리스트 확인 (뉴스/공시 — LLM 호출 또는 캐시)
            # Agent 2의 scanner_result에서 catalyst 태그 확인
            scanner_result = get_state("scanner_result") or {}
            selected = scanner_result.get("selected") or []
            has_catalyst = False
            catalyst_reason = ""
            for s in selected:
                if isinstance(s, dict) and s.get("code") == code:
                    # news_positive, sector_momentum 등 확인
                    if s.get("catalyst"):
                        has_catalyst = True
                        catalyst_reason = s.get("catalyst", "")
                    elif s.get("score", 0) >= 70:
                        has_catalyst = True
                        catalyst_reason = f"eval_score={s.get('score',0)}"
                    break

            # 동시 오버나이트 한도 체크
            if overnight_count >= TRACK2_MAX_POSITIONS:
                decisions.append({
                    "code": code, "action": "CLOSE",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"오버나이트 한도 {TRACK2_MAX_POSITIONS}종목 초과 → 청산",
                })
                continue

            # 모든 조건 충족 → Track 2 전환
            if has_catalyst or pnl_pct >= 0.05:
                # 카탈리스트 있거나 수익 5%↑이면 보유
                set_track_info(code, track=2,
                               entry_price=entry_price,
                               entry_time=pos.get("entry_time", ""))
                overnight_count += 1
                decisions.append({
                    "code": code, "action": "HOLD_OVERNIGHT",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"🌙 오버나이트 전환 (수익 {pnl_pct:.1%}, 카탈리스트={catalyst_reason or 'high_pnl'})",
                })
                print(f"    🌙 {code}: Track 2 전환 (수익 {pnl_pct:.1%})")

                log_risk_event("TRACK2_TRANSITION", level="INFO",
                               message=f"{code} 오버나이트 전환 pnl={pnl_pct:.1%}")
            else:
                decisions.append({
                    "code": code, "action": "CLOSE",
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": f"수익 {pnl_pct:.1%}이나 카탈리스트 없음 → 청산",
                })

        set_state("overnight_candidates", decisions)
        print(f"  🌙 [Track 2] 판정 완료: "
              f"{sum(1 for d in decisions if d['action']=='HOLD_OVERNIGHT')}건 보유, "
              f"{sum(1 for d in decisions if d['action']=='CLOSE')}건 청산")
        return decisions

    def get_track1_close_list(self) -> list:
        """
        15:10 Track 1 강제 청산 대상 목록 반환.
        Agent 4가 15:10에 호출.
        Track 2로 전환된 종목은 제외.
        """
        positions = get_positions()
        close_list = []
        for code in positions:
            track = get_track_info(code)
            if track.get("track", 1) == 1:
                close_list.append(code)
        return close_list

    def _emergency_liquidate(self) -> list:
        """긴급 전량 청산"""
        positions = get_positions()
        actions = []
        log_risk_event("EMERGENCY_LIQUIDATE",
                       level="CRITICAL",
                       message=f"{len(positions)}종목 긴급 전량 청산")
        for code in list(positions.keys()):
            actions.append({
                "type": "SELL_ALL",
                "code": code,
                "reason": "긴급 전량 청산 (CRITICAL)",
                "timestamp": datetime.now().isoformat(),
            })
            log_trade("FORCE_CLOSE", code,
                      reason="긴급 전량 청산 (CRITICAL)",
                      position_pct=positions[code].get("entry_pct", 0))
            remove_position(code)
            add_to_blacklist(code)

            try:
                notify_trade_decision(
                    "FORCE_CLOSE", code,
                    positions[code].get("entry_pct", 0), "?",
                    "긴급청산", "긴급 전량 청산 (CRITICAL)",
                )
            except Exception as e:
                logger.debug(f"agents/head_strategist.py: {type(e).__name__}: {e}")
                pass
            print(f"    🚨 긴급 청산: {code}")
        return actions

async def head_strategist_run() -> dict:
    """헤드 전략가 실행 함수 — async def로 정의"""
    strategist = HeadStrategist()
    return await strategist.run()
