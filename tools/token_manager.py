# tools/token_manager.py — KIS API 인증 및 토큰 관리자
# Phase 2 구현: 토큰 발급, 유효성 확인, 자동 갱신, 웹소켓 접속키 발급

import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── 환경변수 로드 ──────────────────────────────────────────────
USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"

if USE_PAPER:
    BASE_URL = "https://openapivts.koreainvestment.com:29443"
    APP_KEY = os.getenv("KIS_PAPER_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_PAPER_APP_SECRET", "")
    MODE_LABEL = "모의투자"
else:
    BASE_URL = "https://openapi.koreainvestment.com:9443"
    APP_KEY = os.getenv("KIS_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_APP_SECRET", "")
    MODE_LABEL = "실전투자"

TOKEN_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "token_cache.json"
)


# ── 1. 토큰 발급 ───────────────────────────────────────────────
def get_access_token() -> str:
    """
    KIS API에서 OAuth2 액세스 토큰을 발급받고 캐시에 저장한다.
    USE_PAPER 환경변수에 따라 모의투자/실전 엔드포인트 자동 전환.
    """
    if not APP_KEY or not APP_SECRET:
        raise EnvironmentError(
            f"❌ [{MODE_LABEL}] API 키가 설정되지 않았습니다. "
            f".env 파일에 {'KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET' if USE_PAPER else 'KIS_APP_KEY / KIS_APP_SECRET'} 를 입력하세요."
        )

    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    response = requests.post(url, headers=headers, json=body, timeout=10)
    response.raise_for_status()
    data = response.json()

    token = data.get("access_token")
    expires_in = int(data.get("expires_in", 86400))
    expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

    if not token:
        raise ValueError(f"토큰 발급 실패: {data}")

    os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
    cache = {
        "access_token": token,
        "expires_at": expires_at,
        "mode": MODE_LABEL,
        "issued_at": datetime.now().isoformat(),
    }
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"✅ [{MODE_LABEL}] 토큰 발급 완료 | 만료: {expires_at}")
    return token


# ── 2. 토큰 유효성 확인 ────────────────────────────────────────
def is_token_valid() -> bool:
    """
    캐시된 토큰이 유효한지 확인한다.
    만료 30분 전이면 False를 반환해 조기 갱신을 유도한다.
    """
    if not os.path.exists(TOKEN_CACHE_PATH):
        return False
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        expires_at = datetime.fromisoformat(cache.get("expires_at", ""))
        if cache.get("mode") != MODE_LABEL:
            print(f"⚠️  모드 변경 감지 ({cache.get('mode')} → {MODE_LABEL}), 재발급 필요")
            return False
        if datetime.now() >= expires_at - timedelta(minutes=30):
            print(f"⚠️  [{MODE_LABEL}] 토큰 만료 임박, 갱신 필요")
            return False
        return True
    except (json.JSONDecodeError, ValueError, KeyError):
        return False


# ── 3. 토큰 자동 갱신 ─────────────────────────────────────────
def ensure_token() -> str:
    """
    모든 API 호출 전에 이 함수를 사용한다.
    캐시가 유효하면 재사용, 만료 임박이면 자동 재발급.
    """
    if is_token_valid():
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"✅ [{MODE_LABEL}] 캐시된 토큰 재사용")
        return cache["access_token"]
    print(f"🔄 [{MODE_LABEL}] 토큰 재발급 중...")
    return get_access_token()


# ── 4. 웹소켓 접속키 발급 ─────────────────────────────────────
def get_websocket_approval_key() -> str:
    """
    실시간 WebSocket 연결에 필요한 접속키를 발급받는다.
    엔드포인트: /oauth2/Approval
    """
    if not APP_KEY or not APP_SECRET:
        raise EnvironmentError(f"❌ [{MODE_LABEL}] API 키가 설정되지 않았습니다.")

    url = f"{BASE_URL}/oauth2/Approval"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": APP_SECRET,
    }

    response = requests.post(url, headers=headers, json=body, timeout=10)
    response.raise_for_status()
    data = response.json()

    approval_key = data.get("approval_key")
    if not approval_key:
        raise ValueError(f"웹소켓 접속키 발급 실패: {data}")

    print(f"✅ [{MODE_LABEL}] 웹소켓 접속키 발급 완료")
    return approval_key


# ── 테스트 블록 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(f"  QUANTUM FLOW — KIS 토큰 관리자 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 50)

    if not APP_KEY or not APP_SECRET:
        print()
        print("⚠️  API 키가 설정되지 않았습니다.")
        print(f"   .env 파일에 아래 항목을 입력하세요:\n")
        if USE_PAPER:
            print("   KIS_PAPER_APP_KEY=여기에_모의투자_앱키")
            print("   KIS_PAPER_APP_SECRET=여기에_모의투자_앱시크릿")
        else:
            print("   KIS_APP_KEY=여기에_실전_앱키")
            print("   KIS_APP_SECRET=여기에_실전_앱시크릿")
        print()
        print("📁 token_manager.py 구조 확인 완료 — API 키 입력 후 재실행하세요.")
        exit(0)

    try:
        print("\n[1] 토큰 발급 시도...")
        token = ensure_token()
        print(f"    토큰 앞 10자리: {token[:10]}...")

        print("\n[2] 캐시 재사용 확인...")
        token2 = ensure_token()
        assert token == token2
        print(f"    캐시 재사용 성공!")

        print("\n[3] token_cache.json 파일 확인...")
        with open(TOKEN_CACHE_PATH) as f:
            cache = json.load(f)
        print(f"    만료 시각: {cache['expires_at']}")
        print(f"    모드: {cache['mode']}")

        print("\n[4] 웹소켓 접속키 발급...")
        ws_key = get_websocket_approval_key()
        print(f"    접속키 앞 10자리: {ws_key[:10]}...")

        print("\n" + "=" * 50)
        print("  ✅ 모든 테스트 통과!")
        print("=" * 50)

    except EnvironmentError as e:
        print(f"\n{e}")
    except requests.exceptions.RequestException as e:
        print(f"\n❌ 네트워크 오류: {e}")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
