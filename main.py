# QUANTUM FLOW — 메인 실행 진입점
# 현재: Phase 1 환경 확인용
# 실행: python main.py

from dotenv import load_dotenv
import os

load_dotenv()


def check_env():
    required = [
        "KIS_APP_KEY", "KIS_APP_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OPENAI_API_KEY"
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"❌ 누락된 환경변수: {missing}")
        return False
    print("✅ 환경변수 확인 완료")
    return True


if __name__ == "__main__":
    check_env()
