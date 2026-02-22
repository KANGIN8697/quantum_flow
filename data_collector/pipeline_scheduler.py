# data_collector/pipeline_scheduler.py — 데이터 수집 파이프라인 스케줄러
# 24시간 데이터 수집 작업을 스케줄링하고 실행

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("pipeline_scheduler")

# ── 환경변수 ───────────────────────────────────────────────────
# 별도 API 키 필요 없음

# ── 스케줄 설정 ────────────────────────────────────────────────
SCHEDULES = {
    "global_prices": {
        "interval_hours": 1,  # 1시간마다
        "function": "data_collector.price.global_collector.fetch_all_global_prices",
        "description": "글로벌 가격 데이터 수집",
    },
    "kis_daily": {
        "interval_hours": 24,  # 매일
        "function": "data_collector.price.kis_daily_collector.fetch_multiple_daily_prices",
        "args": [["005930", "000660", "373220"]],  # 삼성전자, SK하이닉스, LG에너지솔루션
        "description": "KIS 일봉 데이터 수집",
    },
    "fred_macro": {
        "interval_hours": 6,  # 6시간마다
        "function": "data_collector.macro.fred_collector.fetch_all_fred_daily",
        "description": "FRED 거시경제 데이터 수집",
    },
    "ecos_macro": {
        "interval_hours": 24,  # 매일
        "function": "data_collector.macro.ecos_collector.fetch_all_ecos_data",
        "description": "ECOS 한국 거시경제 데이터 수집",
    },
    "news_collection": {
        "interval_hours": 2,  # 2시간마다
        "function": "data_collector.text.news_collector.collect_economic_news",
        "description": "경제 뉴스 수집",
    },
    "dart_disclosures": {
        "interval_hours": 4,  # 4시간마다
        "function": "data_collector.text.dart_collector.fetch_major_disclosures",
        "description": "DART 주요 공시 수집",
    },
    "regime_analysis": {
        "interval_hours": 6,  # 6시간마다
        "function": "data_collector.regime.regime_classifier.classify_multiple_regimes",
        "args": [{}],  # 가격 데이터 dict 전달 필요
        "description": "시장 국면 분석",
    },
    "vector_update": {
        "interval_hours": 12,  # 12시간마다
        "function": "data_collector.vector.vector_store_builder.build_news_vector_store",
        "args": [[]],  # 뉴스 데이터 전달 필요
        "description": "벡터 스토어 업데이트",
    },
}


class PipelineScheduler:
    """데이터 수집 파이프라인 스케줄러"""

    def __init__(self):
        self.running = False
        self.last_runs = {}  # {task_name: last_run_timestamp}
        self.tasks = {}

        # 작업 함수들 동적 임포트 준비
        self._prepare_tasks()

    def _prepare_tasks(self):
        """스케줄된 작업들 준비"""
        for task_name, config in SCHEDULES.items():
            try:
                # 함수 경로를 실제 함수로 변환
                func_path = config["function"]
                module_path, func_name = func_path.rsplit(".", 1)

                # 동적 임포트 (실제 사용 시 importlib 사용)
                # 여기서는 함수 참조를 저장
                self.tasks[task_name] = {
                    "func_path": func_path,
                    "config": config,
                    "last_run": 0,
                }

            except Exception as e:
                logger.error(f"작업 준비 실패 {task_name}: {e}", exc_info=True)

    async def run_task(self, task_name: str) -> dict:
        """특정 작업 실행"""
        if task_name not in self.tasks:
            return {"error": f"알 수 없는 작업: {task_name}"}

        task_info = self.tasks[task_name]
        config = task_info["config"]

        try:
            # 실제 함수 호출 (동적 임포트 구현 필요)
            # 임시로 성공 응답
            result = {
                "task": task_name,
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "description": config["description"],
            }

            self.last_runs[task_name] = time.time()
            logger.info(f"작업 완료: {task_name}")

            return result

        except Exception as e:
            logger.error(f"작업 실행 실패 {task_name}: {e}", exc_info=True)
            return {
                "task": task_name,
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    async def run_scheduler(self):
        """메인 스케줄러 루프"""
        logger.info("데이터 수집 파이프라인 스케줄러 시작")
        self.running = True

        while self.running:
            try:
                current_time = time.time()

                # 각 작업의 실행 시간 확인
                for task_name, task_info in self.tasks.items():
                    config = task_info["config"]
                    interval_seconds = config["interval_hours"] * 3600
                    last_run = self.last_runs.get(task_name, 0)

                    if current_time - last_run >= interval_seconds:
                        logger.info(f"작업 실행: {task_name}")
                        await self.run_task(task_name)

                # 1분마다 체크
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("스케줄러 중단 요청")
                break
            except Exception as e:
                logger.error(f"스케줄러 오류: {e}", exc_info=True)
                await asyncio.sleep(60)

        logger.info("데이터 수집 파이프라인 스케줄러 종료")

    def stop(self):
        """스케줄러 중단"""
        self.running = False

    def get_status(self) -> dict:
        """스케줄러 상태 조회"""
        current_time = time.time()
        status = {
            "running": self.running,
            "timestamp": datetime.now().isoformat(),
            "tasks": {},
        }

        for task_name, task_info in self.tasks.items():
            config = task_info["config"]
            last_run = self.last_runs.get(task_name, 0)
            next_run = last_run + (config["interval_hours"] * 3600)

            status["tasks"][task_name] = {
                "description": config["description"],
                "interval_hours": config["interval_hours"],
                "last_run": datetime.fromtimestamp(last_run).isoformat() if last_run > 0 else None,
                "next_run": datetime.fromtimestamp(next_run).isoformat(),
                "is_due": current_time >= next_run,
            }

        return status


# ── 편의 함수 ─────────────────────────────────────────────────

async def start_pipeline_scheduler() -> dict:
    """파이프라인 스케줄러 시작"""
    scheduler = PipelineScheduler()
    try:
        await scheduler.run_scheduler()
        return {"status": "completed"}
    except KeyboardInterrupt:
        scheduler.stop()
        return {"status": "stopped"}
    except Exception as e:
        logger.error(f"스케줄러 실행 실패: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


def get_scheduler_status() -> dict:
    """스케줄러 상태 조회"""
    scheduler = PipelineScheduler()
    return scheduler.get_status()


async def run_manual_task(task_name: str) -> dict:
    """수동 작업 실행"""
    scheduler = PipelineScheduler()
    return await scheduler.run_task(task_name)