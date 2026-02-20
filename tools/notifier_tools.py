# tools/notifier_tools.py — 텔레그램 알림 전송 툴
# Phase 5 구현: 매수/매도/Risk-Off/오류/일일 리포트 알림

import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def _send(text, parse_mode="HTML"):
    """텔레그램 메시지 전송. 성공 True, 실패 False (예외 없음)."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  [텔레그램] 설정 없음 — 콘솔: {text[:60]}...")
        return False
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={"chat_id":CHAT_ID,"text":text,"parse_mode":parse_mode},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [텔레그램] 전송 실패: {e}")
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
    pnl_pct = (price-entry_price)/entry_price*100 if entry_price>0 else 0.0
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
    win_rate = (win_count/total_trades*100) if total_trades>0 else 0.0
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
