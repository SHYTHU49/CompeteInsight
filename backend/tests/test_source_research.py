"""Source discovery continuation behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cg.agents.research_agents import SourceResearchAgent
from cg.agents.runtime import AgentContext
from cg.llm import LLMClient
from cg.schemas.research import ResearchPlan, ResearchRequest, SourceCandidate, SourceTask
from cg.settings import Settings


class FakeSearch:
    def __init__(self, responses: dict[str, list[SourceCandidate]]):
        self.responses = responses
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 3) -> list[SourceCandidate]:
        self.queries.append(query)
        return [
            item.model_copy()
            for item in self.responses.get(query, [])[:max_results]
        ]


@pytest.mark.asyncio
async def test_discover_runs_gap_search_when_first_pass_has_coverage_gaps(tmp_path: Path) -> None:
    request = ResearchRequest(
        project_name="Gap search",
        target_product="Alpha",
        competitors=["Beta"],
        analysis_dimensions=["positioning", "pricing"],
        max_sources=4,
        max_sources_per_query=2,
    )
    plan = ResearchPlan(
        research_goal=request.research_goal,
        competitors=request.competitors,
        dimensions=request.analysis_dimensions,
        queries=[],
        source_tasks=[
            SourceTask(
                task_id="task_01",
                entity="Alpha",
                dimension="positioning",
                intent="official",
                query="Alpha official product features",
                expected_source_types=["official_website"],
            ),
            SourceTask(
                task_id="task_02",
                entity="Beta",
                dimension="pricing",
                intent="pricing",
                query="Beta pricing plans",
                expected_source_types=["pricing_page"],
            ),
        ],
    )
    search = FakeSearch(
        {
            "Alpha official product features": [
                SourceCandidate(
                    url="https://alpha.example/product",
                    title="Alpha product",
                    source_type="official_website",
                    score=0.7,
                )
            ],
            "Beta pricing plans": [
                SourceCandidate(
                    url="https://beta.example/pricing",
                    title="Beta pricing",
                    source_type="pricing_page",
                    score=0.72,
                )
            ],
        }
    )

    candidates = await make_agent(tmp_path).discover(request, plan, search)  # type: ignore[arg-type]

    assert search.queries[:2] == ["Alpha official product features", "Beta pricing plans"]
    assert "Alpha pricing plans" in search.queries
    assert "Beta official product positioning features" in search.queries
    assert {candidate.url for candidate in candidates} == {
        "https://beta.example/pricing",
        "https://alpha.example/product",
    }
    assert all(candidate.query for candidate in candidates)


@pytest.mark.asyncio
async def test_discover_uses_second_pass_to_break_domain_concentration(tmp_path: Path) -> None:
    request = ResearchRequest(
        project_name="Domain diversity",
        target_product="Alpha",
        competitors=["Beta"],
        analysis_dimensions=["positioning", "user_voice"],
        max_sources=3,
        max_sources_per_query=3,
    )
    plan = ResearchPlan(
        research_goal=request.research_goal,
        competitors=request.competitors,
        dimensions=request.analysis_dimensions,
        queries=[],
        source_tasks=[
            SourceTask(
                task_id="task_01",
                entity="Alpha",
                dimension="positioning",
                intent="official",
                query="Alpha official product features",
                expected_source_types=["official_website"],
            )
        ],
    )
    search = FakeSearch(
        {
            "Alpha official product features": [
                SourceCandidate(
                    url="https://alpha.example/product",
                    title="Alpha product",
                    source_type="official_website",
                    score=0.83,
                ),
                SourceCandidate(
                    url="https://alpha.example/docs",
                    title="Alpha docs",
                    source_type="docs",
                    score=0.8,
                ),
                SourceCandidate(
                    url="https://alpha.example/blog",
                    title="Alpha launch",
                    source_type="blog",
                    score=0.78,
                ),
            ],
            "Alpha reviews comparison alternatives user feedback": [
                SourceCandidate(
                    url="https://reviews.example/alpha",
                    title="Alpha reviews",
                    source_type="review_platform",
                    score=0.65,
                )
            ],
        }
    )

    candidates = await make_agent(tmp_path).discover(request, plan, search)  # type: ignore[arg-type]

    assert "Alpha reviews comparison alternatives user feedback" not in search.queries
    assert len(candidates) == 3
    assert "https://reviews.example/alpha" not in {candidate.url for candidate in candidates}


def make_agent(tmp_path: Path) -> SourceResearchAgent:
    settings = Settings(cg_data_dir=str(tmp_path))
    ctx = AgentContext(
        run_id="run_test",
        run_dir=tmp_path,
        settings=settings,
        llm=LLMClient(settings),
        trace=record_trace,
    )
    return SourceResearchAgent(ctx)


async def record_trace(
    node: str,
    phase: str,
    status: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    _ = (node, phase, status, message, payload)
