# tools/notifier_tools.py — 텔레그램 알림 전송 툴
# Phase 5 구현: 매수/매도/Risk-Off/오류/일일 리포트 알림

import os
import requests
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── HTTP 세션 (텔레그램 API, TCP 재사용 + 자동 재시도) ──────────
_TG_RETRY = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
_TG_SESSION = requests.Session()
_TG_SESSION.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=4, max_retries=_TG_RETRY))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_PHOTO_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"


def _send(text, parse_mode="HTML"):
    """텔레그램 메시지 전송. 성공 True, 실패 False (예외 없음)."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  [텔레그램] 설정 없음 — 콘솔: {text[:60]}...")
        return False
    try:
        resp = _TG_SESSION.post(
            TELEGRAM_API,
            json={"chat_id":CHAT_ID,"text":text,"parse_mode":parse_mode},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [텔레그램] 전송 실패: {e}")
        return False


def send_image(image_path: str, caption: str = "") -> bool:
    """텔레그램으로 이미지 파일 전송. 대시보드 이미지 전송에 사용."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  [텔레그램] 설정 없음 — 이미지: {image_path}")
        return False
    if not os.path.exists(image_path):
        print(f"  [텔레그램] 이미지 파일 없음: {image_path}")
        return False
    try:
        with open(image_path, "rb") as img:
            data = {"chat_id": CHAT_ID}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = _TG_SESSION.post(
                TELEGRAM_PHOTO_API,
                data=data,
                files={"photo": img},
                timeout=30,
            )
        resp.raise_for_status()
        print(f"  [텔레그램] 이미지 전송 완료: {os.path.basename(image_path)}")
        return True
    except Exception as e:
        print(f"  [텔레그램] 이미지 전송 실패: {e}")
        return False


def send_alert(message):
    """단순 텍스트 경보. websocket_feeder 등 내부 모듈에서 호출."""
    return _send(f"  {message}")


