"""web-search skill — DuckDuckGo HTML scrape, no API key needed.

Params:
  query: str (required)
  max_results: int (default 5, max 20)

Returns:
  list of {title, url, snippet}
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from kun.skills.dispatcher import SkillResult, register

_DDG_URL = "https://html.duckduckgo.com/html/"
_TIMEOUT = 15.0
_UA = "KUN-Agent/0.1 (https://github.com/AShan0227/KUN; +contact: ashan0227)"

# DDG HTML uses <a class="result__a" href="...">title</a> + <a class="result__snippet">
_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    query = str(params.get("query") or "").strip()
    if not query:
        return SkillResult(skill_id="web-search", ok=False, error="query is required")

    max_results = max(1, min(20, int(params.get("max_results") or 5)))

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = await client.post(_DDG_URL, data={"q": query})
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        return SkillResult(
            skill_id="web-search",
            ok=False,
            error=f"http error: {e}",
            duration_sec=time.perf_counter() - started,
        )

    results: list[dict[str, str]] = []
    for match in _RESULT_RE.finditer(html):
        if len(results) >= max_results:
            break
        url, title, snippet = match.groups()
        results.append(
            {
                "title": _strip_html(title),
                "url": url,
                "snippet": _strip_html(snippet),
            }
        )

    return SkillResult(
        skill_id="web-search",
        ok=True,
        output=results,
        duration_sec=time.perf_counter() - started,
        metadata={"query": query, "result_count": len(results)},
    )


register("web-search", execute)
