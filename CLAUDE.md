# QUANTUM FLOW v2.1 — 프로젝트 가이드

## 프로젝트 개요
한국 주식 자동매매 봇. 4개 에이전트 파이프라인 + 하이브리드 LLM 아키텍처.
- **Agent 1** (macro_analyst): 거시경제 분석 → Claude Sonnet
- **Agent 2** (market_scanner): 종목 스캔 → Claude Sonnet
- **Agent 3** (head_strategist): 전략 결정 → LLM 없음 (룰 기반)
- **Agent 4** (market_watcher): 시장 감시 → GPT-4o-mini

## API 키 위치
**루트 `.env` 파일** (`quantum_flow/.env` 또는 상위 `.env`)에 모든 키 등록됨:
- `KIS_APP_KEY` / `KIS_APP_SECRET` — 한국투자증권 실전
- `KIS_PAPER_APP_KEY` / `KIS_PAPER_APP_SECRET` — 모의투자
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — 텔레그램 알림
- `ANTHROPIC_API_KEY` — Claude API (Agent 1,2 분석 + 백테스트 LLM)
- `OPENAI_API_KEY` — GPT-4o-mini (Agent 4)
- `DART_API_KEY` — DART 공시 OpenAPI (발급: opendart.fss.or.kr)
- `FRED_API_KEY` — FRED 매크로 데이터 (선택)

**GitHub Secrets에도 등록됨** (CI/CD용). 로컬에서는 `.env` 참조.

## 디렉토리 구조
```
quantum_flow/
├── main.py                  # 스케줄러 진입점 (APScheduler)
├── shared_state.py          # 에이전트 간 공유 상태 (threading.Lock)
├── agents/
│   ├── macro_analyst.py     # Agent 1: 거시경제 (async)
│   ├── market_scanner.py    # Agent 2: 종목 스캔 (async)
│   ├── head_strategist.py   # Agent 3: 전략 결정 (async)
│   └── market_watcher.py    # Agent 4: 시장 감시 (sync, 별도 스레드)
├── config/
│   └── settings.py          # 전역 상수 (매수/손절/시간 등)
├── tools/
│   ├── llm_client.py        # 하이브리드 LLM 래퍼 (싱글턴)
│   ├── cost_tracker.py      # LLM 비용 추적 (싱글턴)
│   ├── order_executor.py    # KIS API 주문 실행
│   ├── token_manager.py     # KIS API 토큰 갱신
│   ├── notifier_tools.py    # 텔레그램 알림
│   ├── scanner_tools.py     # 기술적 분석 (돈치안/RSI)
│   ├── stock_eval_tools.py  # 종목 종합 평가
│   ├── trade_logger.py      # 매매 기록
│   ├── dashboard_tools.py   # 대시보드 이미지 생성
│   ├── macro_data_tools.py  # FRED/yfinance 데이터 수집
│   ├── news_tools.py        # 뉴스/RSS 수집 + DART 공시
│   ├── intraday_tools.py    # 분봉 데이터
│   ├── market_calendar.py   # 개장일 판단
│   ├── websocket_feeder.py  # 실시간 체결 피드
│   ├── abnormal_detector.py # 이상 거래 감지 (거래량 급증, 가격 갭, VI)
│   ├── performance_reporter.py # 성과 리포트 (Sharpe, MDD, 승률)
│   ├── position_scaler.py   # 5단계 포지션 스케일링
│   ├── sector_rotation.py   # ETF 모멘텀 섹터 로테이션
│   └── utils.py             # safe_float 등 유틸
├── backtest/                # ★ 백테스트 시스템 (아래 상세)
├── data_collector/          # 데이터 수집 파이프라인
│   └── text/dart_collector.py  # DART 공시 수집
├── database/                # DB 관리
└── tasks/                   # CrewAI 태스크 정의
```

## 백테스트 시스템 (`backtest/`)

키움증권 수집 데이터 + 과거 뉴스/DART 공시로 Agent 1→2 파이프라인 성과를 측정.

### 파일 구조
```
backtest/
├── __init__.py
├── run_backtest.py      # CLI 진입점
├── engine.py            # 메인 루프 (랜덤 날짜→매크로분석→종목선정→수익률)
├── data_loader.py       # 키움 CSV + FRED/yfinance 데이터 로더
├── news_crawler.py      # Google News RSS 날짜별 크롤링 + JSON 캐시
├── dart_crawler.py      # DART 공시 날짜별 조회 + JSON 캐시
├── mock_provider.py     # Agent 입력 데이터 프로바이더 (MockMacro/Scanner/LLM)
├── report.py            # 성과 집계 + HTML 리포트
├── cache/               # 뉴스/DART/yfinance 캐시 (자동 생성)
└── results/             # 결과 JSON + HTML (자동 생성)
```

