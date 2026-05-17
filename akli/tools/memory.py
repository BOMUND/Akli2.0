"""Tools for core memory and dialog-summary archive."""

from __future__ import annotations

import re

from akli.memory.store import MemoryStore
from akli.memory.summaries import DialogSummaryStore
from akli.tools.base import ToolContext, ToolSpec, make_spec

VALID_CATEGORIES = {"identity", "preferences", "notes"}
KEY_CHARS = re.compile(r"[^\wа-яА-ЯёЁ.-]+", re.UNICODE)


async def _list_summaries(params: dict, ctx: ToolContext) -> str:
    query = str(params.get("query") or "").strip()
    items = DialogSummaryStore().list(query=query, limit=0)
    if not items:
        return "No dialog summaries found."
    lines = []
    for item in items:
        lines.append(f"- {item.name} | {item.modified} | {item.title}")
    return "\n".join(lines)


async def _read_summary(params: dict, ctx: ToolContext) -> str:
    name = str(params.get("name") or "").strip()
    if not name:
        return "Summary file name is required."
    try:
        return DialogSummaryStore().read(name)
    except Exception as e:
        return f"Could not read summary: {e}"


async def _remember_core(params: dict, ctx: ToolContext) -> str:
    text = str(params.get("text") or "").strip()
    if not text:
        return "Nothing to remember."
    category = str(params.get("category") or "notes").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "notes"
    key = _clean_key(str(params.get("key") or ""))
    if not key:
        key = _clean_key(text[:40]) or "remembered_fact"
    store = ctx.memory or MemoryStore()
    store.update({category: {key: {"value": text}}})
    try:
        ctx.log(f"SYS: memory saved ({category}.{key})")
    except Exception:
        pass
    return f"Saved to core memory: {category}.{key}"


async def _forget_core(params: dict, ctx: ToolContext) -> str:
    """Удалить факт из core memory.

    Принимает явный ``key`` или свободный ``text`` (фраза
    пользователя, по которой ищем в ``value``). Опциональный
    ``category`` сужает поиск.
    """
    store = ctx.memory or MemoryStore()
    snapshot = store.as_dict()
    key = _clean_key(str(params.get("key") or ""))
    text = str(params.get("text") or "").strip().lower()
    category_param = str(params.get("category") or "").strip().lower()
    categories = [category_param] if category_param in VALID_CATEGORIES else list(VALID_CATEGORIES)

    removed: list[tuple[str, str]] = []
    patch: dict = {}
    for cat in categories:
        block = snapshot.get(cat) or {}
        if not isinstance(block, dict):
            continue
        for k, v in list(block.items()):
            if not isinstance(v, dict):
                continue
            value = str(v.get("value") or "")
            match = False
            if key and _clean_key(k) == key:
                match = True
            elif text and text in value.lower():
                match = True
            if match:
                patch.setdefault(cat, {})[k] = {"value": ""}
                removed.append((cat, k))

    if not removed:
        return (
            f"No matching fact found to forget (key={key!r}, text={text!r})."
        )

    store.update(patch)
    msg = ", ".join(f"{cat}.{k}" for cat, k in removed)
    try:
        ctx.log(f"SYS: memory forgot ({msg})")
    except Exception:
        pass
    return f"Forgot from core memory: {msg}"


def _clean_key(raw: str) -> str:
    key = KEY_CHARS.sub("_", raw.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_.-")
    return key[:48]


SPECS: list[ToolSpec] = [
    make_spec(
        "memory_list_summaries",
        "List saved dialog summary files by date/title. Use this when old conversation context may help.",
        {
            "query": {"type": "string", "description": "Optional keyword to filter summaries."},
        },
        _list_summaries,
    ),
    make_spec(
        "memory_read_summary",
        "Read one dialog summary file returned by memory_list_summaries.",
        {
            "name": {"type": "string", "description": "Exact file name from memory_list_summaries."},
        },
        _read_summary,
        required=["name"],
    ),
    make_spec(
        "memory_remember_core",
        "Save an important user fact to core memory only when the user explicitly asks to remember it.",
        {
            "text": {"type": "string", "description": "Stable fact to remember about the user."},
            "category": {"type": "string", "description": "identity, preferences, or notes."},
            "key": {"type": "string", "description": "Short stable key for this fact."},
        },
        _remember_core,
        required=["text"],
    ),
    make_spec(
        "memory_forget_core",
        "Remove a fact from core memory when the user explicitly asks to forget it. "
        "Pass either the exact key (preferred) or a text snippet that appears in the fact's value.",
        {
            "key": {"type": "string", "description": "Exact stable key of the fact to remove (preferred)."},
            "text": {"type": "string", "description": "Substring to match against fact values when key is unknown."},
            "category": {"type": "string", "description": "Optional: identity, preferences, or notes."},
        },
        _forget_core,
    ),
]
