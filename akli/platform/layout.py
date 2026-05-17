"""Windows-helpers для переключения раскладки клавиатуры.

Используется только как страховочный fallback (см. :mod:`akli.tools.apps`).
Большая часть приложения должна работать через clipboard paste и
AppsFolder, чтобы вообще не зависеть от раскладки.
"""

from __future__ import annotations

import sys
from typing import Callable

from akli.utils.log import get_logger

_log = get_logger("layout")


def switch_to_en() -> Callable[[], None] | None:
    """Переключить раскладку на EN-US, вернуть колбэк восстановления.

    Возвращает ``None``, если что-то пошло не так — вызывающий код не
    должен этого ждать.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_INPUTLANGCHANGEREQUEST = 0x0050
        HWND_BROADCAST = 0xFFFF

        current_hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(current_hwnd, 0)
        original_kl = user32.GetKeyboardLayout(thread_id)

        en_kl = user32.LoadKeyboardLayoutW("00000409", 1)
        user32.PostMessageW(HWND_BROADCAST, WM_INPUTLANGCHANGEREQUEST, 0, en_kl)

        def restore() -> None:
            try:
                user32.PostMessageW(HWND_BROADCAST, WM_INPUTLANGCHANGEREQUEST, 0, original_kl)
            except Exception as e:
                _log.debug("layout restore failed: %s", e)

        return restore
    except Exception as e:
        _log.debug("switch_to_en failed: %s", e)
        return None
