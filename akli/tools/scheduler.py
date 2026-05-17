"""Tool ``remind`` — поставить напоминание.

Заменяет старый ``actions/reminder.py``, который требовал ``schtasks``,
XML-файл и UAC-права. Здесь — обычный ``threading.Timer``, состояние в
``state/reminders.json``. Парсинг времени — детерминированный Python
(см. :mod:`akli.utils.timeparse`), а не LLM (фикс ``B5``).

Если запрошенное время уже в прошлом ≤ 60 сек — автокоррекция на
``now + 60s``, а не ошибка.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from akli.core.config import REMINDERS_FILE
from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger
from akli.utils.timeparse import TimeParseError, parse as parse_time

_log = get_logger("scheduler")


@dataclass
class Reminder:
    id:      str
    when:    str   # ISO 8601
    message: str

    def datetime_when(self) -> datetime:
        return datetime.fromisoformat(self.when)


class Scheduler:
    def __init__(
        self,
        path:   Path | None = None,
        notify: Callable[[Reminder], None] | None = None,
    ) -> None:
        self._path  = path or REMINDERS_FILE
        self._notify = notify or _default_notify
        self._timers: dict[str, threading.Timer] = {}
        self._lock  = threading.Lock()

    # ─────────────────────────────── persistence ──

    def _load(self) -> list[Reminder]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            out: list[Reminder] = []
            for r in raw.get("items", []):
                try:
                    out.append(Reminder(**r))
                except Exception:
                    continue
            return out
        except Exception as e:
            _log.warn("reminders.json corrupt: %s", e)
            return []

    def _save(self, items: list[Reminder]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"items": [asdict(r) for r in items]},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # ─────────────────────────────── public API ──

    def restore(self) -> int:
        """Поднимает все ранее сохранённые напоминалки на старте приложения.

        Если приложение упало / было убито между set-ом напоминалки и её
        временем срабатывания — на следующем старте мы это видим. Раньше
        всё, что просрочено больше чем на 5 сек, тихо выкидывалось — и
        пользователь не знал, что что-то было запланировано. Теперь:

        * напоминания из будущего → нормально планируем дальше;
        * напоминания из ближайшего прошлого (≤ 5 минут) → стреляем сразу
          с пометкой "(missed)", чтобы пользователь увидел, что было;
        * напоминания, которые просрочены сильно (например, ноутбук
          провёл неделю в спячке) → тоже сообщаем одним пакетом, но
          без шума на каждое.
        """
        items = self._load()
        now = datetime.now()
        survivors: list[Reminder] = []
        very_late: list[Reminder] = []
        late_threshold = timedelta(minutes=5)
        with self._lock:
            for r in items:
                try:
                    dt = r.datetime_when()
                except ValueError:
                    continue
                if dt > now:
                    self._schedule_locked(r, (dt - now).total_seconds())
                    survivors.append(r)
                    continue
                overdue = now - dt
                if overdue <= late_threshold:
                    # Стреляем сразу, не сохраняем в survivors.
                    self._fire_now(r, missed=True)
                else:
                    very_late.append(r)
            if very_late:
                _log.warn("scheduler: %d просроченных напоминаний (>5 мин), "
                          "стреляю одним сводным сообщением", len(very_late))
                joined = "; ".join(r.message or "?" for r in very_late[:5])
                if len(very_late) > 5:
                    joined += f"; … (ещё {len(very_late) - 5})"
                self._fire_now(
                    Reminder(
                        id      = f"missed-{int(now.timestamp())}",
                        when    = now.isoformat(timespec="seconds"),
                        message = f"Missed reminders: {joined}",
                    ),
                    missed=True,
                )
            self._save(survivors)
        return len(survivors)

    def _fire_now(self, r: Reminder, *, missed: bool = False) -> None:
        """Немедленно отправить нотификацию (для просроченных при restore)."""
        try:
            r_for_fire = r
            if missed and r.message and not r.message.startswith("(missed)") \
                    and not r.message.startswith("Missed reminders:"):
                r_for_fire = Reminder(
                    id      = r.id,
                    when    = r.when,
                    message = f"(missed) {r.message}",
                )
            self._notify(r_for_fire)
        except Exception as e:
            _log.warn("missed-reminder notify failed: %s", e)

    def add(self, when_text: str, message: str) -> Reminder:
        now = datetime.now()
        target = parse_time(when_text, now=now)

        # Гарантия: до срабатывания ≥ 1 сек, чтобы не падать на «уже прошло».
        delta = (target - now).total_seconds()
        if delta < 1.0:
            target = now + timedelta(seconds=60)
            delta = 60.0

        r = Reminder(
            id      = uuid.uuid4().hex[:8],
            when    = target.isoformat(timespec="seconds"),
            message = message,
        )
        with self._lock:
            items = self._load()
            items.append(r)
            self._save(items)
            self._schedule_locked(r, delta)
        return r

    def list(self) -> list[Reminder]:
        return self._load()

    def cancel(self, rid: str) -> bool:
        with self._lock:
            timer = self._timers.pop(rid, None)
            if timer:
                timer.cancel()
            items = [r for r in self._load() if r.id != rid]
            self._save(items)
            return timer is not None

    # ─────────────────────────────── internals ──

    def _schedule_locked(self, r: Reminder, delay_sec: float) -> None:
        delay_sec = max(1.0, delay_sec)
        timer = threading.Timer(delay_sec, self._fire, args=(r.id,))
        timer.daemon = True
        timer.start()
        self._timers[r.id] = timer

    def _fire(self, rid: str) -> None:
        with self._lock:
            self._timers.pop(rid, None)
            items = self._load()
            target = next((r for r in items if r.id == rid), None)
            if target is None:
                return
            survivors = [r for r in items if r.id != rid]
            try:
                self._save(survivors)
            except Exception as e:
                _log.warn("reminder save after fire failed: %s", e)
        try:
            self._notify(target)
        except Exception as e:
            _log.warn("notify failed: %s", e)


# ─────────────────────────── default notification ──

def _show_windows_messagebox(msg: str) -> None:
    """MessageBoxW в отдельном потоке: окно модальное и блокирует поток-
    создатель до клика. Если scheduler-таск запустит его синхронно — все
    следующие напоминания зависнут в очереди до тех пор, пока пользователь
    не дотронется до окна. Поэтому всегда в новом thread."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Akli reminder", 0x40 | 0x1000)
    except Exception as e:
        _log.warn("Windows reminder popup failed: %s", e)


