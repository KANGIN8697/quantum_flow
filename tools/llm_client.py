# tools/llm_client.py — 하이브리드 LLM 클라이언트
# Claude Sonnet: 분석 (Agent 1, 2)  /  GPT-4o-mini: 분류 (Agent 4)
# 싱글턴 패턴, 자동 비용 추적, JSON 파싱 내장

import os
import re
import json
import threading
from dotenv import load_dotenv

load_dotenv()

from tools.cost_tracker import get_cost_tracker


class LLMClient:
    """하이브리드 LLM 래퍼. analyze=Claude, classify=GPT."""

    def __init__(self):
        # ── Anthropic (분석용) ────────────────────────────
        import anthropic
        self._anthropic = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        )
        self.analysis_model = os.getenv(
            "ANALYSIS_MODEL", "claude-sonnet-4-5-20250929"
        )

        # ── OpenAI (분류용) ───────────────────────────────
        import openai
        self._openai = openai.OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""),
        )
        self.sentinel_model = os.getenv(
            "SENTINEL_MODEL", "gpt-4o-mini"
        )

        self._tracker = get_cost_tracker()

    # ── 분석 (Claude Sonnet) ──────────────────────────────

    def analyze(self, system: str, user: str,
                temperature: float = 0.3, max_tokens: int = 3000) -> str:
        """Claude Sonnet 호출. 텍스트 응답 반환."""
        resp = self._anthropic.messages.create(
            model=self.analysis_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text

        self._tracker.record(
            self.analysis_model,
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )
        return text

    def analyze_json(self, system: str, user: str,
                     temperature: float = 0.3, max_tokens: int = 3000) -> dict:
        """Claude Sonnet 호출 → JSON dict 반환."""
        text = self.analyze(system, user, temperature, max_tokens)
        return self._parse_json(text)

    # ── 분류 (GPT-4o-mini) ────────────────────────────────

    def classify(self, prompt: str,
                 temperature: float = 0.1, max_tokens: int = 200) -> str:
        """GPT-4o-mini 호출. 텍스트 응답 반환."""
        resp = self._openai.chat.completions.create(
            model=self.sentinel_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = resp.choices[0].message.content.strip()

        usage = resp.usage
        self._tracker.record(
            self.sentinel_model,
            usage.prompt_tokens,
            usage.completion_tokens,
        )
        return text

    def classify_json(self, prompt: str,
                      temperature: float = 0.1, max_tokens: int = 200) -> dict:
        """GPT-4o-mini 호출 → JSON dict 반환."""
        text = self.classify(prompt, temperature, max_tokens)
        return self._parse_json(text)

    # ── JSON 파싱 유틸 ────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        """LLM 응답에서 JSON 추출. 실패 시 빈 dict."""
        # 1) ```json ... ``` 블록
        m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 2) ``` ... ``` 블록
        m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 3) 첫 번째 { ... } 직접 매칭
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        return {}


# ── 싱글턴 ─────────────────────────────────────────────────
_instance = None
_init_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = LLMClient()
    return _instance
