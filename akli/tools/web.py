"""Tool ``web_search`` — короткие фактические ответы из веба.

Бывший вариант лез сначала в OpenRouter (медленно и часто падал), потом
в DuckDuckGo. Здесь: сразу DuckDuckGo (мгновенный snippet-результат),
с timeout 12 сек на запрос. Если DDG лежит — пробуем Gemini grounded
search через ``google.genai``.
"""

from __future__ import annotations

import asyncio
from typing import Final

from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger

_log = get_logger("tools.web")

DDG_RESULT_LIMIT: Final = 5
DDG_TIMEOUT_SEC:  Final = 12


async def _run(params: dict, ctx: ToolContext) -> str:
    query = (params.get("query") or "").strip()
    if not query:
        return "I need a search query."

    try:
        snippets = await asyncio.wait_for(
            asyncio.to_thread(_ddg_search, query),
            timeout=DDG_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        snippets = []
    except Exception as e:
        _log.warn("ddg failed: %s", e)
        snippets = []

    if snippets:
        return _format(query, snippets)

    # Запасной путь — grounded Gemini.
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_gemini_grounded, query, ctx.config.gemini_api_key),
            timeout=20,
        )
        if text:
            return text
    except asyncio.TimeoutError:
        return "Search timed out — try a different query."
    except Exception as e:
        _log.warn("gemini grounded failed: %s", e)

    return "Could not retrieve search results right now."


def _ddg_search(query: str) -> list[dict]:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return []
    with DDGS() as ddg:
        return list(ddg.text(query, max_results=DDG_RESULT_LIMIT)) or []


def _gemini_grounded(query: str, api_key: str) -> str:
    if not api_key:
        return ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        resp = client.models.generate_content(
            model    = "gemini-2.5-flash",
            contents = query,
            config   = config,
        )
        return (resp.text or "").strip()
    except Exception as e:
        _log.warn("gemini grounded inner: %s", e)
        return ""


def _format(query: str, items: list[dict]) -> str:
    lines = [f"Search results for '{query}':"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "").strip()
        body  = item.get("body", "").strip()
        href  = item.get("href", "").strip()
        snippet = body[:240] + ("…" if len(body) > 240 else "")
        lines.append(f"{i}. {title}\n   {snippet}\n   {href}")
    return "\n".join(lines)


SPEC = make_spec(
    name        = "web_search",
    description = "Quick factual web search (top snippets). Use for current events, prices, definitions.",
    parameters  = {
        "query": {"type": "string", "description": "What to search for."},
    },
    required = ["query"],
    run      = _run,
)
