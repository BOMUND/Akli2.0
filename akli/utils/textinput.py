"""Ввод произвольного текста в активное окно.

Раньше использовали ``pyautogui.write`` — это посылает виртуальные
scancode-ы по latin-mapping, поэтому на русской раскладке вместо
«Telegram» получалось «Еудупкфь» (см. баг ``B3``).

Стратегия:

1. Главный путь — clipboard paste (``pyperclip`` + ``Ctrl+V``). Работает
   для любого языка и Unicode, никаких raw keys.
2. На Windows есть запасной путь через ``SendInput`` с флагом
   ``KEYEVENTF_UNICODE`` — печатает Unicode-символы напрямую, в обход
   раскладки. Активируется только если буфер обмена недоступен.
3. Самый последний фолбэк — ``pyautogui.typewrite`` для ASCII текста.

Hotkey-комбинации (``Ctrl+C``, ``Win+D``) идут обычным ``pyautogui.hotkey`` —
там нет проблемы с раскладкой.
"""

from __future__ import annotations

import sys
import time
from typing import Iterable

import pyautogui

try:
    import pyperclip
    _HAS_CLIP = True
except ImportError:
    _HAS_CLIP = False

from akli.utils.log import get_logger

_log = get_logger("textinput")

_IS_WINDOWS = sys.platform.startswith("win")


def type_text(text: str, *, clear_first: bool = False) -> None:
    """Вставляет ``text`` в активное окно как пользовательский ввод."""
    if not text:
        return
    if clear_first:
        _clear_field()
        time.sleep(0.05)

    if _HAS_CLIP:
        try:
            pyperclip.copy(text)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
            return
        except Exception as e:
            _log.warn("clipboard paste failed: %s", e)

    if _IS_WINDOWS:
        if _send_input_unicode(text):
            return

    # Совсем последний фолбэк — это сработает только для ASCII.
    try:
        pyautogui.typewrite(text, interval=0.03)
    except Exception as e:
        _log.warn("typewrite fallback failed: %s", e)


def hotkey(*keys: str) -> None:
    pyautogui.hotkey(*keys)


def press(key: str) -> None:
    pyautogui.press(key)


def _clear_field() -> None:
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.03)
    pyautogui.press("delete")


# ────────────────────────────── Win-only низкоуровневый путь ──

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP    = 0x0002
    KEYEVENTF_UNICODE  = 0x0004

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk",         wintypes.WORD),
            ("wScan",       wintypes.WORD),
            ("dwFlags",     wintypes.DWORD),
            ("time",        wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUTunion(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [
            ("type",  wintypes.DWORD),
            ("union", _INPUTunion),
        ]

    def _send_input_unicode(text: str) -> bool:
        try:
            inputs: list[_INPUT] = []
            for ch in text:
                code = ord(ch)
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                    inp = _INPUT(
                        type  = INPUT_KEYBOARD,
                        union = _INPUTunion(ki=_KEYBDINPUT(
                            wVk         = 0,
                            wScan       = code,
                            dwFlags     = flags,
                            time        = 0,
                            dwExtraInfo = None,
                        )),
                    )
                    inputs.append(inp)
            arr = (_INPUT * len(inputs))(*inputs)
            sent = _user32.SendInput(
                len(inputs), arr, ctypes.sizeof(_INPUT),
            )
            return sent == len(inputs)
        except Exception as e:
            _log.warn("SendInput failed: %s", e)
            return False
else:
    def _send_input_unicode(text: str) -> bool:  # noqa: ARG001
        return False
