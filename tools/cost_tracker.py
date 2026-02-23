# tools/cost_tracker.py — LLM API 비용 추적기
# 모델별 토큰 사용량 및 비용을 기록하고 일일 요약 제공

import threading
from dataclasses import dataclass, field
from datetime import datetime


# ── 모델별 1K 토큰당 가격 (USD) ──────────────────────────────
PRICING = {
    # Anthropic Claude
    "claude-sonnet-4-5-20250929":  {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5-20251001":   {"input": 0.001, "output": 0.005},
    # OpenAI
    "gpt-4o-mini":                 {"input": 0.00015, "output": 0.0006},
}


@dataclass
class CostTracker:
    """모델별 토큰 사용량 및 비용 추적."""
    _records: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, model: str, input_tokens: int, output_tokens: int):
        """API 호출 1건의 토큰 사용량을 기록."""
        prices = PRICING.get(model, {"input": 0, "output": 0})
        cost = (input_tokens / 1000) * prices["input"] + \
               (output_tokens / 1000) * prices["output"]
        with self._lock:
            self._records.append({
                "ts": datetime.now().isoformat(),
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost, 6),
            })

    def daily_summary(self) -> dict:
        """오늘 누적 비용 요약."""
        with self._lock:
            if not self._records:
                return {"total_calls": 0, "total_cost_usd": 0, "by_model": {}}

            by_model = {}
            for r in self._records:
                m = r["model"]
                if m not in by_model:
                    by_model[m] = {"calls": 0, "input_tokens": 0,
                                   "output_tokens": 0, "cost_usd": 0}
                by_model[m]["calls"] += 1
                by_model[m]["input_tokens"] += r["input_tokens"]
                by_model[m]["output_tokens"] += r["output_tokens"]
                by_model[m]["cost_usd"] += r["cost_usd"]

            total_cost = sum(v["cost_usd"] for v in by_model.values())
            return {
                "total_calls": len(self._records),
                "total_cost_usd": round(total_cost, 4),
                "by_model": by_model,
            }

    def reset(self):
        """일일 리셋."""
        with self._lock:
            self._records.clear()


# ── 싱글턴 ─────────────────────────────────────────────────
_instance = None
_init_lock = threading.Lock()


def get_cost_tracker() -> CostTracker:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = CostTracker()
    return _instance
