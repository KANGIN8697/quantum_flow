#!/bin/bash
# =============================================================================
# QUANTUM FLOW v2.1 — 코드 업로드 후 원클릭 세팅
# 실행: bash ~/quantum-flow/server/setup.sh
# =============================================================================
set -e

PROJECT_DIR="/home/ubuntu/quantum-flow"
VENV_DIR="$PROJECT_DIR/.venv"

echo "======================================"
echo "  QUANTUM FLOW 서버 세팅 시작"
echo "======================================"

# ── 1. pyenv PATH 활성화 ──────────────────────────────────────────
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
pyenv global 3.11

echo "[1/5] Python 버전: $(python --version)"

# ── 2. 가상환경 생성 & 패키지 설치 ───────────────────────────────
echo "[2/5] 가상환경 생성..."
cd "$PROJECT_DIR"
python -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  패키지 설치 완료"

# ── 3. .env 파일 생성 ─────────────────────────────────────────────
echo "[3/5] .env 파일 설정..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/server/.env.template" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    echo ""
    echo "  ⚠ .env 파일이 생성됐어. API 키를 지금 입력해."
    echo "  (저장: Ctrl+X → Y → Enter)"
    echo ""
    sleep 2
    nano "$PROJECT_DIR/.env"
else
    echo "  .env 이미 존재 — 건너뜀"
fi

# ── 4. outputs 디렉토리 생성 ──────────────────────────────────────
echo "[4/5] 로그 디렉토리 생성..."
mkdir -p "$PROJECT_DIR/outputs/reports"
chmod +x "$PROJECT_DIR/server/healthcheck.sh"

# ── 5. systemd 서비스 등록 & 시작 ────────────────────────────────
echo "[5/5] 서비스 등록 & 시작..."
sudo cp "$PROJECT_DIR/server/quantum-flow.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quantum-flow
sudo systemctl start quantum-flow

# 크론잡 등록 (헬스체크 5분마다)
CRON_JOB="*/5 * * * * /home/ubuntu/quantum-flow/server/healthcheck.sh >> /home/ubuntu/quantum-flow/outputs/reports/healthcheck.log 2>&1"
(crontab -l 2>/dev/null | grep -v "healthcheck.sh"; echo "$CRON_JOB") | crontab -

echo ""
echo "======================================"
echo "  ✅ 세팅 완료!"
echo "======================================"
echo ""
echo "로그 확인:"
echo "  sudo journalctl -u quantum-flow -f"
echo ""

# 서비스 상태 출력
sudo systemctl status quantum-flow --no-pager
