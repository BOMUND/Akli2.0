"""Tool ``open_app`` — открыть приложение по имени.

Никаких ``Win + pyautogui.write(...)`` (баг ``B3``). Алгоритм:

1. Системные алиасы (notepad, calc, cmd, explorer …) → ``subprocess.Popen``.
2. Поиск AUMID через :class:`AppsFolder` → ``explorer shell:AppsFolder\\<AUMID>``.
3. Если не нашли — переключаем раскладку на EN-US, делаем медленный
   Win+typewrite по ASCII-имени, возвращаем раскладку. Это только для
   очень редких случаев, когда приложение не зарегистрировано в Get-StartApps.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pyautogui

from akli.platform.appsfolder import AppsFolder
from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger

_log = get_logger("tools.apps")
_apps = AppsFolder()

# Базовые системные приложения. Имя → исполняемый файл.
_BUILTINS: dict[str, str] = {
    "notepad":            "notepad.exe",
    "блокнот":            "notepad.exe",
    "calculator":         "calc.exe",
    "калькулятор":        "calc.exe",
    "calc":               "calc.exe",
    "cmd":                "cmd.exe",
    "командная строка":   "cmd.exe",
    "powershell":         "powershell.exe",
    "explorer":           "explorer.exe",
    "проводник":          "explorer.exe",
    "paint":              "mspaint.exe",
    "task manager":       "taskmgr.exe",
    "диспетчер задач":    "taskmgr.exe",
    "control panel":      "control.exe",
    "панель управления":  "control.exe",
}


async def _run(params: dict, ctx: ToolContext) -> str:
    name = (params.get("app_name") or "").strip()
    if not name:
        return "I need an application name to open."
    return await asyncio.to_thread(_launch, name, ctx)


def _launch(name: str, ctx: ToolContext) -> str:
    ctx.heartbeat()
    lowered = name.lower()

    # 1. Системные бинарники
    if lowered in _BUILTINS:
        exe = _BUILTINS[lowered]
        if _spawn(exe):
            return f"Opened {name}."
        # если не нашёл — едем дальше

    if sys.platform.startswith("win"):
        # 2. AUMID lookup
        hit = _apps.find(name)
        if hit:
            found_name, aumid = hit
            if _spawn_aumid(aumid):
                _log.info("opened via AUMID: %s (%s)", found_name, aumid)
                return f"Opened {found_name}."

        # 3. Последний шанс: PATH lookup
        path = shutil.which(name) or shutil.which(name + ".exe")
        if path and _spawn(path):
            return f"Opened {name}."

        # 4. Совсем плохо — fallback на Win+search, но через EN-раскладку.
        return _legacy_win_search(name)

    # mac/linux — простая попытка через PATH или `open`
    path = shutil.which(name) or shutil.which(name.lower())
    if path and _spawn(path):
        return f"Opened {name}."
    return f"Could not find '{name}' on this system."


def _spawn(executable: str) -> bool:
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(
                [executable],
                shell  = False,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            | getattr(subprocess, "DETACHED_PROCESS", 0),
                stdout = subprocess.DEVNULL,
                stderr = subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", executable])
        else:
            subprocess.Popen([executable])
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        _log.warn("spawn failed for %s: %s", executable, e)
        return False


def _spawn_aumid(aumid: str) -> bool:
    try:
        subprocess.Popen(
            ["explorer.exe", f"shell:AppsFolder\\{aumid}"],
            shell  = False,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        _log.warn("AUMID launch failed (%s): %s", aumid, e)
        return False


def _legacy_win_search(name: str) -> str:
    """Очень последний фолбэк. Не работает для не-ASCII имён."""
    if not name.isascii():
        return (
            f"Could not locate '{name}' in installed apps. "
            f"Refresh the app cache or install the application."
        )
    try:
        from akli.platform import layout as _layout
    except ImportError:
        _layout = None

    restore = None
    if _layout is not None:
        restore = _layout.switch_to_en()

    try:
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.5)
        pyautogui.typewrite(name, interval=0.05)
        time.sleep(0.6)
        pyautogui.press("enter")
        return f"Opened {name} via Start search."
    except Exception as e:
        return f"Could not open '{name}': {e}"
    finally:
        if restore is not None:
            restore()


SPEC = make_spec(
    name        = "open_app",
    description = "Opens an installed application by display name (any language).",
    parameters  = {
        "app_name": {
            "type": "string",
            "description": "Application name as the user pronounces it (e.g. 'Telegram', 'Калькулятор').",
        },
    },
    required = ["app_name"],
    run      = _run,
)
