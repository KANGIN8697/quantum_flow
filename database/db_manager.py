# database/db_manager.py — SQLite DB 스키마 & 접근 계층
# QUANTUM FLOW 데이터 파이프라인의 중앙 저장소
#
# 테이블 구조:
#   ohlcv_5m / ohlcv_15m / ohlcv_60m  — 분봉 (Creon/키움)
#   daily_ohlcv                        — 국내 일봉 (KIS API)
#   global_daily                       — 해외 지수/지표 일봉 (yfinance)
#   kr_macro                           — 한국 거시경제 (ECOS)
#   us_macro                           — 미국 거시경제 (FRED)
#   dart_disclosures                   — DART 공시
#   news_headlines                     — 뉴스 헤드라인 (RSS)
#   fomc_statements / bok_statements   — 중앙은행 성명서
#   market_regime                      — 시장 국면 분류
#
# 10GB 초과 시 PostgreSQL 마이그레이션 고려 (현재 SQLite)

import os
import sqlite3
import json
import logging
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("db_manager")

# ── 경로 설정 ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "market_data.db")


# ── 커넥션 관리 ───────────────────────────────────────────

@contextmanager
def get_conn(db_path: str = None):
    """SQLite 커넥션 컨텍스트 매니저. WAL 모드 + 외래키 활성화."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 테이블 생성 (DDL) ─────────────────────────────────────

_SCHEMA_SQL = """
-- ═══════════════════════════════════════════════════════════
--  1. 분봉 데이터 (Creon/키움)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ohlcv_5m (
    datetime    TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    regime      TEXT    DEFAULT 'normal',
    PRIMARY KEY (datetime, ticker)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_5m_ticker ON ohlcv_5m(ticker, datetime);

CREATE TABLE IF NOT EXISTS ohlcv_15m (
    datetime    TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    regime      TEXT    DEFAULT 'normal',
    PRIMARY KEY (datetime, ticker)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_15m_ticker ON ohlcv_15m(ticker, datetime);

CREATE TABLE IF NOT EXISTS ohlcv_60m (
    datetime    TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    regime      TEXT    DEFAULT 'normal',
    PRIMARY KEY (datetime, ticker)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_60m_ticker ON ohlcv_60m(ticker, datetime);


-- ═══════════════════════════════════════════════════════════
--  2. 국내 일봉 (KIS API)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_ohlcv (
    date        TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    adj_close   REAL,
    regime      TEXT    DEFAULT 'normal',
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_daily_ticker ON daily_ohlcv(ticker, date);


-- ═══════════════════════════════════════════════════════════
--  3. 해외 지수/지표 일봉 (yfinance)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS global_daily (
    date        TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    name        TEXT,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL    NOT NULL,
    volume      INTEGER,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_global_ticker ON global_daily(ticker, date);


-- ═══════════════════════════════════════════════════════════
--  4. 한국 거시경제 (ECOS)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS kr_macro (
    date            TEXT    NOT NULL,
    indicator_code  TEXT    NOT NULL,
    indicator_name  TEXT    NOT NULL,
    value           REAL,
    unit            TEXT,
    PRIMARY KEY (date, indicator_code)
);


-- ═══════════════════════════════════════════════════════════
--  5. 미국 거시경제 (FRED)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS us_macro (
    date            TEXT    NOT NULL,
    series_id       TEXT    NOT NULL,
    series_name     TEXT    NOT NULL,
    value           REAL,
    unit            TEXT,
    PRIMARY KEY (date, series_id)
);


-- ═══════════════════════════════════════════════════════════
--  6. DART 공시
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS dart_disclosures (
    date            TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    corp_name       TEXT    NOT NULL,
    report_type     TEXT,
    title           TEXT    NOT NULL,
    url             TEXT,
    summary         TEXT,
    rcept_no        TEXT    UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_dart_ticker ON dart_disclosures(ticker, date);
CREATE INDEX IF NOT EXISTS idx_dart_date ON dart_disclosures(date);


-- ═══════════════════════════════════════════════════════════
--  7. 뉴스 헤드라인 (RSS)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS news_headlines (
    date        TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    summary     TEXT,
    category    TEXT,
    url         TEXT,
    UNIQUE(date, source, title)
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_headlines(date);


-- ═══════════════════════════════════════════════════════════
--  8. 중앙은행 성명서
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fomc_statements (
    date            TEXT    PRIMARY KEY,
    full_text       TEXT    NOT NULL,
    rate_decision   TEXT,
    key_phrases     TEXT
);

CREATE TABLE IF NOT EXISTS bok_statements (
    date            TEXT    PRIMARY KEY,
    full_text       TEXT    NOT NULL,
    rate_decision   TEXT
);


-- ═══════════════════════════════════════════════════════════
--  9. 시장 국면 (Regime)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS market_regime (
    date            TEXT    PRIMARY KEY,
    regime          TEXT    NOT NULL DEFAULT 'normal',
    score           INTEGER DEFAULT 0,
    vix             REAL,
    kospi_change    REAL,
    usd_krw_change  REAL,
    foreign_net     REAL,
    sp500_change    REAL,
    volume_ratio    REAL,
    trigger_reasons TEXT
);


-- ═══════════════════════════════════════════════════════════
--  10. 수집 메타데이터 (진행 추적)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS collection_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    ticker      TEXT,
    rows_added  INTEGER DEFAULT 0,
    status      TEXT    DEFAULT 'OK',
    message     TEXT
);
"""


def init_db(db_path: str = None):
    """모든 테이블을 생성한다. 이미 존재하면 skip."""
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info(f"DB 초기화 완료: {db_path or DB_PATH}")


# ── UPSERT 헬퍼 ──────────────────────────────────────────

def upsert_rows(table: str, rows: list, db_path: str = None):
    """
    INSERT OR REPLACE로 bulk upsert.
    rows: list of dict (컬럼명: 값)
    """
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})"
    values = [tuple(r[c] for c in columns) for r in rows]

    with get_conn(db_path) as conn:
        conn.executemany(sql, values)
        count = len(values)

    return count


def insert_rows_ignore(table: str, rows: list, db_path: str = None):
    """INSERT OR IGNORE로 bulk insert (중복 무시)."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    sql = f"INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({placeholders})"
    values = [tuple(r[c] for c in columns) for r in rows]

    with get_conn(db_path) as conn:
        cursor = conn.executemany(sql, values)
        return cursor.rowcount


# ── 조회 헬퍼 ────────────────────────────────────────────

def query(sql: str, params: tuple = (), db_path: str = None) -> list:
    """SELECT 쿼리 실행, list of dict 반환."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def query_df(sql: str, params: tuple = (), db_path: str = None):
    """SELECT 쿼리 → pandas DataFrame 반환."""
    import pandas as pd
    with get_conn(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_latest_date(table: str, ticker: str = None,
                    date_col: str = "date", db_path: str = None) -> str:
    """테이블에서 특정 ticker의 최신 날짜를 반환. 없으면 빈 문자열."""
    if ticker:
        sql = f"SELECT MAX({date_col}) as d FROM {table} WHERE ticker=?"
        result = query(sql, (ticker,), db_path)
    else:
        sql = f"SELECT MAX({date_col}) as d FROM {table}"
        result = query(sql, (), db_path)
    return result[0]["d"] or "" if result else ""


def get_row_count(table: str, db_path: str = None) -> int:
    """테이블 행 수 반환."""
    result = query(f"SELECT COUNT(*) as cnt FROM {table}", (), db_path)
    return result[0]["cnt"] if result else 0


def log_collection(source: str, action: str, ticker: str = None,
                   rows_added: int = 0, status: str = "OK",
                   message: str = "", db_path: str = None):
    """수집 작업 로그 기록."""
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "action": action,
        "ticker": ticker,
        "rows_added": rows_added,
        "status": status,
        "message": message,
    }
    insert_rows_ignore("collection_log", [row], db_path)


# ── DB 상태 리포트 ────────────────────────────────────────

def db_status_report(db_path: str = None) -> dict:
    """전체 테이블별 행 수와 최신 날짜 반환."""
    tables = [
        "ohlcv_5m", "ohlcv_15m", "ohlcv_60m",
        "daily_ohlcv", "global_daily",
        "kr_macro", "us_macro",
        "dart_disclosures", "news_headlines",
        "fomc_statements", "bok_statements",
        "market_regime",
    ]
    report = {}
    for t in tables:
        try:
            count = get_row_count(t, db_path)
            date_col = "datetime" if t.startswith("ohlcv_") else "date"
            latest = get_latest_date(t, date_col=date_col, db_path=db_path)
            report[t] = {"rows": count, "latest": latest}
        except Exception:
            report[t] = {"rows": 0, "latest": ""}
    return report


# ── 초기화 실행 ───────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("=" * 55)
    print("  QUANTUM FLOW — DB 초기화 완료")
    print("=" * 55)
    status = db_status_report()
    for table, info in status.items():
        print(f"  {table:25s} | {info['rows']:>8,} rows | latest: {info['latest']}")
    print(f"\n  DB 경로: {DB_PATH}")
    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"  DB 크기: {size_mb:.2f} MB")
