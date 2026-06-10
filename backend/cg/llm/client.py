"""Minimal OpenAI-compatible LLM client with JSON helpers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from openai import RateLimitError

from cg.settings import Settings, get_settings


_LLM_RATE_LOCK = asyncio.Lock()
_LAST_LLM_REQUEST_AT = 0.0


class LLMClient:
    """Small wrapper around OpenAI-compatible chat APIs."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.cg_llm_model
        self.provider = self.settings.cg_llm_provider
        self._client: AsyncOpenAI | None = None
        if self.is_configured:
            self._client = AsyncOpenAI(
                api_key=self.settings.active_llm_api_key,
                base_url=self.settings.active_llm_base_url,
                timeout=self.settings.cg_llm_timeout_seconds,
            )

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.active_llm_api_key)

    async def complete(self, system: str, user: str, *, temperature: float | None = None) -> str:
        if not self._client:
            raise RuntimeError("LLM is not configured")
        last_error: Exception | None = None
        for attempt in range(self.settings.cg_llm_max_retries + 1):
            await self._wait_for_slot()
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    temperature=self.settings.cg_llm_temperature if temperature is None else temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return response.choices[0].message.content or ""
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                last_error = exc
                if attempt >= self.settings.cg_llm_max_retries:
                    raise
                cooldown = self.settings.cg_llm_rate_limit_cooldown_seconds * (attempt + 1)
                await asyncio.sleep(cooldown)
        raise last_error or RuntimeError("LLM request failed")

    async def _wait_for_slot(self) -> None:
        global _LAST_LLM_REQUEST_AT
        interval = self.settings.cg_llm_min_interval_seconds
        if interval <= 0:
            return
        async with _LLM_RATE_LOCK:
            now = time.monotonic()
            wait_for = interval - (now - _LAST_LLM_REQUEST_AT)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            _LAST_LLM_REQUEST_AT = time.monotonic()

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        text = await self.complete(system, user, temperature=temperature)
        try:
            return parse_json_object(text)
        except (json.JSONDecodeError, ValueError):
            repair_system = (
                "You repair malformed JSON. Output only one valid JSON object. "
                "Do not explain. Do not wrap the object in Markdown."
            )
            repair_user = (
                "Repair the following text into a valid JSON object. Preserve fields and content where possible. "
                "If it cannot be repaired, output {}.\n\n"
                f"{text[:24000]}"
            )
            repaired = await self.complete(repair_system, repair_user, temperature=0)
            try:
                return parse_json_object(repaired)
            except (json.JSONDecodeError, ValueError):
                return {}


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response, including fenced blocks."""

    stripped = text.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S)
        if match:
            stripped = match.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM response is not a JSON object")
    return value
