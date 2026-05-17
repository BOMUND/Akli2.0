"""Базовые типы для системы инструментов.

Каждый инструмент — это объект с тремя полями:

* ``name`` — имя для модели (как в Gemini ``function_declaration``);
* ``schema`` — описание параметров (формат, совместимый с tools API);
* ``run`` — корутина, исполняющая действие.

Раньше тулзы вызывались через 11 ``elif`` в ``main.py`` + такую же
лестницу в ``agent/executor.py``. Здесь — единый ``Router`` с диктом.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from akli.core.config import AppConfig
from akli.live.state import SpeakingState
from akli.memory.store import MemoryStore
from akli.utils.log import get_logger


@dataclass
class ToolContext:
    """Всё, что нужно тулзе извне."""

    state:  SpeakingState
    config: AppConfig
    log:    Callable[[str], None]   # запись в UI activity-log
    cancel: asyncio.Event           # выставляется при таймауте или ``STOP``
    memory: MemoryStore | None = None

    def heartbeat(self, message: str | None = None) -> None:
        """Сбрасывает счётчик idle-таймаута. Звать из долгих операций.

        Опциональный ``message`` логируется в UI activity-log, чтобы
        пользователь видел, что именно сейчас делает тулза («ищу X»,
        «открываю Y», «жду ответа сервера»). Это сильно лучше пустого
        прогресс-бара на 20-секундной операции.
        """
        self.state.heartbeat()
        if message:
            try:
                self.log(message)
            except Exception:
                # Лог в UI не должен ронять тулзу.
                pass

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise asyncio.CancelledError("tool cancelled")


@dataclass
class ToolSpec:
    """Описание инструмента для регистрации."""

    name:        str
    description: str
    schema:      dict[str, object]   # JSON-schema для function_declaration
    run:         Callable[[dict, ToolContext], Awaitable[str]]
    response_modality: str = "audio"   # обычно "audio", но stub может быть "silent"


class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, params: dict, ctx: ToolContext) -> str: ...


def make_spec(
    name:        str,
    description: str,
    parameters:  dict[str, object],
    run:         Callable[[dict, ToolContext], Awaitable[str]],
    *,
    required:    list[str] | None = None,
) -> ToolSpec:
    """Сахар: формирует ``ToolSpec`` со standard JSON-schema."""
    schema = {
        "type":       "object",
        "properties": parameters,
        "required":   required or [],
    }
    return ToolSpec(
        name        = name,
        description = description,
        schema      = schema,
        run         = run,
    )