### 실행 방법
```bash
cd quantum_flow

# 기본 (규칙 기반, 실제 뉴스+DART, 10일 테스트)
python -m backtest.run_backtest --csv-dir ../collected_data --dates 10

# 실제 Claude API 사용 (뉴스+DART+LLM 분석)
python -m backtest.run_backtest --csv-dir ../collected_data --dates 50 --llm

# 빠른 테스트 (뉴스/DART 끄기)
python -m backtest.run_backtest --csv-dir ../collected_data --dates 5 --no-news --no-dart

# 옵션
#   --llm          : 실제 Claude API 사용 (ANTHROPIC_API_KEY 필요, 비용 발생)
#   --no-news      : 뉴스 크롤링 비활성화 (가상 뉴스)
#   --no-dart      : DART 공시 비활성화
#   --forward-days : 수익률 측정 기간 (기본 5일)
#   --seed         : 랜덤 시드 (기본 42)
```

### 데이터 소스
| 소스 | 키 필요 | 수집량 | 비고 |
|---|---|---|---|
| 키움 CSV (collected_data/daily/) | 없음 | ~4,200종목 × 600일 | 로컬 파일 |
| yfinance (VIX,DXY,KOSPI 등) | 없음 | 6개 시리즈 | 자동 캐싱 |
| FRED 매크로 | FRED_API_KEY | 16개 시리즈 | 선택 |
| Google News RSS | 없음 | 날짜당 120~300건 | 자동 캐싱 |
| DART 공시 | DART_API_KEY | 날짜당 200~600건 | 자동 캐싱 |

### 키움 CSV 데이터 (collected_data/)
- `collect_kiwoom_all.py`로 수집 (Windows + pykiwoom)
- `--resume` 플래그로 중단 후 재개 가능
- 컬럼: `종목코드,close,volume,거래대금,date,open,high,low,...,ticker`
- date: YYYYMMDD 형식, next=0만 사용 (종목당 ~600일)
- `merge_csv.py` — 개별 CSV를 하나로 합치기

### 백테스트 파이프라인 흐름
```
1. 키움 CSV 전체 로드 (4,200종목)
2. yfinance/FRED 매크로 데이터 다운로드 (캐싱)
3. 랜덤 테스트 날짜 N개 선택
4. 각 날짜마다:
   a. Google News RSS 크롤링 (경제/섹터 키워드 9개)
   b. DART 공시 조회 (3일 lookback, HIGH/MEDIUM 필터)
   c. Agent 1 매크로 분석 (LLM or 규칙 기반)
   d. 거래량 Top 50 종목 추출
   e. 기술적 필터 (Donchian 95% + RSI 50~70)
   f. Agent 2 종목 선정 (LLM or 점수 기반, 최대 10개)
   g. 5일 후 수익률 측정
5. 성과 집계 (평균수익, 벤치마크, 초과수익, 승률)
6. HTML 리포트 생성
```

## 하이브리드 LLM 아키텍처

### llm_client.py (tools/llm_client.py)
싱글턴 `get_llm_client()` → `LLMClient` 인스턴스 반환.
- `analyze(system, user)` → Anthropic Claude Sonnet (복잡한 분석)
- `analyze_json(system, user)` → analyze + JSON 파싱
- `classify(prompt)` → OpenAI GPT-4o-mini (단순 분류, temp=0.1)
- `classify_json(prompt)` → classify + JSON 파싱
- `_parse_json(text)` → ```json 블록 → ``` 블록 → {} 직접 매칭

### cost_tracker.py (tools/cost_tracker.py)
싱글턴 `get_cost_tracker()`. `record(model, input_tokens, output_tokens)` 호출마다 비용 계산.
`daily_summary()` → 모델별 집계. `reset()` → 일일 리셋 (main.py job_daily_report에서 호출).

## 에이전트 파이프라인

### 실행 순서 (main.py 스케줄러)
```
05:50  토큰 갱신
08:30  Agent 1: 거시경제 분석 (macro_analyst_run)
08:50  Agent 2: 종목 스캔 1차 (market_scanner_run)
09:02  오버나이트 손절 체크
09:05  Agent 3: 전략 결정 (head_strategist_run)
09:10  Agent 4: 시장 감시 시작 (MarketWatcher.run)
11:30  Agent 2: 종목 스캔 2차
15:20  장마감 판단 (오버나이트 종합 평가)
15:25  Agent 4: 감시 중지
15:35  일일 대시보드 + 리포트 + LLM 비용 요약
23:00  토큰 갱신 (이중 안전)
```

### shared_state 키 맵 (실제 코드 기준)

**Agent 1 (macro_analyst) writes:**
- `macro_risk` — 거시 리스크 등급
- `macro_sectors` — 선호 섹터 리스트
- `macro_avoid_sectors` — 회피 섹터
- `macro_urgent` — 긴급 뉴스 레벨
- `macro_confidence` — 신뢰도
- `macro_result` — 전체 분석 결과 dict
- `sector_multipliers` — 섹터별 비중 배수
- `risk_off` — Risk-Off 플래그 (긴급 시)
- `force_exit` — 긴급 청산 플래그

**Agent 2 (market_scanner) reads → writes:**
- reads: `macro_sectors`, `macro_avoid_sectors`, `sector_multipliers`, `preferred_sectors`
- writes: `watch_list`, `scanner_result`, `sector_scores_morning`, `sector_scores_midday`

