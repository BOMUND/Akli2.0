"""Диспетчер инструментов.

``Router`` живёт в единственном экземпляре. Принимает список ``ToolSpec``
при инициализации и отдаёт ``function_declarations`` для Gemini Live.
При вызове ``dispatch`` оборачивает выполнение тулзы в защитную обёртку:

* поднимает фазу ``TOOL`` в :class:`SpeakingState`;
* создаёт ``asyncio.Event`` для отмены и передаёт в :class:`ToolContext`;
* ловит ``CancelledError`` и любые исключения и **возвращает** их строкой —
  ``LiveSession`` не должен падать из-за плохой тулзы (Gemini-замечание).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Callable

from akli.core.config import AppConfig
from akli.live.state import SpeakingState
from akli.tools.base import ToolContext, ToolSpec
from akli.utils.log import get_logger

_log = get_logger("router")


class Router:
    def __init__(
        self,
        tools:  list[ToolSpec],
        state:  SpeakingState,
        config: AppConfig,
        log:    Callable[[str], None],
    ) -> None:
        self._tools = {t.name: t for t in tools}
        self._state = state
        self._config = config
        self._log = log

    def declarations(self) -> list[dict]:
        return [
            {
                "name":        t.name,
                "description": t.description,
                "parameters":  t.schema,
            }
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    async def dispatch(self, name: str, params: dict) -> dict:
        """Возвращает payload для ``FunctionResponse.response``."""
        spec = self._tools.get(name)
        if spec is None:
            _log.warn("unknown tool '%s' from model", name)
            return {"result": f"Unknown tool '{name}'."}

        loop = asyncio.get_running_loop()
        cancel = self._state.begin_tool(name, loop)
        ctx = ToolContext(
            state  = self._state,
            config = self._config,
            log    = self._log,
            cancel = cancel,
        )
        _log.info("→ %s  %s", name, _compact(params))

        # Запускаем тулзу как отдельную таску и параллельно ждём cancel-event.
        # Без этого STOP-кнопка не работает: голосовая отмена выставляет
        # ``cancel.set()``, но `await spec.run(...)` сам по себе ничего не
        # слушает и продолжает блокировать, пока тулза не вернёт результат.
        # На to_thread-вызовах (web_search, browser, files) это особенно
        # больно — поток в C-коде, asyncio.CancelledError туда не доходит.
        try:
            tool_task = asyncio.create_task(
                _maybe_await(spec.run(params, ctx)),
                name=f"tool[{name}]",
            )
            cancel_wait = asyncio.create_task(cancel.wait(), name=f"cancel[{name}]")
            done, pending = await asyncio.wait(
                {tool_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Уборка незавершённого. CancelledError здесь гасим жёстко,
            # чтобы он не вылетел наверх в LiveSession (см. предупреждение
            # Gemini об aiosyncio.CancelledError).
            if cancel_wait in pending:
                cancel_wait.cancel()
                try:
                    await cancel_wait
                except (asyncio.CancelledError, BaseException):
                    pass

            if cancel.is_set() and not tool_task.done():
                # Пользователь нажал STOP / попросил голосом. Возвращаем
                # модели результат немедленно, а сама тулза догорит в фоне
                # (если это блокирующий to_thread — отменить мы её всё
                # равно не сможем; зато модель уже не висит на ответе).
                tool_task.cancel()
                _log.warn("tool '%s' cancelled by user", name)
                return {"result": "Cancelled by user."}

            try:
                result = tool_task.result()
            except asyncio.CancelledError:
                _log.warn("tool '%s' cancelled", name)
                return {"result": "Tool was cancelled."}
            except Exception as e:
                _log.error("tool '%s' raised: %s", name, e)
                return {"result": f"Tool failed: {e}"}
        finally:
            self._state.end_tool()

        text = result if isinstance(result, str) else str(result)
        _log.info("✓ %s → %s", name, _compact(text)[:120])
        return {"result": text}


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _compact(obj: object) -> str:
    s = repr(obj) if not isinstance(obj, str) else obj
    return s[:200].replace("\n", " ")
