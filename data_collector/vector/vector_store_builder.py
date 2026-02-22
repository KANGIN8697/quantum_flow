# data_collector/vector/vector_store_builder.py — ChromaDB 벡터 스토어 구축
#
# Agent 1이 "오늘과 유사한 과거 국면"을 검색하는 핵심 모듈
#
# 사용 라이브러리:
#   chromadb (로컬 벡터 DB)
#   sentence-transformers (임베딩 생성)
#   → 모델: jhgan/ko-sroberta-multitask (한국어 특화)
#
# 임베딩 대상:
#   1. 날짜별 매크로 요약 텍스트 (macro_daily 컬렉션)
#   2. FOMC 성명서 문단 (fomc_docs 컬렉션)
#   3. 한은 성명서 문단 (bok_docs 컬렉션)

import os
import sys
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("vector_store_builder")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from database.db_manager import query, query_df, init_db

# ── 라이브러리 가용성 체크 ────────────────────────────────

_CHROMA_AVAILABLE = False
_EMBEDDER_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_AVAILABLE = True
except ImportError:
    logger.warning("chromadb 미설치 — pip install chromadb")

try:
    from sentence_transformers import SentenceTransformer
    _EMBEDDER_AVAILABLE = True
except ImportError:
    logger.warning("sentence-transformers 미설치 — pip install sentence-transformers")


# ── 경로 설정 ─────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.path.join(BASE_DIR, "..", "..", "database", "chroma")
EMBEDDING_MODEL = "jhgan/ko-sroberta-multitask"


# ── ChromaDB 클라이언트 / 임베더 싱글톤 ──────────────────

_chroma_client = None
_embedder = None


def _get_chroma():
    """ChromaDB 클라이언트 싱글톤."""
    global _chroma_client
    if _chroma_client is None:
        if not _CHROMA_AVAILABLE:
            raise RuntimeError("chromadb 미설치")
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _chroma_client


def _get_embedder():
    """SentenceTransformer 싱글톤."""
    global _embedder
    if _embedder is None:
        if not _EMBEDDER_AVAILABLE:
            raise RuntimeError("sentence-transformers 미설치")
        logger.info(f"임베딩 모델 로딩: {EMBEDDING_MODEL}")
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
    return _embedder


def _embed(texts: list) -> list:
    """텍스트 리스트를 벡터로 변환."""
    embedder = _get_embedder()
    embeddings = embedder.encode(texts, show_progress_bar=False)
    return [emb.tolist() for emb in embeddings]


# ══════════════════════════════════════════════════════════════
#  1. 날짜별 매크로 요약 임베딩 (macro_daily 컬렉션)
# ══════════════════════════════════════════════════════════════

def _build_daily_summary(date: str) -> str:
    """
    날짜별 매크로 요약 텍스트 생성.
    형식: "날짜: {date}, KOSPI: {change}%, VIX: {vix},
           원달러: {usd_krw}, 외인: {foreign_net}억,
           S&P500: {sp500_change}%, 헤드라인: {top3_headlines}"
    """
    # Regime 정보
    regime_rows = query(
        "SELECT * FROM market_regime WHERE date=?", (date,)
    )
    regime_info = regime_rows[0] if regime_rows else {}

    # VIX
    vix_rows = query(
        "SELECT close FROM global_daily WHERE date=? AND ticker='^VIX'", (date,)
    )
    vix = vix_rows[0]["close"] if vix_rows else "N/A"

    # 원달러
    krw_rows = query(
        "SELECT close FROM global_daily WHERE date=? AND ticker='KRW=X'", (date,)
    )
    usd_krw = krw_rows[0]["close"] if krw_rows else "N/A"

    # S&P500 등락
    sp500_change = regime_info.get("sp500_change", "N/A")
    kospi_change = regime_info.get("kospi_change", "N/A")
    foreign_net = regime_info.get("foreign_net", "N/A")

    # 뉴스 헤드라인 (상위 3건)
    news_rows = query(
        "SELECT title FROM news_headlines WHERE date LIKE ? ORDER BY date DESC LIMIT 3",
        (f"{date}%",)
    )
    headlines = [r["title"] for r in news_rows] if news_rows else []
    headline_str = " / ".join(headlines) if headlines else "뉴스 없음"

    regime = regime_info.get("regime", "N/A")

    summary = (
        f"날짜: {date}, 국면: {regime}, "
        f"KOSPI: {kospi_change}%, VIX: {vix}, "
        f"원달러: {usd_krw}, 외인: {foreign_net}억, "
        f"S&P500: {sp500_change}%, "
        f"헤드라인: {headline_str}"
    )
    return summary