**Agent 4 (market_watcher) reads → writes:**
- reads: `risk_off`, `recovery_state`, `reentry_count`, `risk_off_time`
- writes: `risk_off`, `risk_off_time`, `recovery_state`, `reentry_count`
- `update_risk_params()` 호출하여 risk_params 갱신

### LLM 호출 위치
| Agent | 함수 | LLM 메서드 | 용도 |
|-------|------|-----------|------|
| Agent 1 | `run_macro_analysis()` | `llm.analyze_json()` via `asyncio.to_thread()` | 거시경제 종합 분석 |
| Agent 2 | `select_with_llm()` | `llm.analyze_json()` | 종목 최종 선정 |
| Agent 4 | `check_llm_context()` | `llm.classify()` | Risk-Off 이중 검증 |
| Agent 4 | `_check_llm_recovery()` | `llm.classify()` | Recovery 안정화 판단 |

## 핵심 비즈니스 규칙

### 진입 조건 (3중 AND 필터)
- 돈치안 채널 돌파 + 거래량 200%↑ + 틱속도 5건/초
- 다중 타임프레임: 60분봉(대추세) → 15분봉(파동) → 5분봉(타점)
- 보조: VWAP 상방, ADX 25↑, RSI, 볼린저밴드, 체결강도 100%↑

### 포지션 관리
- 최대 보유 종목: 5개 (`MAX_POSITIONS`)
- 종목당 초기 투입: 20% (`POSITION_SIZE_RATIO`)
- 추가매수 비율: 초기의 30% (`PYRAMID_ADD_RATIO`)
- 최대 피라미딩: 2회 (`PYRAMID_MAX_COUNT`)
- 당일 최대 손실: -3% (`DAILY_LOSS_LIMIT`)

### 손절 체계
- 초기 손절: 진입가 - 2×ATR (Day 1)
- 트레일링: 최고가 - 3×ATR
- 타임 디케이: Day 1→2.0×ATR, Day 2→1.0×ATR, Day 3→+0.3×ATR
- 추가매수 후: 평단 -3%
- Time-Stop: 2일 미진입 시 기계적 매도

### 오버나이트
- +7% 이상 수익 시 보유, 익일 트레일링 스탑 -5%
- 스코어링 (60점 이상 시 홀딩): 수익률 25점 + 뉴스 25점 + 차트 추세 25점 + 거래량 25점

### Risk-Off 이중 검증
1. 정량 트리거 (4개 중 2개 이상): VIX +20%, KOSPI -2%, 달러/원 ±15원, 시총 상위 10종목 중 7개 하락
2. LLM 검증 (GPT-4o-mini): YES/NO 판단
3. 60초 유예 후 재확인

## 코딩 컨벤션

### 수정 시 반드시 지킬 것
1. **LLM 호출은 반드시 `llm_client` 경유** — 직접 `openai`/`anthropic` import 금지
2. **shared_state 키 이름 변경 금지** — 기존 코드의 키가 우선
3. **Agent 3, order_executor, websocket_feeder 수정 금지** — LLM 사용 안 함
4. **모든 LLM 호출에 try/except** — 실패 시 보수적 기본값 사용
5. **Agent 4는 정량 트리거 먼저, LLM은 보조** — 이중 검증 패턴 유지
6. **async 주의**: macro_analyst는 async (asyncio.to_thread로 동기 llm 호출)
7. **환경변수로 모델 교체 가능** — `ANALYSIS_MODEL`, `SENTINEL_MODEL`
8. **secrets는 절대 하드코딩 금지** — 항상 os.getenv() 또는 .env 파일
9. **dry_run 파라미터** — 모든 주문 함수에 포함 (True면 로그만 출력)
10. **USE_PAPER=true 기본값 유지** — 모의투자 우선

### shared_state 접근
```python
from shared_state import get_state, set_state, update_risk_params
from shared_state import get_positions, add_position, remove_position
```

### 텔레그램 알림
```python
from tools.notifier_tools import notify_buy, notify_sell, notify_risk_off, notify_error
from tools.notifier_tools import notify_daily_report, notify_trade_decision, notify_stop_loss
```

## 테스트 방법
```bash
# 임포트 체크
python -c "from tools.llm_client import get_llm_client; print('OK')"
python -c "from agents.macro_analyst import run_macro_analysis; print('OK')"
python -c "from agents.market_scanner import market_scanner_run; print('OK')"
python -c "from agents.market_watcher import MarketWatcher; print('OK')"

# 백테스트 (가장 빠른 테스트)
python -m backtest.run_backtest --csv-dir ../collected_data --dates 5 --no-news --no-dart

# 백테스트 (실제 데이터 + LLM)
python -m backtest.run_backtest --csv-dir ../collected_data --dates 50 --llm

# 단일 실행 (--once 모드)
python main.py --once
```

## 환경 설정
```bash
pip install -r requirements.txt
cp .env.template .env   # API 키 입력
```

필수: `KIS_APP_KEY`, `KIS_APP_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`
선택: `OPENAI_API_KEY`, `DART_API_KEY`, `FRED_API_KEY`
