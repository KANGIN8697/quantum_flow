# config/settings.py — 전역 상수 설정
# Phase 1에서 정의, 이후 Phase에서 참조

# 매수 신호 필터 기준값 (v2: 50,000회 탐색 확정, 2026-02-27)
DONCHIAN_PERIOD = 25          # 돈치안 채널 기간 (v1:20→v2:25, 집중도 92%)
VOLUME_SURGE_RATIO = 3.0      # 전일 동시간대 대비 거래량 배율 (v1:1.5→v2:3.0, 집중도 74%)
TICK_SPEED_MIN = 5            # 초당 최소 체결 건수
RSI_LOWER = 30                # RSI 하한 (v1:50→v2:30)
RSI_UPPER = 75                # RSI 상한 (v1:70→v2:75, 추세 지속 구간 포함)

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

# 손절 / 트레일링
ATR_PERIOD = 14
INITIAL_STOP_ATR = 2.0        # 초기 손절: -2.0 x ATR (v2 확정)
TRAILING_STOP_PCT = 0.02      # v2 트레일링 스탑: 고점 대비 -2% (v1:3%→v2:2%, 집중도 82%)
TRAILING_STOP_ATR = 4.0       # 트레일링 ATR 배수 (ATR 방식 병행 시)
TAKE_PROFIT_PCT = 0.07        # 익절 기준: +7% (v1:10%→v2:7%, 반전확률 상승 구간)
TIME_STOP_DAYS = 3            # 타임스탑: 3일 (v1:7→v2:3, 자본회전 속도 향상)
PYRAMID_STOP_PCT = -0.03      # 추가매수 후 손절: 평단 -3%
# 타임 디케이 ATR 배수 (일차별, time_stop=3일에 맞춤)
TIME_DECAY_ATR = {
    1: 2.0,    # Day 1: 진입가 - 2.0×ATR
    2: 1.0,    # Day 2: 진입가 - 1.0×ATR
    3: -0.3,   # Day 3: 진입가 + 0.3×ATR (본전+버퍼) → 이후 강제 청산
}

# 오버나이트 종합 평가
OVERNIGHT_THRESHOLD = 0.07    # (레거시) 수익률만으로 판단 시 최소 기준
OVERNIGHT_STOP_PCT = -0.05    # 익일 트레일링 스탑: 종가 대비 -5% (v2 확정)

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
MARKET_OPEN_HOLD = "09:20"    # 매수 신호 활성화 시각 (백테스트: 09:10~09:20 오프닝 러시 제외)
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
ADX_THRESHOLD = 25            # ADX 진입 최소값 (v2: 25 확정, 집중도 64%)
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

# 장중 개별종목 급변 감지 기준
STOCK_RAPID_CHANGE_PCT = 0.03     # 5분내 ±3% 변동 주의
STOCK_RAPID_ALERT_PCT  = 0.05     # 5분내 ±5% 변동 경고
VOLUME_SPIKE_CAUTION   = 3.0      # 거래량 3배 이상 주의
VOLUME_SPIKE_ALERT     = 5.0      # 거래량 5배 이상 경고

# 장중 감시 부가 기준 (Agent 4 확장용)
VIX_CAUTION_THRESHOLD    = 0.10   # VIX +10% 주의
KOSPI_CAUTION_THRESHOLD  = -0.01  # 코스피 -1% 주의
FX_CAUTION_THRESHOLD     = 10     # 달러/원 ±10원 주의
SP500_CAUTION_THRESHOLD  = -0.01  # S&P500 -1% 주의
SP500_ALERT_THRESHOLD    = -0.025 # S&P500 -2.5% 경고

# ========== 매크로 필터 상수 (백테스트 결과 기반) ==========
# [필터1] Neutral 레짐 → 신규 매수 차단 (신뢰도: 높음, 4개 기간 일관)
NEUTRAL_REGIME_BLOCK = True       # Neutral 레짐 시 매수 차단 활성화

