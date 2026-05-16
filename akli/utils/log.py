"""Простой структурный логгер.

Печатает строки вида ``HH:MM:SS  LEVEL  scope  message`` в stderr.
Уровень регулируется переменной окружения ``AKLI_LOG`` (по умолчанию INFO).
Опциональный дубль в файл — через ``AKLI_LOG_FILE``.

Нарочно без ``logging``-стандарта: меньше боли с конфигурацией, меньше
зависимостей. Глобальное состояние ограничено модулем.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

_lock = threading.Lock()
_min_level = _LEVELS.get(os.environ.get("AKLI_LOG", "INFO").upper(), 20)
_file: TextIO | None = None

_path = os.environ.get("AKLI_LOG_FILE")
if _path:
    try:
        _file = open(_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _file = None


def _emit(level: str, scope: str, message: str) -> None:
    if _LEVELS[level] < _min_level:
        return
    line = f"{time.strftime('%H:%M:%S')}  {level:<5}  {scope:<14}  {message}"
    with _lock:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass
        if _file is not None:
            try:
                _file.write(line + "\n")
            except Exception:
                pass


class Logger:
    """Лёгкая обёртка с привязанным ``scope`` (имя подсистемы)."""

    __slots__ = ("scope",)

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def debug(self, msg: str, *args: object) -> None:
        _emit("DEBUG", self.scope, msg % args if args else msg)

    def info(self, msg: str, *args: object) -> None:
        _emit("INFO", self.scope, msg % args if args else msg)

    def warn(self, msg: str, *args: object) -> None:
        _emit("WARN", self.scope, msg % args if args else msg)

    def error(self, msg: str, *args: object) -> None:
        _emit("ERROR", self.scope, msg % args if args else msg)


def get_logger(scope: str) -> Logger:
    return Logger(scope)
