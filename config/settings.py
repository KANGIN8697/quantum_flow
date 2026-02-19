# config/settings.py — 전역 상수 설정
# Phase 1에서 정의, 이후 Phase에서 참조

# 매수 신호 필터 기준값
DONCHIAN_PERIOD = 20          # 돈치안 채널 기간
VOLUME_SURGE_RATIO = 2.0      # 전일 동시간대 대비 거래량 배율
TICK_SPEED_MIN = 5            # 초당 최소 체결 건수
RSI_LOWER = 50                # RSI 하한
RSI_UPPER = 70                # RSI 상한

# 포지션 관리
MAX_POSITIONS = 5             # 최대 보유 종목 수
POSITION_SIZE_RATIO = 0.20    # 종목당 초기 투입 비율
PYRAMID_ADD_RATIO = 0.30      # 추가매수 비율 (초기 대비)
DAILY_LOSS_LIMIT = -0.03      # 당일 최대 손실 한도

# 추가매수 조건
PYRAMID_PRICE_TRIGGER = 0.05  # 평단 대비 +5% 상승 시
PYRAMID_VOLUME_RATIO = 0.50   # 초기 진입 거래량의 50%
PYRAMID_TICK_RATIO = 0.40     # 초기 진입 틱 속도의 40%
PYRAMID_VOLUME_TREND_MIN = 5  # 거래량 추세 확인 분 단위

# 손절 / 트레일링
ATR_PERIOD = 14
INITIAL_STOP_ATR = 2.0        # 초기 손절: -2 x ATR
TRAILING_STOP_ATR = 3.0       # 트레일링: -3 x ATR
PYRAMID_STOP_PCT = -0.03      # 추가매수 후 손절: 평단 -3%

# 오버나이트
OVERNIGHT_THRESHOLD = 0.07    # +7% 이상 시 오버나이트 트랙 전환
OVERNIGHT_STOP_PCT = -0.05    # 익일 손절: -5%

# 시간 설정 (KST)
MARKET_OPEN_HOLD = "09:10"    # 매수 신호 활성화 시각
FORCE_CLOSE_TIME = "15:20"    # 강제 청산 시각
NO_PYRAMID_AFTER = "15:00"    # 추가매수 금지 시각
SCANNER_RUN_1 = "08:30"       # Agent 2 1차 실행
SCANNER_RUN_2 = "11:30"       # Agent 2 2차 실행

# Agent 4 Risk-Off 트리거 기준값
VIX_SURGE_THRESHOLD = 0.20    # VIX 전일 대비 +20%
KOSPI_DROP_THRESHOLD = -0.02  # 코스피 당일 -2%
FX_CHANGE_THRESHOLD = 15      # 달러/원 ±15원
MARKET_DROP_COUNT = 7         # 시총 상위 10종목 중 하락 종목 수
RISK_OFF_TRIGGER_MIN = 2      # 4개 트리거 중 최소 충족 수
RISK_OFF_CONFIRM_WAIT = 60    # Risk-Off 선언 전 유예 시간 (초)

# 감시 풀
MAX_WATCH_STOCKS = 30         # 최대 감시 종목 수
NEWS_CHECK_INTERVAL = 20      # Agent 4 뉴스 수집 주기 (분)
