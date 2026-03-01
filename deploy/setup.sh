#!/bin/bash
# QUANTUM FLOW — AWS 서버 초기 세팅 스크립트
# 사용법: bash deploy/setup.sh
#
# 사전 요구:
#   - Ubuntu 22.04+ (AWS EC2 t3.micro 이상)
#   - 기본 사용자: ubuntu
#   - 프로젝트가 /home/ubuntu/quantum_flow 에 클론되어 있어야 함

set -euo pipefail

PROJECT_DIR="/home/ubuntu/quantum_flow"
VENV_DIR="$PROJECT_DIR/.venv"

echo "================================================"
echo "  QUANTUM FLOW — 서버 초기 세팅"
echo "================================================"

# 1. 시스템 패키지
echo ""
echo "[1/6] 시스템 패키지 설치..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip \
    fonts-noto-cjk \
    git curl

# 2. 타임존 설정
echo ""
echo "[2/6] 타임존 설정 (Asia/Seoul)..."
sudo timedatectl set-timezone Asia/Seoul
echo "  현재 시각: $(date)"

# 3. Python 가상환경
echo ""
echo "[3/6] Python 가상환경 생성..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  가상환경 생성 완료: $VENV_DIR"
else
    echo "  가상환경 이미 존재: $VENV_DIR"
fi

# 4. 패키지 설치
echo ""
echo "[4/6] Python 패키지 설치..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
echo "  패키지 설치 완료"

# 5. 디렉토리 생성
echo ""
echo "[5/6] 출력 디렉토리 생성..."
mkdir -p "$PROJECT_DIR/outputs/reports"
mkdir -p "$PROJECT_DIR/outputs/dashboards"

# 6. systemd 서비스 등록
echo ""
echo "[6/6] systemd 서비스 등록..."
sudo cp "$PROJECT_DIR/deploy/quantum_flow.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quantum_flow
echo "  서비스 등록 완료"

echo ""
echo "================================================"
echo "  초기 세팅 완료!"
echo "================================================"
echo ""
echo "  다음 단계:"
echo "  1. .env 파일 생성:"
echo "     cp .env.template .env && nano .env"
echo ""
echo "  2. 서비스 시작:"
echo "     sudo systemctl start quantum_flow"
echo ""
echo "  3. 로그 확인:"
echo "     journalctl -u quantum_flow -f"
echo ""
echo "  4. 서비스 상태:"
echo "     sudo systemctl status quantum_flow"
echo ""
