#!/bin/bash
# =============================================================================
# QUANTUM FLOW v2.1 — 서버 헬스체크 스크립트
# 크론잡: */5 * * * * /home/ubuntu/quantum-flow/server/healthcheck.sh
# =============================================================================

SERVICE="quantum-flow"
ENV_FILE="/home/ubuntu/quantum-flow/.env"

# .env 직접 파싱 (크론잡에서 source가 동작하지 않는 문제 우회)
if [ -f "$ENV_FILE" ]; then
    TELEGRAM_BOT_TOKEN=$(grep -oP '(?<=TELEGRAM_BOT_TOKEN=)\S+' "$ENV_FILE" | tr -d '"' | tr -d "'")
    TELEGRAM_CHAT_ID=$(grep -oP '(?<=TELEGRAM_CHAT_ID=)\S+' "$ENV_FILE" | tr -d '"' | tr -d "'")
fi

# 텔레그램 알림 함수
send_alert() {
    local emoji="$1"
    local message="$2"
    local full_msg="${emoji} [QUANTUM FLOW] ${message}"
    local hostname=$(hostname -s)
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S KST')

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${full_msg}%0A서버: ${hostname}%0A시각: ${timestamp}" \
        > /dev/null 2>&1
}

# ── 1. 서비스 상태 체크 ──────────────────────────────────────────────────────
if ! systemctl is-active --quiet "$SERVICE"; then
    send_alert "🚨" "서비스 중지 감지. 자동 재시작 시도 중..."
    sudo systemctl restart "$SERVICE"
    sleep 15

    if systemctl is-active --quiet "$SERVICE"; then
        send_alert "✅" "서비스 정상 복구됨"
    else
        send_alert "❌" "서비스 복구 실패! 수동 확인 필요"
        # 로그 마지막 20줄도 함께 전송
        LOG_TAIL=$(journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null | tail -20)
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=📋 최근 로그:%0A$(echo "$LOG_TAIL" | sed 's/&/%26/g')" \
            > /dev/null 2>&1
    fi
fi

# ── 2. 메모리 사용률 체크 (90% 초과 시 알림) ────────────────────────────────
MEM_USAGE=$(free | awk '/Mem:/ {printf("%.0f"), $3/$2 * 100}')
if [ "$MEM_USAGE" -gt 90 ]; then
    send_alert "⚠️" "메모리 사용률 ${MEM_USAGE}% — OOM 위험"
fi

# ── 3. 디스크 사용률 체크 (85% 초과 시 알림) ────────────────────────────────
DISK_USAGE=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_USAGE" -gt 85 ]; then
    send_alert "⚠️" "디스크 사용률 ${DISK_USAGE}% — 로그 정리 필요"

    # 30일 이상 된 로그 자동 삭제
    find /home/ubuntu/quantum-flow/outputs/reports/ -name "*.log" -mtime +30 -delete 2>/dev/null
fi

# ── 4. 장 시간 중 프로세스 생존 확인 (09:00~15:35 KST) ─────────────────────
HOUR=$(date +%H)
MIN=$(date +%M)
TIME_INT=$((HOUR * 100 + MIN))

if [ "$TIME_INT" -ge 900 ] && [ "$TIME_INT" -le 1535 ]; then
    # 메인 프로세스가 살아있는지 PID 확인
    PID=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
    if [ -z "$PID" ] || [ "$PID" -eq 0 ]; then
        send_alert "🔴" "장 중 메인 프로세스 PID 없음 — 즉시 확인 필요"
    fi
fi

exit 0
