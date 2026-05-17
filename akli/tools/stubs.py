"""Заглушки для будущих инструментов.

``file_processor`` и ``dev_agent`` остались в декларациях, чтобы модель
знала об их существовании, но реализации пока нет — возвращают
понятное сообщение. Когда придёт время — здесь меняется только тело
``run``-функций.
"""

from __future__ import annotations

from akli.tools.base import ToolContext, make_spec

_UNAVAILABLE = (
    "This tool is temporarily unavailable in the current build. "
    "I cannot complete the request."
)


async def _file_processor(params: dict, ctx: ToolContext) -> str:
    return _UNAVAILABLE


async def _dev_agent(params: dict, ctx: ToolContext) -> str:
    return _UNAVAILABLE


SPECS = [
    make_spec(
        name        = "file_processor",
        description = "Heavy file processing tasks (transcription, OCR, etc). Currently disabled.",
        parameters  = {
            "operation": {"type": "string", "description": "Operation kind."},
            "path":      {"type": "string", "description": "Target file path."},
        },
        required = ["operation", "path"],
        run      = _file_processor,
    ),
    make_spec(
        name        = "dev_agent",
        description = "Autonomous developer agent (planning, coding). Currently disabled.",
        parameters  = {
            "task":        {"type": "string", "description": "Top-level task description."},
            "project_dir": {"type": "string", "description": "Working directory."},
        },
        required = ["task"],
        run      = _dev_agent,
    ),
]