def build_macro_daily_embeddings(start_date: str = "2020-01-01",
                                 end_date: str = None) -> int:
    """
    날짜별 매크로 요약 텍스트를 생성하고 ChromaDB에 저장.
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    client = _get_chroma()
    collection = client.get_or_create_collection(
        name="macro_daily",
        metadata={"description": "날짜별 매크로 요약 임베딩"}
    )

    # 이미 저장된 ID 확인
    existing = set()
    try:
        existing_data = collection.get()
        existing = set(existing_data["ids"]) if existing_data["ids"] else set()
    except Exception:
        pass

    # Regime이 있는 날짜 목록
    dates = query(
        "SELECT date FROM market_regime WHERE date>=? AND date<=? ORDER BY date",
        (start_date, end_date)
    )

    if not dates:
        logger.info("임베딩 대상 날짜 없음")
        return 0

    batch_ids = []
    batch_docs = []
    batch_embeddings = []
    batch_meta = []
    count = 0

    for row in dates:
        date = row["date"]
        doc_id = f"macro_{date}"

        if doc_id in existing:
            continue

        summary = _build_daily_summary(date)

        # Regime 메타데이터
        regime_rows = query("SELECT regime, score FROM market_regime WHERE date=?", (date,))
        regime = regime_rows[0]["regime"] if regime_rows else "normal"
        score = regime_rows[0]["score"] if regime_rows else 0

        batch_ids.append(doc_id)
        batch_docs.append(summary)
        batch_meta.append({"date": date, "regime": regime, "score": score})
        count += 1

        # 50건씩 배치 임베딩
        if len(batch_ids) >= 50:
            embeddings = _embed(batch_docs)
            collection.add(
                ids=batch_ids,
                documents=batch_docs,
                embeddings=embeddings,
                metadatas=batch_meta,
            )
            logger.info(f"  macro_daily: {count}건 임베딩 저장")
            batch_ids, batch_docs, batch_embeddings, batch_meta = [], [], [], []

    # 잔여 배치 처리
    if batch_ids:
        embeddings = _embed(batch_docs)
        collection.add(
            ids=batch_ids,
            documents=batch_docs,
            embeddings=embeddings,
            metadatas=batch_meta,
        )

    logger.info(f"macro_daily 임베딩 완료: {count}건 추가")
    return count


# ══════════════════════════════════════════════════════════════
#  2. FOMC/한은 성명서 임베딩 (Phase 3)
# ══════════════════════════════════════════════════════════════

def build_fomc_embeddings() -> int:
    """FOMC 성명서를 문단 단위로 분할하여 임베딩."""
    client = _get_chroma()
    collection = client.get_or_create_collection(
        name="fomc_docs",
        metadata={"description": "FOMC 성명서 임베딩"}
    )

    statements = query("SELECT date, full_text, rate_decision FROM fomc_statements")
    if not statements:
        logger.info("FOMC 성명서 없음 (Phase 3에서 수집)")
        return 0

    count = 0
    for stmt in statements:
        date = stmt["date"]
        text = stmt["full_text"]
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 50]

        for i, para in enumerate(paragraphs):
            doc_id = f"fomc_{date}_{i}"
            embedding = _embed([para])[0]
            collection.add(
                ids=[doc_id],
                documents=[para],
                embeddings=[embedding],
                metadatas=[{
                    "date": date,
                    "rate_decision": stmt.get("rate_decision", ""),
                    "paragraph_index": i,
                }],
            )
            count += 1

    logger.info(f"FOMC 임베딩 완료: {count}개 문단")
    return count


def build_bok_embeddings() -> int:
    """한은 성명서 임베딩."""
    client = _get_chroma()
    collection = client.get_or_create_collection(
        name="bok_docs",
        metadata={"description": "한은 통화정책방향 임베딩"}
    )

    statements = query("SELECT date, full_text, rate_decision FROM bok_statements")
    if not statements:
        logger.info("한은 성명서 없음 (Phase 3에서 수집)")
        return 0

    count = 0
    for stmt in statements:
        date = stmt["date"]
        text = stmt["full_text"]
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 30]

        for i, para in enumerate(paragraphs):
            doc_id = f"bok_{date}_{i}"
            embedding = _embed([para])[0]
            collection.add(
                ids=[doc_id],
                documents=[para],
                embeddings=[embedding],
                metadatas=[{"date": date, "rate_decision": stmt.get("rate_decision", "")}],
            )
            count += 1

    logger.info(f"한은 임베딩 완료: {count}개 문단")
    return count


# ══════════════════════════════════════════════════════════════
#  3. 유사 과거 국면 검색 (Agent 1 핵심 함수)
# ══════════════════════════════════════════════════════════════

def find_similar_periods(today_summary: str, n: int = 5) -> list:
    """
    오늘 상황 텍스트를 임베딩으로 변환하고,
    벡터 DB에서 코사인 유사도 상위 n개 과거 국면을 반환한다.

    Parameters
    ----------
    today_summary : 오늘 매크로 요약 텍스트
    n             : 반환할 유사 국면 수

    Returns
    -------
    list of dict: [{date, regime, summary, similarity_score, outcome_3d, outcome_5d}, ...]
    """
    client = _get_chroma()
    collection = client.get_or_create_collection(name="macro_daily")

    # 오늘 요약 임베딩
    query_embedding = _embed([today_summary])[0]

    # 유사도 검색
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"] or not results["ids"][0]:
        return []

    similar = []
    for i, doc_id in enumerate(results["ids"][0]):
        date = results["metadatas"][0][i].get("date", "")
        regime = results["metadatas"][0][i].get("regime", "")
        summary = results["documents"][0][i]
        distance = results["distances"][0][i]
        similarity = round(1 - distance, 4)  # 코사인 거리 → 유사도

        # 해당 날짜 이후 3일/5일 수익률 계산
        outcome_3d = _get_kospi_outcome(date, days=3)
        outcome_5d = _get_kospi_outcome(date, days=5)

        similar.append({
            "date": date,
            "regime": regime,
            "summary": summary,
            "similarity_score": similarity,
            "outcome_3d": outcome_3d,
            "outcome_5d": outcome_5d,
        })

    return similar


def _get_kospi_outcome(date: str, days: int) -> float:
    """특정 날짜 이후 N일간 코스피(또는 S&P500) 등락률."""
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days + 2)
        target_str = target_date.strftime("%Y-%m-%d")

        rows = query(
            "SELECT close FROM global_daily WHERE ticker='^GSPC' AND date>=? AND date<=? ORDER BY date",
            (date, target_str)
        )
        if len(rows) >= 2:
            start_close = rows[0]["close"]
            end_close = rows[-1]["close"]
            if start_close > 0:
                return round((end_close - start_close) / start_close * 100, 2)
    except Exception:
        pass
    return None


# ── 일일 자동 추가 ────────────────────────────────────────

def add_today():
    """매일 16:30 실행: 오늘 데이터를 벡터 DB에 추가."""
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"벡터 DB 당일 추가: {today}")

    try:
        count = build_macro_daily_embeddings(start_date=today, end_date=today)
        logger.info(f"  {count}건 임베딩 추가")
        return count
    except Exception as e:
        logger.error(f"벡터 DB 추가 실패: {e}")
        return 0


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 55)
    print("  QUANTUM FLOW — 벡터 DB 구축")
    print("=" * 55)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="전체 임베딩 빌드")
    parser.add_argument("--search", type=str, help="유사 국면 검색 (텍스트)")
    parser.add_argument("--start", type=str, default="2020-01-01")
    args = parser.parse_args()

    if args.build:
        if not _CHROMA_AVAILABLE or not _EMBEDDER_AVAILABLE:
            print("  필요 라이브러리: pip install chromadb sentence-transformers")
        else:
            init_db()
            count = build_macro_daily_embeddings(start_date=args.start)
            print(f"\n  {count}건 임베딩 완료")
    elif args.search:
        results = find_similar_periods(args.search)
        for r in results:
            print(f"  {r['date']} [{r['regime']}] sim={r['similarity_score']:.3f} "
                  f"3d={r['outcome_3d']}% 5d={r['outcome_5d']}%")
    else:
        print("  --build: 전체 임베딩 빌드")
        print("  --search '텍스트': 유사 국면 검색")