# [필터2] 달러 강세 → 포지션 축소 (소프트 필터, N=65 소표본)
USD_STRENGTH_THRESHOLD = 0.5      # USD/KRW 일간 변화율(%) 기준
USD_STRENGTH_POSITION_MULT = 0.7  # 달러 강세 시 포지션 축소 비율 (30% 감소)

# [필터3] KOSPI 5일 모멘텀 → 확신도 가산 (v2 업데이트: p=0.0295 ★ 유의)
KOSPI_MOMENTUM_THRESHOLD = 2.0    # KOSPI 5일 변화율(%) 기준
KOSPI_MOMENTUM_POSITION_MULT = 1.1  # 모멘텀 확인 시 포지션 가산 (10%)

# [필터4] 매크로 부스트 — KOSPI5d>3% & 달러강세 (v2 신규, p=0.0295 ★)
# 50,000회 백테스트: 해당 국면 평균 수익률 4.26% vs 베이스 2.19% (+94%)
MACRO_BOOST_ENABLED = True
KOSPI_STRONG_MOMENTUM_PCT = 3.0   # KOSPI 5일 수익률 3%+ 기준
USD_STRONG_MA20_ABOVE = True      # 달러/원이 20일 MA 상회 여부
MACRO_BOOST_POSITION_MULT = 1.20  # 부스트 시 포지션 +20% 확대
MACRO_BOOST_MAX_POSITIONS = 6     # 부스트 시 동시 보유 6종목 허용

# ========== 추가 리스크 요소 (향후 통합 대상) ==========
# VIX 레벨별 동적 트레일링 스탑
VIX_NORMAL_MAX = 20               # VIX ≤ 20: 정상
VIX_CAUTION_MAX = 25              # VIX 20~25: 주의
VIX_HIGH_MAX = 30                 # VIX 25~30: 높음
# VIX > 30: 극단적 (Risk-OFF 고려)

# VIX 레벨에 따른 트레일링 스탑 조정 배수
VIX_TRAIL_ADJUSTMENT = {
    "NORMAL": 1.0,    # VIX ≤ 20: 기본 트레일링
    "CAUTION": 1.3,   # VIX 20~25: 트레일링 30% 완화 (조기청산 방지)
    "HIGH": 1.5,      # VIX 25~30: 트레일링 50% 완화
    "EXTREME": 0.0,   # VIX > 30: 신규 진입 금지 (기존 Risk-OFF와 연동)
}

# 외국인 수급 필터 (향후 데이터 수집 후 활성화)
FOREIGN_CUMUL_DAYS = 5            # 외국인 순매수/순매도 누적 일수
FOREIGN_SELL_THRESHOLD = -500     # 5일 순매도 억원 기준 (초과 시 주의)
FOREIGN_SELL_POSITION_MULT = 0.8  # 외국인 순매도 시 포지션 20% 축소

# ========== 백테스트 결과 기반 최적화 상수 (2026-02-25) ==========
# 분석: 299종목 × 663 파라미터 조합, 2024-02-01~2026-02-25
# 체결강도(CHG) 신호 최적 기준값
CHG_STRENGTH_THRESHOLD = 0.70     # 체결강도 진입 기준 (백테스트 최적: 0.70, 기존 0.55대비 수익+0.03%)
CHG_STRENGTH_MA5_MIN   = 0.55     # 체결강도 5분MA 최소값 (추세 확인용)

# [필터5] 이벤트 부재 신호 약화 (v2 신규, p=0.003 ★)
# 뉴스분석 결과: 기술신호+이벤트없음 = 1.06% vs 기술신호+이벤트 = 2.06% (유의)
# 당일 수익률 < 0%이면서 돌파 신호 발생 시 → 신호 신뢰도 감소
EVENT_FILTER_ENABLED = True
EVENT_MIN_DAY_RETURN = 0.00       # 당일 수익률 최소값 (0% = 양봉 필수)
EVENT_WEAK_POSITION_MULT = 0.60   # 이벤트 미동반 시 포지션 40% 축소

