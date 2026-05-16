"""DPI awareness для Windows.

Qt пытается выставить DPI awareness после старта, но если процесс уже
получил какой-то контекст от родителя — словит ``ERROR_ACCESS_DENIED``
(см. баг ``B4``: «SetProcessDpiAwarenessContext() failed: Отказано в
доступе»).

Поэтому **до** создания ``QApplication`` мы вызываем
``SetProcessDpiAwareness(2)`` сами. Qt тогда видит, что DPI уже задан,
и тихо использует существующий контекст.

На не-Windows функция ничего не делает.
"""

from __future__ import annotations

import os
import sys

from akli.utils.log import get_logger

_log = get_logger("dpi")


def ensure_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    try:
        import ctypes

        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        result = shcore.SetProcessDpiAwareness(2)
        # S_OK = 0, E_ACCESSDENIED = -2147024891 (уже выставлено — норм)
        if result not in (0, -2147024891):
            _log.warn("SetProcessDpiAwareness returned %d", result)
    except Exception as e:
        _log.debug("DPI awareness setup skipped: %s", e)
