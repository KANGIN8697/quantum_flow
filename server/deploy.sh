#!/bin/bash
# =============================================================================
# QUANTUM FLOW v2.1 — 서버 원클릭 배포 스크립트
# 실행: bash deploy.sh
# 대상: AWS Lightsail Ubuntu 24.04 (신규 서버)
# =============================================================================

set -e  # 에러 발생 시 즉시 중단

PROJECT_DIR="/home/ubuntu/quantum-flow"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="quantum-flow"

echo "============================================"
echo "  QUANTUM FLOW v2.1 — 서버 배포 시작"
echo "============================================"

# ── 1. 시스템 패키지 업데이트 ─────────────────────────────────────────────────
echo "[1/9] 시스템 패키지 업데이트..."
sudo apt update -qq && sudo apt upgrade -y -qq
sudo apt install -y -qq \
    build-essential git curl wget htop tmux \
    make libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev llvm \
    libncurses5-dev libncursesw5-dev xz-utils \
    tk-dev libffi-dev liblzma-dev \
    unattended-upgrades fail2ban

# ── 2. 스왑 설정 (2GB) ────────────────────────────────────────────────────────
echo "[2/9] 스왑 메모리 설정 (2GB)..."
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
    echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf > /dev/null
    sudo sysctl -p > /dev/null
    echo "  스왑 설정 완료"
else
    echo "  스왑 이미 설정됨 — 건너뜀"
fi

# ── 3. 타임존 설정 ────────────────────────────────────────────────────────────
echo "[3/9] 타임존 → Asia/Seoul 설정..."
sudo timedatectl set-timezone Asia/Seoul
echo "  현재 시각: $(date)"

# ── 4. pyenv + Python 3.11 설치 ───────────────────────────────────────────────
echo "[4/9] Python 3.11 설치 (pyenv)..."
if [ ! -d "$HOME/.pyenv" ]; then
    curl -fsSL https://pyenv.run | bash
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init -)"

    # 쉘 설정에 영구 추가
    {
        echo 'export PYENV_ROOT="$HOME/.pyenv"'
        echo 'export PATH="$PYENV_ROOT/bin:$PATH"'
        echo 'eval "$(pyenv init -)"'
    } >> ~/.bashrc

    pyenv install 3.11 --skip-existing
    pyenv global 3.11
    echo "  Python $(python --version) 설치 완료"
else
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init -)"
    echo "  pyenv 이미 설치됨 — 건너뜀"
fi

# ── 5. 프로젝트 디렉토리 & 가상환경 ──────────────────────────────────────────
echo "[5/9] 프로젝트 환경 설정..."
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/outputs/reports"

if [ ! -d "$VENV_DIR" ]; then
    python -m venv "$VENV_DIR"
    echo "  가상환경 생성 완료"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

# ── 6. 의존성 설치 ────────────────────────────────────────────────────────────
echo "[6/9] Python 패키지 설치..."
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    pip install -r "$PROJECT_DIR/requirements.txt" -q
    echo "  requirements.txt 설치 완료"
else
    echo "  ⚠ requirements.txt 없음 — 수동으로 설치하세요"
fi

# ── 7. server/ 파일 배포 ──────────────────────────────────────────────────────
echo "[7/9] 서버 설정 파일 배포..."

# healthcheck.sh 배포
if [ -f "$PROJECT_DIR/server/healthcheck.sh" ]; then
    chmod +x "$PROJECT_DIR/server/healthcheck.sh"
    echo "  healthcheck.sh 권한 설정 완료"
fi

# systemd 서비스 등록
if [ -f "$PROJECT_DIR/server/quantum-flow.service" ]; then
    sudo cp "$PROJECT_DIR/server/quantum-flow.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    echo "  systemd 서비스 등록 완료"
else
    echo "  ⚠ quantum-flow.service 없음 — server/ 폴더 확인하세요"
fi

# ── 8. 크론잡 등록 ────────────────────────────────────────────────────────────
echo "[8/9] 헬스체크 크론잡 등록 (5분마다)..."
CRON_JOB="*/5 * * * * /home/ubuntu/quantum-flow/server/healthcheck.sh >> /home/ubuntu/quantum-flow/outputs/reports/healthcheck.log 2>&1"
(crontab -l 2>/dev/null | grep -v "healthcheck.sh"; echo "$CRON_JOB") | crontab -
echo "  크론잡 등록 완료"

# ── 9. 보안 강화 ──────────────────────────────────────────────────────────────
echo "[9/9] 보안 설정..."

# SSH 비밀번호 로그인 비활성화
sudo sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart sshd

# fail2ban 활성화
sudo systemctl enable fail2ban > /dev/null 2>&1
sudo systemctl start fail2ban

# 자동 보안 업데이트
sudo dpkg-reconfigure -f noninteractive unattended-upgrades > /dev/null 2>&1
echo "  보안 설정 완료"

# ── 완료 체크 ─────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  ✅ 배포 완료!"
echo "============================================"
echo ""
echo "다음 단계:"
echo "  1. .env 파일 설정:"
echo "     cp $PROJECT_DIR/server/.env.template $PROJECT_DIR/.env"
echo "     nano $PROJECT_DIR/.env"
echo "     chmod 600 $PROJECT_DIR/.env"
echo ""
echo "  2. 코드 업로드 확인 후 서비스 시작:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  3. 로그 확인:"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo ""

# .env 파일 존재 여부 확인
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "⚠️  경고: .env 파일이 없습니다. 서비스 시작 전에 반드시 설정하세요!"
fi
