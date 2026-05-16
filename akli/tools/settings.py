"""Tool ``computer_settings`` — управление системой (громкость, яркость, ...).

Раньше было 60+ отдельных функций по ~10 строк каждая. Здесь —
один dict-диспетчер: имя действия → callable. Добавить новое — одна
строка. Сейчас закрываем нужный минимум для MVP.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from typing import Callable

import pyautogui

from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger

_log = get_logger("tools.settings")


def _press(*keys: str) -> None:
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


def _press_n(key: str, n: int = 1) -> None:
    for _ in range(max(1, min(n, 50))):
        pyautogui.press(key)
        time.sleep(0.04)


def _lock_workstation() -> None:
    if sys.platform.startswith("win"):
        import ctypes
        ctypes.WinDLL("user32").LockWorkStation()


def _shutdown(arg: str) -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", arg, "/t", "0"])


# action_name → (callable, optional arg-handler)
_ACTIONS: dict[str, Callable[[dict], str]] = {
    "volume_up":     lambda p: (_press_n("volumeup", int(p.get("amount", 5))) or "Volume up."),
    "volume_down":   lambda p: (_press_n("volumedown", int(p.get("amount", 5))) or "Volume down."),
    "volume_mute":   lambda p: (_press("volumemute") or "Volume muted."),
    "play_pause":    lambda p: (_press("playpause") or "Toggled play/pause."),
    "next_track":    lambda p: (_press("nexttrack") or "Next track."),
    "prev_track":    lambda p: (_press("prevtrack") or "Previous track."),
    "screenshot":    lambda p: _take_screenshot(),
    "lock":          lambda p: (_lock_workstation() or "Workstation locked."),
    "shutdown":      lambda p: (_shutdown("/s") or "Shutting down."),
    "restart":       lambda p: (_shutdown("/r") or "Restarting."),
    "logout":        lambda p: (_shutdown("/l") or "Logging out."),
    "minimize_all":  lambda p: (pyautogui.hotkey("win", "d") or "Minimized."),
    "show_desktop":  lambda p: (pyautogui.hotkey("win", "d") or "Desktop shown."),
    "switch_window": lambda p: (pyautogui.hotkey("alt", "tab") or "Switched window."),
}


def _take_screenshot() -> str:
    from pathlib import Path
    target = Path.home() / "Pictures" / "Akli"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"screen_{int(time.time())}.png"
    pyautogui.screenshot(str(path))
    return f"Saved screenshot to {path}"


async def _run(params: dict, ctx: ToolContext) -> str:
    action = (params.get("action") or "").strip().lower()
    if not action:
        return "I need an action like volume_up, lock, screenshot."
    fn = _ACTIONS.get(action)
    if fn is None:
        return f"Unknown action '{action}'. Available: {', '.join(sorted(_ACTIONS))}."
    try:
        return await asyncio.to_thread(fn, params)
    except Exception as e:
        return f"Settings failed: {e}"


SPEC = make_spec(
    name        = "computer_settings",
    description = (
        "Controls system: volume, media, screenshots, lock/shutdown/restart, "
        "show desktop. Each action is one keyword."
    ),
    parameters  = {
        "action": {
            "type":        "string",
            "description": "Action keyword (e.g. 'volume_up', 'lock', 'screenshot').",
        },
        "amount": {
            "type":        "integer",
            "description": "Number of presses for repeatable actions (default 5).",
        },
    },
    required = ["action"],
    run      = _run,
)
