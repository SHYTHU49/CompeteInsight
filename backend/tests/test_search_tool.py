"""SearchTool provider aggregation behavior."""

from __future__ import annotations

import httpx
import pytest

from cg.settings import Settings
from cg.tools.search import SearchTool, configured_search_providers, merge_candidates
from cg.schemas.research import SourceCandidate


@pytest.mark.asyncio
async def test_search_merges_configured_providers_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        serper_api_key="serper-key",
        tavily_api_key="tavily-key",
        cg_search_providers="serper,tavily,duckduckgo",
    )
    search = SearchTool(settings)

    async def fake_serper(query: str, max_results: int) -> list[SourceCandidate]:
        return [
            SourceCandidate(
                url="https://alpha.example/product#overview",
                title="Alpha product",
                snippet="Official product page",
                source_provider="serper",
                query=query,
                score=0.78,
            )
        ][:max_results]

    async def fake_tavily(query: str, max_results: int) -> list[SourceCandidate]:
        return [
            SourceCandidate(
                url="https://alpha.example/product",
                title="Alpha",
                snippet="Richer Tavily snippet",
                source_provider="tavily",
                query=query,
                score=0.8,
            ),
            SourceCandidate(
                url="https://reviews.example/alpha",
                title="Alpha reviews",
                snippet="Third-party feedback",
                source_provider="tavily",
                query=query,
                score=0.72,
            ),
        ][:max_results]

    async def fake_duckduckgo(query: str, max_results: int) -> list[SourceCandidate]:
        return [
            SourceCandidate(
                url="https://community.example/alpha",
                title="Alpha community",
                source_provider="duckduckgo",
                query=query,
                score=0.62,
            )
        ][:max_results]

    monkeypatch.setattr(search, "_search_serper", fake_serper)
    monkeypatch.setattr(search, "_search_tavily", fake_tavily)
    monkeypatch.setattr(search, "_search_duckduckgo", fake_duckduckgo)

    results = await search.search("Alpha product reviews", max_results=4)

    assert [item.url for item in results] == [
        "https://alpha.example/product",
        "https://reviews.example/alpha",
        "https://community.example/alpha",
    ]
    assert results[0].source_provider == "serper,tavily"
    assert results[0].score > 0.8


@pytest.mark.asyncio
async def test_search_continues_when_provider_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        serper_api_key="serper-key",
        tavily_api_key="tavily-key",
        cg_search_providers="serper,tavily",
    )
    search = SearchTool(settings)

    async def broken_serper(query: str, max_results: int) -> list[SourceCandidate]:
        raise httpx.ConnectError("boom")

    async def fake_tavily(query: str, max_results: int) -> list[SourceCandidate]:
        return [
            SourceCandidate(
                url="https://alpha.example/docs",
                title="Alpha docs",
                source_provider="tavily",
                query=query,
                score=0.76,
            )
        ][:max_results]

    monkeypatch.setattr(search, "_search_serper", broken_serper)
    monkeypatch.setattr(search, "_search_tavily", fake_tavily)

    results = await search.search("Alpha docs", max_results=3)

    assert len(results) == 1
    assert results[0].url == "https://alpha.example/docs"
    assert results[0].source_provider == "tavily"


def test_configured_search_providers_preserves_order_and_dedupes() -> None:
    settings = Settings(cg_search_providers="tavily,serper,tavily,duckduckgo")

    assert configured_search_providers(settings) == ["tavily", "serper", "duckduckgo"]


def test_merge_candidates_keeps_best_score_and_provider_lineage() -> None:
    merged = merge_candidates(
        [
            SourceCandidate(
                url="https://alpha.example/pricing#faq",
                title="Alpha pricing",
                source_provider="serper",
                score=0.75,
            )
        ],
        [
            SourceCandidate(
                url="https://alpha.example/pricing",
                title="Alpha plans",
                snippet="Plans and billing",
                source_provider="brave",
                score=0.77,
            )
        ],
    )

    assert len(merged) == 1
    assert merged[0].url == "https://alpha.example/pricing"
    assert merged[0].source_provider == "serper,brave"
    assert merged[0].snippet == "Plans and billing"
