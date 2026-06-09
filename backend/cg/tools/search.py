"""Search providers for discovering real public sources."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from cg.schemas.research import SourceCandidate
from cg.settings import Settings


def _parse_date(value: object) -> datetime | None:
    """Parse a date from ISO string or Unix timestamp; return None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[:25], fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


SearchProvider = Callable[[str, int], Awaitable[list[SourceCandidate]]]

PUBLIC_FALLBACK_PROVIDERS = {"duckduckgo"}
CONTENT_SEARCH_PROVIDERS = {"tavily", "exa", "zhihu_inner", "zhihu_global"}


class SearchTool:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def provider_names(self) -> list[str]:
        return configured_search_providers(self.settings)

    @property
    def active_provider_names(self) -> list[str]:
        return [
            provider
            for provider in self.provider_names
            if provider in PUBLIC_FALLBACK_PROVIDERS or self._provider_key(provider)
        ]

    async def search(self, query: str, max_results: int = 3) -> list[SourceCandidate]:
        """Search all configured providers and merge their unique results."""

        merged: list[SourceCandidate] = []
        providers = self.active_provider_names
        if self.settings.cg_search_use_all_providers:
            provider_batches = await asyncio.gather(
                *(self._search_provider(provider, query, max_results) for provider in providers),
                return_exceptions=True,
            )
            for provider_results in provider_batches:
                if isinstance(provider_results, list):
                    merged = merge_candidates(merged, provider_results)
            return sorted(merged, key=lambda item: item.score, reverse=True)[:max_results]

        for provider in providers:
            provider_results = await self._search_provider(provider, query, max_results)
            merged = merge_candidates(merged, provider_results)
            if len(merged) >= max_results and not self.settings.cg_search_use_all_providers:
                break
        return sorted(merged, key=lambda item: item.score, reverse=True)[:max_results]

    async def search_content_providers(
        self,
        query: str,
        max_results_per_provider: int = 5,
        providers: list[str] | None = None,
    ) -> dict[str, list[SourceCandidate]]:
        """Search each content-first provider independently.

        Unlike `search`, this does not merge or truncate across providers; each
        configured engine gets its own budget so the Search Agent can observe
        provider coverage and dedupe at the run level.
        """

        provider_names = providers or [
            provider
            for provider in self.active_provider_names
            if provider in CONTENT_SEARCH_PROVIDERS
        ]
        if not provider_names:
            provider_names = [
                provider
                for provider in ["tavily", "exa", "zhihu_inner", "zhihu_global"]
                if provider in self.active_provider_names
            ]
        max_results = max(1, min(max_results_per_provider, self.settings.cg_search_max_results_per_provider))
        batches = await asyncio.gather(
            *(self._search_provider(provider, query, max_results) for provider in provider_names),
            return_exceptions=True,
        )
        results: dict[str, list[SourceCandidate]] = {}
        for provider, batch in zip(provider_names, batches, strict=False):
            if isinstance(batch, list):
                for candidate in batch:
                    candidate.query = query
                    candidate.source_provider = candidate.source_provider or provider
                results[provider] = batch[:max_results]
            else:
                results[provider] = []
        return results

    async def search_many(self, queries: list[str], max_results: int = 3) -> list[SourceCandidate]:
        seen: set[str] = set()
        results: list[SourceCandidate] = []
        for query in queries:
            try:
                provider_timeout = max(8, self.settings.cg_search_provider_timeout_seconds)
                query_timeout = min(
                    self.settings.cg_http_timeout_seconds,
                    max(provider_timeout, len(self.active_provider_names) * provider_timeout),
                )
                query_results = await asyncio.wait_for(
                    self.search(query, max_results=max_results),
                    timeout=query_timeout,
                )
            except Exception:
                query_results = []
            for candidate in query_results:
                normalized = normalize_url(candidate.url)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    results.append(candidate)
            if len(results) >= max_results * 4:
                break
        return results

    async def search_zhihu(self, query: str, max_results: int = 8) -> list[SourceCandidate]:
        """知乎站内搜索 + 全网搜索，用于采集中文用户声音。"""
        if not self.settings.zhihu_api_key:
            return []
        results: list[SourceCandidate] = []
        try:
            zhihu_results = await asyncio.wait_for(
                self._search_zhihu_inner(query, min(max_results, 10)),
                timeout=self._timeout(),
            )
            results.extend(zhihu_results)
        except Exception:
            pass
        try:
            global_results = await asyncio.wait_for(
                self._search_zhihu_global(query, min(max_results, 10)),
                timeout=self._timeout(),
            )
            results = merge_candidates(results, global_results)
        except Exception:
            pass
        return sorted(results, key=lambda c: c.score, reverse=True)[:max_results]

    async def _search_zhihu_inner(self, query: str, count: int) -> list[SourceCandidate]:
        """知乎站内搜索 API。"""
        headers = {
            "Authorization": f"Bearer {self.settings.zhihu_api_key}",
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        }
        params = {"Query": query, "Count": min(count, 10)}
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.get(
                "https://developer.zhihu.com/api/v1/content/zhihu_search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        data = response.json()
        if data.get("Code") != 0:
            return []
        candidates: list[SourceCandidate] = []
        for item in (data.get("Data") or {}).get("Items") or []:
            url = as_str(item.get("Url"))
            if not url:
                continue
            authority = int(item.get("AuthorityLevel") or "1")
            # 权威等级：1低→0.48，2中→0.55，3高→0.65，4超高→0.72
            score = {1: 0.48, 2: 0.55, 3: 0.65, 4: 0.72}.get(authority, 0.50)
            title = as_str(item.get("Title"))
            snippet = as_str(item.get("ContentText"))
            author = as_str(item.get("AuthorName"))
            badge = as_str(item.get("AuthorBadgeText"))
            if badge:
                snippet = f"[{author}·{badge}] {snippet}"
            pub = _parse_date(item.get("EditTime"))
            candidates.append(SourceCandidate(
                url=url,
                title=title,
                snippet=snippet[:300],
                content=snippet,
                content_source="zhihu_inner_content",
                source_type="user_review",
                query=query,
                score=score,
                source_provider="zhihu_inner",
                published_at=pub,
                date_source="zhihu_edit_time" if pub else "unknown",
            ))
        return candidates

    async def _search_zhihu_global(self, query: str, count: int) -> list[SourceCandidate]:
        """知乎全网搜索 API（可过滤站点）。"""
        headers = {
            "Authorization": f"Bearer {self.settings.zhihu_api_key}",
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        }
        params = {"Query": query, "Count": min(count, 20), "SearchDB": "all"}
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.get(
                "https://developer.zhihu.com/api/v1/content/global_search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        data = response.json()
        if data.get("Code") != 0:
            return []
        candidates: list[SourceCandidate] = []
        for item in (data.get("Data") or {}).get("Items") or []:
            url = as_str(item.get("Url"))
            if not url:
                continue
            authority = int(item.get("AuthorityLevel") or "1")
            score = {1: 0.45, 2: 0.52, 3: 0.62, 4: 0.70}.get(authority, 0.48)
            snippet = as_str(item.get("ContentText")).replace("<em>", "").replace("</em>", "")
            pub = _parse_date(item.get("EditTime"))
            candidates.append(SourceCandidate(
                url=url,
                title=as_str(item.get("Title")),
                snippet=snippet[:300],
                content=snippet,
                content_source="zhihu_global_content",
                source_type="user_review",
                query=query,
                score=score,
                source_provider="zhihu_global",
                published_at=pub,
                date_source="zhihu_edit_time" if pub else "unknown",
            ))
        return candidates

    async def _search_provider(
        self, provider: str, query: str, max_results: int
    ) -> list[SourceCandidate]:
        searchers: dict[str, SearchProvider] = {
            "serper": self._search_serper,
            "tavily": self._search_tavily,
            "brave": self._search_brave,
            "serpapi": self._search_serpapi,
            "searchapi": self._search_searchapi,
            "bing": self._search_bing,
            "exa": self._search_exa,
            "searxng": self._search_searxng,
            "duckduckgo": self._search_duckduckgo,
            "zhihu_inner": self._search_zhihu_inner,
            "zhihu_global": self._search_zhihu_global,
        }
        searcher = searchers.get(provider)
        if not searcher:
            return []
        try:
            timeout = max(4, min(self.settings.cg_search_provider_timeout_seconds, self.settings.cg_http_timeout_seconds))
            return await asyncio.wait_for(searcher(query, max_results), timeout=timeout)
        except Exception:
            return []

    def _provider_key(self, provider: str) -> str:
        return {
            "serper": self.settings.serper_api_key,
            "tavily": self.settings.tavily_api_key,
            "brave": self.settings.brave_search_api_key,
            "serpapi": self.settings.serpapi_api_key,
            "searchapi": self.settings.searchapi_api_key,
            "bing": self.settings.bing_search_api_key,
            "exa": self.settings.exa_api_key,
            "searxng": self.settings.searxng_base_url,
            "duckduckgo": "public",
            "zhihu_inner": self.settings.zhihu_api_key,
            "zhihu_global": self.settings.zhihu_api_key,
        }.get(provider, "")

    async def _search_serper(self, query: str, max_results: int) -> list[SourceCandidate]:
        headers = {"X-API-KEY": self.settings.serper_api_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": max_results}
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        candidates: list[SourceCandidate] = []
        for item in data.get("organic", [])[:max_results]:
            link = as_str(item.get("link"))
            if not link:
                continue
            title = as_str(item.get("title"))
            snippet = as_str(item.get("snippet"))
            candidates.append(make_candidate(link, title, snippet, query, "serper", 0.78))
        return candidates

    async def _search_tavily(self, query: str, max_results: int) -> list[SourceCandidate]:
        headers = {
            "Authorization": f"Bearer {self.settings.tavily_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": self.settings.tavily_search_depth,
            "include_answer": False,
            "include_raw_content": True,
        }
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.post("https://api.tavily.com/search", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        candidates: list[SourceCandidate] = []
        for item in data.get("results", [])[:max_results]:
            link = as_str(item.get("url"))
            if not link:
                continue
            title = as_str(item.get("title"))
            snippet = as_str(item.get("content") or item.get("snippet"))
            raw_content = as_str(item.get("raw_content") or item.get("content") or item.get("snippet"))
            score = clamp_score(float_or_default(item.get("score"), 0.72), 0.62, 0.84)
            pub = _parse_date(item.get("published_date"))
            candidates.append(make_candidate(
                link,
                title,
                snippet,
                query,
                "tavily",
                score,
                content=raw_content,
                content_source="tavily_raw_content" if raw_content else "",
                published_at=pub,
                date_source="tavily" if pub else "unknown",
            ))
        return candidates

    async def _search_brave(self, query: str, max_results: int) -> list[SourceCandidate]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.settings.brave_search_api_key,
        }
        params = {"q": query, "count": max_results}
        async with httpx.AsyncClient(timeout=self._timeout(), headers=headers) as client:
            response = await client.get("https://api.search.brave.com/res/v1/web/search", params=params)
            response.raise_for_status()
        data = response.json()
        results = data.get("web", {}).get("results", [])
        candidates: list[SourceCandidate] = []
        for item in results[:max_results]:
            link = as_str(item.get("url"))
            if not link:
                continue
            candidates.append(
                make_candidate(
                    link,
                    as_str(item.get("title")),
                    as_str(item.get("description") or item.get("snippet")),
                    query,
                    "brave",
                    0.74,
                )
            )
        return candidates

    async def _search_serpapi(self, query: str, max_results: int) -> list[SourceCandidate]:
        params = {
            "engine": self.settings.serpapi_engine,
            "q": query,
            "api_key": self.settings.serpapi_api_key,
            "num": max_results,
        }
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.get("https://serpapi.com/search.json", params=params)
            response.raise_for_status()
        return serp_like_candidates(response.json(), query, "serpapi", max_results, 0.76)

    async def _search_searchapi(self, query: str, max_results: int) -> list[SourceCandidate]:
        params = {
            "engine": self.settings.searchapi_engine,
            "q": query,
            "api_key": self.settings.searchapi_api_key,
            "num": max_results,
        }
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.get("https://www.searchapi.io/api/v1/search", params=params)
            response.raise_for_status()
        return serp_like_candidates(response.json(), query, "searchapi", max_results, 0.75)

    async def _search_bing(self, query: str, max_results: int) -> list[SourceCandidate]:
        headers = {"Ocp-Apim-Subscription-Key": self.settings.bing_search_api_key}
        params = {
            "q": query,
            "count": max_results,
            "mkt": self.settings.bing_search_market,
            "responseFilter": "Webpages",
        }
        async with httpx.AsyncClient(timeout=self._timeout(), headers=headers) as client:
            response = await client.get("https://api.bing.microsoft.com/v7.0/search", params=params)
            response.raise_for_status()
        data = response.json()
        candidates: list[SourceCandidate] = []
        for item in data.get("webPages", {}).get("value", [])[:max_results]:
            link = as_str(item.get("url"))
            if not link:
                continue
            candidates.append(
                make_candidate(
                    link,
                    as_str(item.get("name")),
                    as_str(item.get("snippet")),
                    query,
                    "bing",
                    0.74,
                )
            )
        return candidates

    async def _search_exa(self, query: str, max_results: int) -> list[SourceCandidate]:
        headers = {"x-api-key": self.settings.exa_api_key, "Content-Type": "application/json"}
        payload = {
            "query": query,
            "numResults": max_results,
            "type": self.settings.exa_search_type,
            "contents": {"text": True},
        }
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.post("https://api.exa.ai/search", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        candidates: list[SourceCandidate] = []
        for item in data.get("results", [])[:max_results]:
            link = as_str(item.get("url"))
            if not link:
                continue
            text = as_str(item.get("text") or item.get("summary"))
            pub = _parse_date(item.get("publishedDate"))
            candidates.append(
                make_candidate(
                    link,
                    as_str(item.get("title")),
                    text[:500],
                    query,
                    "exa",
                    0.72,
                    content=text,
                    content_source="exa_text" if text else "",
                    published_at=pub,
                    date_source="exa" if pub else "unknown",
                )
            )
        return candidates

    async def _search_searxng(self, query: str, max_results: int) -> list[SourceCandidate]:
        base_url = self.settings.searxng_base_url.strip().rstrip("/")
        if not base_url:
            return []
        params = {
            "q": query,
            "format": "json",
            "categories": self.settings.searxng_categories,
            "language": self.settings.searxng_language,
            "pageno": 1,
        }
        async with httpx.AsyncClient(timeout=self._timeout(), headers=self._headers()) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
        data = response.json()
        candidates: list[SourceCandidate] = []
        for item in data.get("results", [])[:max_results]:
            link = as_str(item.get("url"))
            if not link:
                continue
            candidates.append(
                make_candidate(
                    link,
                    as_str(item.get("title")),
                    as_str(item.get("content")),
                    query,
                    "searxng",
                    0.66,
                )
            )
        return candidates

    async def _search_duckduckgo(self, query: str, max_results: int) -> list[SourceCandidate]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            async with httpx.AsyncClient(
                timeout=min(8, self.settings.cg_http_timeout_seconds),
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception:
            return []

        tree = HTMLParser(response.text)
        candidates: list[SourceCandidate] = []
        for node in tree.css("a.result__a"):
            href = unwrap_duckduckgo_url(node.attributes.get("href", ""))
            if not href:
                continue
            candidates.append(
                make_candidate(
                    href,
                    node.text(strip=True),
                    "",
                    query,
                    "duckduckgo",
                    0.62,
                )
            )
            if len(candidates) >= max_results:
                break
        return candidates

    def _timeout(self) -> int:
        return max(4, min(self.settings.cg_search_provider_timeout_seconds, self.settings.cg_http_timeout_seconds))

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.settings.cg_user_agent}


def configured_search_providers(settings: Settings) -> list[str]:
    raw = settings.cg_search_providers.strip()
    if raw:
        providers = [provider.strip().lower() for provider in raw.split(",") if provider.strip()]
    else:
        providers = [
            "tavily",
            "exa",
            "zhihu_inner",
            "zhihu_global",
        ]
    seen: set[str] = set()
    ordered: list[str] = []
    for provider in providers:
        if provider not in seen:
            seen.add(provider)
            ordered.append(provider)
    return ordered


def serp_like_candidates(
    data: dict,
    query: str,
    provider: str,
    max_results: int,
    score: float,
) -> list[SourceCandidate]:
    organic = data.get("organic_results") or data.get("organic") or []
    candidates: list[SourceCandidate] = []
    for item in organic[:max_results]:
        link = as_str(item.get("link") or item.get("url"))
        if not link:
            continue
        candidates.append(
            make_candidate(
                link,
                as_str(item.get("title")),
                as_str(item.get("snippet") or item.get("description")),
                query,
                provider,
                score,
            )
        )
    return candidates


def make_candidate(
    url: str,
    title: str,
    snippet: str,
    query: str,
    provider: str,
    score: float,
    *,
    content: str = "",
    content_source: str = "",
    published_at: datetime | None = None,
    date_source: str = "unknown",
) -> SourceCandidate:
    return SourceCandidate(
        url=url,
        title=title,
        snippet=snippet,
        content=content,
        content_source=content_source,
        source_type=classify_source(url, title),
        query=query,
        score=score,
        source_provider=provider,
        published_at=published_at,
        date_source=date_source,
    )


def merge_candidates(
    existing: list[SourceCandidate],
    incoming: list[SourceCandidate],
) -> list[SourceCandidate]:
    by_url: dict[str, SourceCandidate] = {}
    for candidate in [*existing, *incoming]:
        normalized = normalize_url(candidate.url)
        if not normalized:
            continue
        candidate.url = normalized
        previous = by_url.get(normalized)
        if not previous:
            by_url[normalized] = candidate
            continue
        by_url[normalized] = merge_candidate(previous, candidate)
    return list(by_url.values())


def merge_candidate(first: SourceCandidate, second: SourceCandidate) -> SourceCandidate:
    primary, secondary = (first, second) if first.score >= second.score else (second, first)
    provider_names = [
        item
        for item in [first.source_provider, second.source_provider]
        if item
    ]
    provider = ",".join(dict.fromkeys(provider_names))
    return SourceCandidate(
        url=primary.url,
        title=primary.title or secondary.title,
        snippet=primary.snippet or secondary.snippet,
        content=primary.content or secondary.content,
        content_source=primary.content_source or secondary.content_source,
        source_type=primary.source_type if primary.source_type != "other" else secondary.source_type,
        query=primary.query or secondary.query,
        score=round(min(1.0, max(first.score, second.score) + 0.02), 3),
        source_provider=provider,
    )


def unwrap_duckduckgo_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(uddg)
    if parsed.scheme in {"http", "https"}:
        return href
    return ""


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed._replace(fragment="").geturl().rstrip("/")


def classify_source(url: str, title: str = "") -> str:
    text = f"{url} {title}".lower()
    if "zhihu.com" in text or "知乎" in title:
        return "user_review"
    if "pricing" in text or "plans" in text or "billing" in text:
        return "pricing_page"
    if "docs" in text or "documentation" in text or "help" in text:
        return "docs"
    if "changelog" in text or "release" in text or "updates" in text:
        return "changelog"
    if "github.com" in text:
        return "github"
    if "review" in text or "g2.com" in text or "producthunt" in text:
        return "review_platform"
    if "blog" in text or "news" in text:
        return "blog"
    if "enterprise" in text or "security" in text:
        return "official_website"
    return "official_website" if is_likely_official(url) else "other"


def is_likely_official(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return bool(host and not any(site in host for site in ["google", "bing", "duckduckgo"]))


def as_str(value: object) -> str:
    return str(value or "").strip()


def float_or_default(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp_score(value: float, low: float, high: float) -> float:
    return round(max(low, min(high, value)), 3)
