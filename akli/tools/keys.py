"""Tool ``type_text`` и ``hotkey`` — ввод текста / горячие клавиши.

Печать произвольного текста идёт через clipboard paste (см.
:mod:`akli.utils.textinput`), чтобы любая раскладка отрабатывала
корректно (баг ``B3``, ``B14``).
"""

from __future__ import annotations

import asyncio

from akli.tools.base import ToolContext, make_spec
from akli.utils import textinput
from akli.utils.log import get_logger

_log = get_logger("tools.keys")


async def _type(params: dict, ctx: ToolContext) -> str:
    text = params.get("text") or ""
    clear = bool(params.get("clear_first", False))
    if not text:
        return "Nothing to type."
    await asyncio.to_thread(textinput.type_text, text, clear_first=clear)
    return f"Typed: {text[:80]}{'…' if len(text) > 80 else ''}"


async def _hotkey(params: dict, ctx: ToolContext) -> str:
    keys = params.get("keys") or ""
    if not keys:
        return "I need a key combo like 'ctrl+c'."
    parts = [k.strip().lower() for k in keys.replace(",", "+").split("+") if k.strip()]
    if not parts:
        return "Could not parse the hotkey."
    await asyncio.to_thread(textinput.hotkey, *parts)
    return f"Pressed {'+'.join(parts)}."


async def _press(params: dict, ctx: ToolContext) -> str:
    key = (params.get("key") or "").strip().lower()
    if not key:
        return "I need a key name."
    await asyncio.to_thread(textinput.press, key)
    return f"Pressed {key}."


SPECS = [
    make_spec(
        name        = "type_text",
        description = "Types arbitrary text into the focused window. Works with any language.",
        parameters  = {
            "text":        {"type": "string",  "description": "Text to type."},
            "clear_first": {"type": "boolean", "description": "If true, clears the field first."},
        },
        required = ["text"],
        run      = _type,
    ),
    make_spec(
        name        = "hotkey",
        description = "Presses a key combination such as 'ctrl+c' or 'alt+tab'.",
        parameters  = {
            "keys": {"type": "string", "description": "Combination like 'ctrl+shift+s'."},
        },
        required = ["keys"],
        run      = _hotkey,
    ),
    make_spec(
        name        = "press_key",
        description = "Presses a single key (Enter, Escape, F5, etc).",
        parameters  = {
            "key": {"type": "string", "description": "Key name."},
        },
        required = ["key"],
        run      = _press,
    ),
]