def notify_buy(code, name, qty, price, score, stop_loss, mode="모의투자"):
    """매수 체결 알림."""
    now = datetime.now().strftime("%H:%M:%S")
    text = (
        f" <b>[매수 체결]</b>  {now}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"수량: {qty:,}주  단가: {price:,}원\n"
        f"금액: {qty*price:,}원\n"
        f"신호점수: {score}점\n"
        f"초기손절: {stop_loss:,}원\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_sell(code, name, qty, price, entry_price, reason, mode="모의투자"):
    """매도 체결 알림."""
    pnl_pct = (price-entry_price)/ (entry_price or 1)*100 if entry_price>0 else 0.0
    pnl_amt = (price-entry_price)*qty
    emoji = " " if pnl_pct<0 else " "
    now = datetime.now().strftime("%H:%M:%S")
    text = (
        f"{emoji} <b>[매도 체결]</b>  {now}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"수량: {qty:,}주  단가: {price:,}원\n"
        f"평단: {entry_price:,}원  수익률: {pnl_pct:+.2f}%\n"
        f"손익금: {pnl_amt:+,}원\n"
        f"사유: {reason}\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_risk_off(triggers, action, mode="모의투자"):
    """Risk-Off 선언 알림."""
    now = datetime.now().strftime("%H:%M:%S")
    trigger_str = "\n".join(f"  • {t}" for t in triggers)
    text = (
        f" <b>[RISK-OFF 선언]</b>  {now}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"발동 트리거:\n{trigger_str}\n"
        f"조치: <b>{action}</b>\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_error(source, error_msg, mode="모의투자"):
    """시스템 오류 알림."""
    now = datetime.now().strftime("%H:%M:%S")
    text = (
        f" <b>[시스템 오류]</b>  {now}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"발생위치: {source}\n"
        f"오류내용: {error_msg[:300]}\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_daily_report(total_trades, win_count, loss_count, total_pnl,
                        total_pnl_pct, positions_held, mode="모의투자"):
    """장 마감 후 일일 거래 결과 리포트 전송."""
    today = datetime.now().strftime("%Y-%m-%d")
    win_rate = (win_count/ (total_trades or 1)*100) if total_trades>0 else 0.0
    pnl_emoji = " " if total_pnl>=0 else " "
    overnight_str = ""
    if positions_held:
        overnight_str = "\n\n  오버나이트 보유:\n"
        for pos in positions_held:
            overnight_str += f"  • {pos['name']}({pos['code']})  {pos['pnl_pct']:+.2f}%\n"
    text = (
        f"  <b>[일일 리포트]</b>  {today}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"총 거래: {total_trades}건  (익절 {win_count} / 손절 {loss_count})\n"
        f"승률: {win_rate:.1f}%\n"
        f"{pnl_emoji} 총 손익: {total_pnl:+,.0f}원  ({total_pnl_pct:+.2f}%)"
        f"{overnight_str}\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_trade_decision(action_type, code, position_pct, eval_grade,
                          strategy, reason, mode="모의투자"):
    """매매 결정 알림 (head_strategist에서 호출)."""
    now = datetime.now().strftime("%H:%M:%S")
    if action_type == "BUY":
        emoji = "🟢"
        label = "매수 결정"
    elif action_type == "SELL_ALL":
        emoji = "🔴"
        label = "전량 매도"
    elif action_type == "PYRAMID":
        emoji = "🔵"
        label = "추가 매수"
    elif action_type == "FORCE_CLOSE":
        emoji = "🚨"
        label = "긴급 청산"
    elif action_type == "OVERNIGHT_HOLD":
        emoji = "🌙"
        label = "오버나이트 홀딩"
    elif action_type == "OVERNIGHT_STOP":
        emoji = "🌅"
        label = "오버나이트 손절"
    else:
        emoji = "⚪"
        label = action_type

    text = (
        f"{emoji} <b>[{label}]</b>  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"종목: <b>{code}</b>\n"
        f"비중: {position_pct:.1%}  등급: {eval_grade}\n"
        f"전략: {strategy}\n"
        f"사유: {reason}\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_stop_loss(code, entry_price, stop_price, current_price,
                     holding_days, reason, mode="모의투자"):
    """손절 알림."""
    pnl_pct = (current_price - entry_price) / (entry_price or 1) * 100 if entry_price > 0 else 0
    now = datetime.now().strftime("%H:%M:%S")
    text = (
        f"🛑 <b>[손절 실행]</b>  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"종목: <b>{code}</b>\n"
        f"진입가: {entry_price:,.0f}원 → 현재가: {current_price:,.0f}원\n"
        f"수익률: {pnl_pct:+.2f}%\n"
        f"손절가: {stop_price:,.0f}원  보유: {holding_days}일\n"
        f"사유: {reason}\n"
        f"모드: {mode}"
    )
    return _send(text)


def notify_dashboard(image_path: str, dashboard_type: str = "일별"):
    """대시보드 이미지 전송."""
    today = datetime.now().strftime("%Y-%m-%d")
    caption = f"📊 QUANTUM FLOW {dashboard_type} 대시보드 ({today})"
    return send_image(image_path, caption)


if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW - 텔레그램 알림 테스트")
    print("=" * 55)
    if not BOT_TOKEN or not CHAT_ID:
        print("\n  텔레그램 설정이 없습니다.")
        print("  .env 파일에 아래 항목을 입력하세요:")
        print("  TELEGRAM_BOT_TOKEN=여기에_봇_토큰")
        print("  TELEGRAM_CHAT_ID=여기에_채팅_ID")
        print("\n  Phase 5 notifier_tools.py - 구현 완료!")
    else:
        send_alert("QUANTUM FLOW 알림 테스트")
        notify_buy("005930","삼성전자",10,72000,65,70560)
        notify_sell("005930","삼성전자",10,75000,72000,"트레일링 손절")
        notify_risk_off(["KOSPI -2.3%","VIX +22%"],"신규 매수 중단")
        notify_error("websocket_feeder","Connection refused")
        notify_daily_report(5,3,2,120000,1.2,[{"code":"035420","name":"NAVER","pnl_pct":8.5}])
        print("  모든 알림 전송 완료!")
    print("=" * 55)


# ── 거시분석 결과 알림 ──────────────────────────
def notify_macro_analysis(macro_result: dict, mode: str = "모의투자"):
    """거시경제 분석 완료 후 텔레그램 알림."""
    risk = macro_result.get("risk_status", "?")
    confidence = macro_result.get("confidence", 0)
    summary = macro_result.get("summary", "요약 없음")
    sectors = macro_result.get("sectors", [])
    strategy = macro_result.get("macro_strategy", "")
    position_pct = macro_result.get("macro_position_pct", 0)

    sector_str = ", ".join(sectors[:5]) if sectors else "없음"

    text = (
        f"<b>📊 거시경제 분석 완료</b> [{mode}]\n"
        f"{'─' * 28}\n"
        f"🔸 판정: <b>Risk-{risk}</b> (확신도 {confidence}%)\n"
        f"🔸 전략: {strategy} (포지션 {int(position_pct*100)}%)\n"
        f"🔸 추천섹터: {sector_str}\n"
        f"{'─' * 28}\n"
        f"📝 {summary[:200]}"
    )
    return _send(text)