def _osascript_escape(text: str) -> str:
    # AppleScript принимает только двойные кавычки внутри строк, обратный
    # слэш — escape-метасимвол. Экранируем оба, иначе при сообщении с
    # цитатами/слэшами osascript ругается и тост не показывается.
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _default_notify(r: Reminder) -> None:
    msg = r.message or "Reminder"
    _log.info("REMINDER FIRED: %s", msg)
    if sys.platform.startswith("win"):
        try:
            import winsound
            winsound.Beep(880, 200)
            winsound.Beep(660, 250)
        except Exception as e:
            _log.debug("winsound reminder beep failed: %s", e)
        toast_sent = False
        try:
            from win10toast import ToastNotifier  # type: ignore
            ToastNotifier().show_toast("Akli", msg, duration=8, threaded=True)
            toast_sent = True
        except Exception as e:
            _log.debug("win10toast reminder failed: %s", e)
        try:
            threading.Thread(
                target = _show_windows_messagebox,
                args   = (msg,),
                daemon = True,
                name   = "AkliReminderBox",
            ).start()
            return
        except Exception as e:
            if toast_sent:
                return
            _log.warn("Windows reminder popup failed: %s", e)
    elif sys.platform == "darwin":
        try:
            import subprocess
            escaped = _osascript_escape(msg)
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{escaped}" with title "Akli"'])
            return
        except Exception as e:
            _log.debug("macOS notification failed: %s", e)
    else:
        try:
            import subprocess
            subprocess.Popen(["notify-send", "Akli", msg])
            return
        except Exception as e:
            _log.debug("notify-send failed: %s", e)


# ─────────────────────────── tool spec ──

_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
        try:
            count = _scheduler.restore()
            if count:
                _log.info("restored %d pending reminders", count)
        except Exception as e:
            _log.warn("restore failed: %s", e)
    return _scheduler


async def _remind(params: dict, ctx: ToolContext) -> str:
    when_text = (params.get("when") or "").strip()
    message   = (params.get("message") or "").strip() or "Reminder"
    if not when_text:
        return "I need a time for the reminder."
    ctx.heartbeat(f"reminder: «{message[:40]}» в «{when_text}»")
    try:
        r = await asyncio.to_thread(get_scheduler().add, when_text, message)
    except TimeParseError as e:
        return f"I could not understand the time '{when_text}'."
    except Exception as e:
        return f"Could not set reminder: {e}"
    dt = r.datetime_when()
    return f"Reminder set for {dt:%H:%M, %A %d %B} — {message}"


async def _list(params: dict, ctx: ToolContext) -> str:
    items = await asyncio.to_thread(get_scheduler().list)
    if not items:
        return "No pending reminders."
    lines = [f"- {r.datetime_when():%H:%M, %d %b}: {r.message}" for r in items]
    return "Pending reminders:\n" + "\n".join(lines)


SPECS = [
    make_spec(
        name        = "remind",
        description = (
            "Sets a reminder. The 'when' field accepts natural language like "
            "'in 5 minutes', 'через 1 минуту', 'tomorrow at 9am', '15:30'."
        ),
        parameters  = {
            "when":    {"type": "string", "description": "When to fire (natural language)."},
            "message": {"type": "string", "description": "What to remind about."},
        },
        required = ["when"],
        run      = _remind,
    ),
    make_spec(
        name        = "list_reminders",
        description = "Lists all pending reminders.",
        parameters  = {},
        run         = _list,
    ),
]
