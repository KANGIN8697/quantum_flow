# data_collector/vector/vector_store_builder.py — 벡터 스토어 구축
# ChromaDB를 사용한 텍스트 데이터 벡터화 및 저장

import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    chromadb = None

try:
    import sentence_transformers
    from sentence_transformers import SentenceTransformer
except ImportError:
    sentence_transformers = None

logger = logging.getLogger("vector_store_builder")

# ── 환경변수 ───────────────────────────────────────────────────
# 별도 API 키 필요 없음 (로컬 ChromaDB 사용)

# ── 벡터 스토어 설정 ───────────────────────────────────────────
CHROMA_DB_PATH = "./outputs/vector_db"
COLLECTION_NAME = "quantum_flow_docs"

# 모델 설정
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # 경량 모델


class VectorStoreBuilder:
    """ChromaDB 벡터 스토어 관리 클래스"""

    def __init__(self):
        if chromadb is None:
            raise ImportError("chromadb 라이브러리가 설치되지 않았습니다")

        if sentence_transformers is None:
            raise ImportError("sentence-transformers 라이브러리가 설치되지 않았습니다")

        # ChromaDB 클라이언트 초기화
        self.client = chromadb.PersistentClient(
            path=CHROMA_DB_PATH,
            settings=Settings(anonymized_telemetry=False)
        )

        # 컬렉션 생성 또는 가져오기
        try:
            self.collection = self.client.get_collection(COLLECTION_NAME)
        except ValueError:
            self.collection = self.client.create_collection(COLLECTION_NAME)

        # 임베딩 모델 초기화
        self.model = SentenceTransformer(EMBEDDING_MODEL)

        logger.info("벡터 스토어 초기화 완료")

    def add_documents(self, documents: list, metadata: dict = None) -> bool:
        """문서들을 벡터 스토어에 추가"""
        try:
            if not documents:
                return False

            # 문서 텍스트 추출
            texts = []
            ids = []
            metadatas = []

            for i, doc in enumerate(documents):
                if isinstance(doc, dict):
                    text = doc.get("content", doc.get("text", ""))
                    doc_metadata = doc.get("metadata", {})
                else:
                    text = str(doc)
                    doc_metadata = {}

                if metadata:
                    doc_metadata.update(metadata)

                texts.append(text)
                ids.append(f"doc_{int(time.time())}_{i}")
                metadatas.append(doc_metadata)

            # 임베딩 생성
            embeddings = self.model.encode(texts, convert_to_list=True)

            # ChromaDB에 추가
            self.collection.add(
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
                ids=ids
            )

            logger.info(f"{len(documents)}개 문서 벡터화 및 저장 완료")
            return True

        except Exception as e:
            logger.error(f"문서 추가 실패: {e}", exc_info=True)
            return False

    def search_similar(self, query: str, n_results: int = 5) -> list:
        """유사 문서 검색"""
        try:
            # 쿼리 임베딩
            query_embedding = self.model.encode([query], convert_to_list=True)[0]

            # 유사도 검색
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"]
            )

            # 결과 포맷팅
            similar_docs = []
            for i, doc in enumerate(results["documents"][0]):
                similar_docs.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i],
                })

            return similar_docs

        except Exception as e:
            logger.error(f"유사 문서 검색 실패: {e}", exc_info=True)
            return []

    def get_collection_stats(self) -> dict:
        """컬렉션 통계 정보"""
        try:
            count = self.collection.count()
            return {
                "total_documents": count,
                "collection_name": COLLECTION_NAME,
                "embedding_model": EMBEDDING_MODEL,
                "last_updated": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"통계 조회 실패: {e}", exc_info=True)
            return {}

    def clear_collection(self) -> bool:
        """컬렉션 초기화"""
        try:
            self.client.delete_collection(COLLECTION_NAME)
            self.collection = self.client.create_collection(COLLECTION_NAME)
            logger.info("벡터 스토어 초기화 완료")
            return True
        except Exception as e:
            logger.error(f"컬렉션 초기화 실패: {e}", exc_info=True)
            return False


# ── 편의 함수 ─────────────────────────────────────────────────

def build_news_vector_store(news_list: list) -> bool:
    """뉴스 데이터를 벡터 스토어에 구축"""
    try:
        builder = VectorStoreBuilder()

        # 뉴스 데이터를 문서 형식으로 변환
        documents = []
        for news in news_list:
            content = f"{news.get('title', '')} {news.get('description', '')}"
            if content.strip():
                documents.append({
                    "content": content,
                    "metadata": {
                        "source": news.get("source", ""),
                        "pubDate": news.get("pubDate", ""),
                        "link": news.get("link", ""),
                        "type": "news",
                    }
                })

        return builder.add_documents(documents, {"batch_type": "news"})

    except Exception as e:
        logger.error(f"뉴스 벡터 스토어 구축 실패: {e}", exc_info=True)
        return False


def build_disclosure_vector_store(disclosures: list) -> bool:
    """공시 데이터를 벡터 스토어에 구축"""
    try:
        builder = VectorStoreBuilder()

        documents = []
        for disc in disclosures:
            content = f"{disc.get('corp_name', '')} {disc.get('report_nm', '')}"
            if content.strip():
                documents.append({
                    "content": content,
                    "metadata": {
                        "corp_code": disc.get("corp_code", ""),
                        "corp_name": disc.get("corp_name", ""),
                        "rcept_dt": disc.get("rcept_dt", ""),
                        "link": disc.get("link", ""),
                        "type": "disclosure",
                    }
                })

        return builder.add_documents(documents, {"batch_type": "disclosure"})

    except Exception as e:
        logger.error(f"공시 벡터 스토어 구축 실패: {e}", exc_info=True)
        return False


def search_vector_store(query: str, n_results: int = 5) -> list:
    """벡터 스토어에서 검색"""
    try:
        builder = VectorStoreBuilder()
        return builder.search_similar(query, n_results)
    except Exception as e:
        logger.error(f"벡터 검색 실패: {e}", exc_info=True)
        return []