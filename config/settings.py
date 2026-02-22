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

# 추가매수 조건 (기능4: 동적 피라미딩)
PYRAMID_PRICE_TRIGGER = 0.05  # 고정 % 폴백 (ATR 불가 시)
PYRAMID_ATR_MULTIPLIER = 1.5  # 동적 피라미딩: 진입가 + (1.5 × ATR) 이상일 때
PYRAMID_MIN_TRIGGER_PCT = 0.02  # ATR 기반 최소 트리거 (2%) — 횡보주 보호
PYRAMID_VOLUME_RATIO = 0.50   # 초기 진입 거래량의 50%
PYRAMID_TICK_RATIO = 0.40     # 초기 진입 틱 속도의 40%
PYRAMID_VOLUME_TREND_MIN = 5  # 거래량 추세 확인 분 단위
PYRAMID_MAX_COUNT = 2         # 최대 피라미딩 횟수

# 손�H / 트레일링
ATR_PERIOD = 14
INITIAL_STOP_ATR = 2.0        # 초기 손절: -2 x ATR (Day 1)
TRAILING_STOP_ATR = 3.0       # 트레일링: -3 x ATR
PYRAMID_STOP_PCT = -0.03      # 추가매수 후 손절: 평단 -3%
# 타임 디케이 ATR 배수 (일차별)
TIME_DECAY_ATR = {
    1: 2.0,    # Day 1: 진입가 - 2.0×ATR
    2: 1.0,    # Day 2: 진입가 - 1.0×ATR
    3: -0.3,   # Day 3: 진입가 + 0.3×ATR (본전+버퍼)
}

# 오버나이트 종합 평가
OVERNIGHT_THRESHOLD = 0.07    # (레거시) 수익률만으로 판단 시 최소 기준
OVERNIGHT_STOP_PCT = -0.03    # 익일 손절: 종가 대비 -3%

# 오버나이트 스코어링 (총 100점, 60점 이상 시 홀딩)
OVERNIGHT_MIN_SCORE = 60      # 오버나이트 최소 합격 점수
OVERNIGHT_PROFIT_WEIGHT = 25  # 수익률 배점 (max 25)
OVERNIGHT_NEWS_WEIGHT = 25    # 뉴스/공시 배점 (max 25)
OVERNIGHT_TREND_WEIGHT = 25   # 차트 추세 배점 (max 25)
OVERNIGHT_VOLUME_WEIGHT = 25  # 거래량 건전성 배점 (max 25)

# 뉴스 센티먼트 키워드
OVERNIGHT_NEWS_POSITIVE = [
    "실적 호조", "어닝 서프라이즈", "상향", "증액", "신고가",
    "목표주가 상향", "매수 의견", "수주", "흑자전환", "계약 체결",
    "신규 사업", "성장", "호실적", "최대 실적", "영업이익 증가",
]
OVERNIGHT_NEWS_NEGATIVE = [
    "하향", "감액", "손실", "적자", "리콜", "소송", "제재",
    "목표주가 하향", "매도 의견", "하락", "유상증자", "횡령",
    "감사의견 거절", "상장폐지", "공매도", "대량 매도",
]

# 시간 설정 (KST)
MARKET_OPEN_HOLD = "09:10"    # 매수 싨호 활성화 시각
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

# 기술적 지표 (ADX, VWAP)
ADX_PERIOD = 14               # ADX 계산 기간
ADX_THRESHOLD = 25            # ADX 진입 최소값 (추세 확인)
VWAP_LOOKBACK = 20            # VWAP 계산 기간 (일봉 기준)
DONCHIAN_PROXIMITY_PCT = 0.97 # 돈치안 상단 근접 판정 비율 (97%)

# 분봉 추세 판단
INTRADAY_15M_MA_THRESHOLD = 0.002  # MA3/MA8 추세 판단 최소 이탈률 (0.2%)

# 수급 분석
INVESTOR_CUMUL_DAYS = 3       # 수급 누적 일수 (기존 5 → 3)
INVESTOR_WEIGHT_DAY1 = 1.5    # 최근일 가중치
INVESTOR_WEIGHT_DAY2 = 1.0
INVESTOR_WEIGHT_DAY3 = 0.5

# 상대강도 진입 게이트
RS_ENTRY_THRESHOLD = 2        # RS 점수 최소 진입 기준

# 분봉 데이터 (KIS API)
INTRADAY_15M_BARS = 8         # 15분봉 조회 개수 (2시간)
INTRADAY_60M_BARS = 5         # 60분봉 조회 개수

# 감시 풀
MAX_WATCH_STOCKS = 30         # 최대 감시 종목 수
NEWS_CHECK_INTERVAL = 20      # Agent 4 뉴스 수집 주기 (분)


# ========== Auto-added constants ==========
RECOVERY_MAX_REENTRY = 3  # max reentry count in recovery phase
RECOVERY_MIN_WAIT = 30  # min wait minutes between recovery reentries
RECOVERY_POSITION_RATIO = 0.5  # recovery phase position ratio
SECTOR_DELTA_BONUS_MAX = 0.2  # sector delta bonus max
SECTOR_DELTA_BONUS_MIN = 0.05  # sector delta bonus min
SECTOR_MIDDAY_TIME = "12:00"  # midday sector time
SECTOR_MORNING_TIME = "09:30"  # morning sector time
SECTOR_MULTIPLIER_DEFAULT = 1.0  # sector default multiplier
SECTOR_MULTIPLIER_MAX = 1.5  # sector max multiplier
SECTOR_MULTIPLIER_MIN = 0.5  # sector min multiplier
TWAP_INTERVAL_SEC = 30  # TWAP interval sec
TWAP_MAX_SPLITS = 5  # TWAP max splits
TWAP_TICK_SPEED_MIN = 3  # TWAP min tick speed
TWAP_VOLUME_THRESHOLD = 500000  # TWAP volume threshold
