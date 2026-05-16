"""Сборка всех ``ToolSpec`` в единый :class:`Router`."""

from __future__ import annotations

from typing import Callable

from akli.core.config import AppConfig
from akli.live.state import SpeakingState
from akli.memory.store import MemoryStore

from akli.tools.registry import Router

from akli.tools import apps as _apps
from akli.tools import keys as _keys
from akli.tools import scheduler as _scheduler
from akli.tools import files as _files
from akli.tools import web as _web
from akli.tools import browser as _browser
from akli.tools import settings as _settings
from akli.tools import memory as _memory
from akli.tools import stubs as _stubs


def build_router(
    state:  SpeakingState,
    config: AppConfig,
    log:    Callable[[str], None],
    memory: MemoryStore | None = None,
) -> Router:
    specs = [
        _apps.SPEC,
        *_keys.SPECS,
        *_scheduler.SPECS,
        _files.SPEC,
        _web.SPEC,
        _browser.SPEC,
        _settings.SPEC,
        *_memory.SPECS,
        *_stubs.SPECS,
    ]
    return Router(specs, state=state, config=config, log=log, memory=memory)


__all__ = ["build_router"]
