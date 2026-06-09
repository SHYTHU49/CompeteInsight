"""Agent runtime primitives for CompeteGraph."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from cg.llm import LLMClient
from cg.settings import Settings


@dataclass(slots=True)
class AgentContext:
    run_id: str
    run_dir: Path
    settings: Settings
    llm: LLMClient
    trace: Callable[[str, str, str, str, dict[str, Any] | None], Awaitable[None]] | None = None


class BaseAgent:
    name = "BaseAgent"
    skill_id = "skill.base.v1"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    @property
    def llm_enabled(self) -> bool:
        return self.ctx.llm.is_configured

    async def invoke_json(self, system: str, user: str) -> dict[str, Any] | None:
        if not self.llm_enabled:
            return None
        try:
            data = await self.ctx.llm.complete_json(system, user)
            return data
        except Exception as exc:
            await self.record_llm_event(
                "error",
                "failed",
                "LLM JSON call failed; deterministic fallback will be used",
                {"error_type": exc.__class__.__name__, "error": str(exc)[:500]},
            )
            return None

    async def invoke_text(self, system: str, user: str) -> str | None:
        if not self.llm_enabled:
            return None
        try:
            text = await self.ctx.llm.complete(system, user)
            return text
        except Exception as exc:
            await self.record_llm_event(
                "error",
                "failed",
                "LLM text call failed; deterministic fallback will be used",
                {"error_type": exc.__class__.__name__, "error": str(exc)[:500]},
            )
            return None

    async def record_llm_event(
        self,
        phase: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.ctx.trace is None:
            return
        event_payload = {
            "provider": self.ctx.llm.provider,
            "model": self.ctx.llm.model,
            **(payload or {}),
        }
        await self.ctx.trace(self.name, phase, status, message, event_payload)
