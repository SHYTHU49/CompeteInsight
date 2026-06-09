"""Real HTTP fetching and content extraction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import trafilatura
from selectolax.parser import HTMLParser

from cg.settings import Settings


@dataclass(slots=True)
class RawPage:
    url: str
    final_url: str
    title: str
    content: str
    html: str
    content_hash: str
    fetched_at: datetime
    http_status: int | None
    parser: str
    ok: bool
    error: str | None = None


class Fetcher:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch(self, url: str) -> RawPage:
        headers = {"User-Agent": self.settings.cg_user_agent}
        timeout = httpx.Timeout(self.settings.cg_http_timeout_seconds)
        proxy = self.settings.http_proxy or None
        fetched_at = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers=headers,
                timeout=timeout,
                proxy=proxy,
            ) as client:
                response = await client.get(url)
            html = decode_response(response)
            title = extract_title(html) or response.url.host or url
            content = extract_content(html, str(response.url))
            content_hash = hashlib.sha256((content or html).encode("utf-8", errors="ignore")).hexdigest()
            return RawPage(
                url=url,
                final_url=str(response.url),
                title=title[:240],
                content=content,
                html=html,
                content_hash=content_hash,
                fetched_at=fetched_at,
                http_status=response.status_code,
                parser="trafilatura+selectolax",
                ok=response.status_code < 400 and bool(content.strip()),
                error=None if response.status_code < 400 else f"HTTP {response.status_code}",
            )
        except Exception as exc:
            return RawPage(
                url=url,
                final_url=url,
                title=url,
                content="",
                html="",
                content_hash="",
                fetched_at=fetched_at,
                http_status=None,
                parser="trafilatura+selectolax",
                ok=False,
                error=str(exc),
            )


def extract_title(html: str) -> str:
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        title = tree.css_first("title")
        if title:
            return normalize_space(title.text())
        heading = tree.css_first("h1")
        if heading:
            return normalize_space(heading.text())
    except Exception:
        return ""
    return ""


def decode_response(response: httpx.Response) -> str:
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.text


def extract_content(html: str, url: str) -> str:
    if not html:
        return ""
    extracted = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if extracted and len(extracted.strip()) > 120:
        return normalize_space(extracted)
    try:
        tree = HTMLParser(html)
        parts = [node.text(separator=" ", strip=True) for node in tree.css("main, article, h1, h2, p, li")]
        content = "\n".join(part for part in parts if len(part) > 30)
        return normalize_space(content)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        return normalize_space(text)


def normalize_space(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()
