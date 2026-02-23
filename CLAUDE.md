# QUANTUM FLOW v2.1 — 프로젝트 가이드

## 프로젝트 개요
한국 주식 자동매매 봇. 4개 에이전트 파이프라인 + 하이브리드 LLM 아키텍처.
- **Agent 1** (macro_analyst): 거시경제 분석 → Claude Sonnet
- **Agent 2** (market_scanner): 종목 스캔 → Claude Sonnet
- **Agent 3** (head_strategist): 전략 결정 → LLM 없음 (룰 기반)
- **Agent 4** (market_watcher): 시장 감시 → GPT-4o-mini

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
│   ├── news_tools.py        # 뉴스/RSS 수집
│   ├── intraday_tools.py    # 분봉 데이터
│   ├── market_calendar.py   # 개장일 판단
│   ├── websocket_feeder.py  # 실시간 체결 피드
│   └── utils.py             # safe_float 등 유틸
├── data_collector/          # 데이터 수집 파이프라인
├── database/                # DB 관리
└── tasks/                   # CrewAI 태스크 정의
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

### 환경변수
```
ANTHROPIC_API_KEY=     # Claude Sonnet용
OPENAI_API_KEY=        # GPT-4o-mini용
ANALYSIS_MODEL=claude-sonnet-4-5-20250929   # Agent 1,2
SENTINEL_MODEL=gpt-4o-mini                  # Agent 4
```

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

### LLM 호출 위치 (수정된 코드)
| Agent | 함수 | LLM 메서드 | 용도 |
|-------|------|-----------|------|
| Agent 1 | `run_macro_analysis()` | `llm.analyze_json()` via `asyncio.to_thread()` | 거시경제 종합 분석 |
| Agent 2 | `select_with_llm()` | `llm.analyze_json()` | 종목 최종 선정 |
| Agent 4 | `check_llm_context()` | `llm.classify()` | Risk-Off 이중 검증 |
| Agent 4 | `_check_llm_recovery()` | `llm.classify()` | Recovery 안정화 판단 |

## 핵심 비즈니스 규칙

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

### Risk-Off 이중 검증
1. 정량 트리거 (4개 중 2개 이상):
   - VIX 전일 대비 +20%
   - KOSPI 당일 -2%
   - 달러/원 ±15원
   - 시총 상위 10종목 중 7개 하락
2. LLM 검증 (GPT-4o-mini): YES/NO 판단
3. 60초 유예 후 재확인

### 오버나이트 스코어링 (60점 이상 시 홀딩)
- 수익률 25점 + 뉴스 25점 + 차트 추세 25점 + 거래량 25점

## 코딩 컨벤션

### 수정 시 반드시 지킬 것
1. **LLM 호출은 반드시 `llm_client` 경유** — 직접 `openai`/`anthropic` import 금지
2. **shared_state 키 이름 변경 금지** — 기존 코드의 키가 우선
3. **Agent 3, order_executor, websocket_feeder 수정 금지** — LLM 사용 안 함
4. **모든 LLM 호출에 try/except** — 실패 시 보수적 기본값 사용
5. **Agent 4는 정량 트리거 먼저, LLM은 보조** — 이중 검증 패턴 유지
6. **async 주의**: macro_analyst는 async (asyncio.to_thread로 동기 llm 호출)
7. **환경변수로 모델 교체 가능** — `ANALYSIS_MODEL`, `SENTINEL_MODEL`

### shared_state 접근
```python
from shared_state import get_state, set_state, update_risk_params
from shared_state import get_positions, add_position, remove_position
```
- `set_state(key, value)` — NOT `write_state`
- `update_risk_params(dict)` — risk_params 부분 업데이트

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

# 단일 실행 (--once 모드)
python main.py --once
```

## 환경 설정
```bash
pip install -r requirements.txt
cp .env.template .env   # API 키 입력
```

필수: `KIS_APP_KEY`, `KIS_APP_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
선택: `FRED_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