# 장중 익절(Take Profit) — DC 진입 후 당일 목표 수익률
INTRADAY_TP_PCT = 0.05            # 장중 익절 5% (백테스트 최적: 5% > 7% > 10%)
INTRADAY_TP_ENABLED = True        # 장중 익절 활성화

# 오프닝 구간 포지션 제한
OPENING_RUSH_END = "09:20"        # 오프닝 러시 종료 시각 (09:10~09:20 진입 자제)
OPENING_RUSH_POS_MULT = 0.0       # 오프닝 구간 진입 금지 (0.0 = 차단)

# 시간대별 포지션 가중치 (백테스트 기반)
# 10:00~10:30 구간이 승률 최고(34.8%), 오전장 집중
INTRADAY_TIME_WEIGHT = {
    "09:20": 0.5,   # 09:20~09:30: 관망 (직후 오프닝 러시)
    "09:30": 0.8,   # 09:30~10:00: 준비
    "10:00": 1.0,   # 10:00~10:30: 주요 매수 구간 (승률 34.8%)
    "10:30": 0.9,   # 10:30~11:00
    "11:00": 0.7,   # 11:00~11:30: 승률 저하
    "11:30": 0.6,   # 11:30~13:00: 점심 구간 (승률 최저)
    "13:00": 0.7,   # 13:00 이후
}

# ========== 2트랙 전략 파라미터 (2026-02-25) ==========
# 다중 타임프레임 MA 설정
TF15_MA_SHORT  = 3       # 15분봉 단기 MA
TF15_MA_MID    = 8       # 15분봉 중기 MA
TF15_MA_LONG   = 20      # 15분봉 장기 MA (전일 데이터 포함)
TF5_MA_SHORT   = 3       # 5분봉 단기 MA
TF5_MA_MID     = 8       # 5분봉 중기 MA

# Track 1 (장중 트레이딩) 청산 규칙
TRACK1_INITIAL_STOP_ATR = 1.5    # Track 1 초기 손절 (타이트)
TRACK1_TRAIL_ATR        = 4.0    # Track 1 트레일링 (여유)
TRACK1_TP_PCT           = 0.05   # Track 1 분할 익절 5% (50% 분할)
TRACK1_TP_SPLIT_RATIO   = 0.50   # 분할 청산 비율 (50%)
TRACK1_TIME_EXIT_BARS   = 30     # 시간 TP: 30봉 후 +1% 미만 → 청산
TRACK1_TIME_EXIT_MIN_PNL = 0.01  # 시간 TP 최소 수익률 (1%)
TRACK1_FORCE_CLOSE      = "15:10"  # Track 1 강제 청산 시각

# Track 2 (오버나이트 스윙) 전환 조건
TRACK2_QUALIFY_PNL      = 0.03   # 오버나이트 자격 수익률 (+3%↑)
TRACK2_EVAL_TIME        = "14:30"  # Track 2 전환 판정 시각
TRACK2_CHG_MIN          = 0.60   # 전환 시 체결강도 최소값
TRACK2_MAX_POSITIONS    = 2      # 최대 오버나이트 종목 수
TRACK2_DECISION_TIME    = "14:45"  # 최종 보유/청산 결정 시각

# Track 2 익일 청산 규칙
TRACK2_NEXT_TRAIL_PCT   = -0.05  # 익일 트레일링 -5%
TRACK2_GAP_DOWN_CUT     = -0.01  # 갭다운 -1% 즉시 청산
TRACK2_NEXT_DEADLINE    = "14:00"  # 익일 최종 청산 시각
TRACK2_MAX_HOLD_DAYS    = 1      # 최대 보유 일수

# 자금 배분
TRACK1_ALLOC            = 0.80   # Track 1 자금 배분 80%
TRACK2_RESERVE          = 0.20   # Track 2 예비 자금 20%
CASH_BUFFER_MIN         = 0.10   # 최소 현금 버퍼 10%
