"""Qt-обёртка приложения.

Стартует ``QApplication``, выводит setup при необходимости, создаёт
:class:`MainWindow` и связывает её сигналы со ``LiveSession``-ом.

Live-сессия живёт в отдельном потоке с собственным asyncio loop. Это
позволяет не блокировать Qt event loop и не делать заморочек с
``QEventLoop`` + asyncio bridge.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from akli.core.config import AppConfig, load, save
from akli.live.session import LiveSession
from akli.live.state import Phase, SpeakingState
from akli.memory.store import MemoryStore
from akli.memory.processor import process_pending_transcripts
from akli.tools import build_router
from akli.ui.setup import SetupDialog
from akli.ui.theme import GLOBAL_QSS
from akli.ui.window import MainWindow
from akli.utils.log import get_logger

_log = get_logger("ui")


class _UiBridge(QObject):
    """Сигнал «лог-строка пришла из бэкенда» — для thread-safe append в UI."""

    log_line = pyqtSignal(str)


class AkliApp:
    def __init__(self) -> None:
        self._qt = QApplication.instance() or QApplication([])
        self._qt.setApplicationName("Akli 2.0")
        self._qt.setStyleSheet(GLOBAL_QSS)

        self._config: AppConfig = load()
        self._state = SpeakingState()
        self._memory = MemoryStore()
        self._bridge = _UiBridge()
        self._bridge.log_line.connect(self._on_log)

        self._window: Optional[MainWindow] = None
        self._session: Optional[LiveSession] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ───────────────────────────── public ──

    def run(self) -> int:
        if not self._config.is_ready():
            if not self._ask_for_setup():
                return 0

        self._window = MainWindow(self._state)
        self._window.text_submitted.connect(self._on_text)
        self._window.mute_toggled.connect(self._on_mute)
        self._window.stop_tool_clicked.connect(self._on_stop_tool)
        self._window.interrupt_clicked.connect(self._on_interrupt)
        self._window.reconnect_clicked.connect(self._on_reconnect)
        self._window.show()

        self._start_session()

        try:
            return self._qt.exec()
        finally:
            self._shutdown_session()

    # ───────────────────────────── setup ──

    def _ask_for_setup(self) -> bool:
        dlg = SetupDialog(self._config)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return False
        self._config = dlg.result_config(self._config)
        save(self._config)
        return True

    # ───────────────────────────── session ──

    def _start_session(self) -> None:
        # Дёргаем здесь для логов «openrouter активен / выключен» — без
        # этого вызова модуль llm не импортируется и пользователь не
        # понимает, делает ли его OpenRouter-ключ хоть что-то.
        # Сейчас провайдер создаётся, но никем не используется
        # (планируется как fallback для текстовых тулз в будущем PR).
        from akli.core.llm import get_provider
        self._llm = get_provider(self._config)

        router = build_router(
            state  = self._state,
            config = self._config,
            log    = lambda line: self._bridge.log_line.emit(line),
            memory = self._memory,
        )
        self._session = LiveSession(
            config = self._config,
            state  = self._state,
            router = router,
            memory = self._memory,
            ui_log = lambda line: self._bridge.log_line.emit(line),
        )

        self._thread = threading.Thread(
            target = self._run_loop,
            name   = "AkliLive",
            daemon = True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Обработка неразобранных диалогов из прошлых запусков:
        # полностью в фоне, не блокирует запуск Live-сессии. При краше
        # прошлой сессии файлы лежали на диске без ``.done`` маркера —
        # теперь извлекаем факты + summary.
        self._loop.create_task(process_pending_transcripts(
            memory           = self._memory,
            gemini_api_key   = self._config.gemini_api_key,
            openrouter_key   = (self._config.openrouter_api_key
                                if self._config.use_openrouter else ""),
            openrouter_model = self._config.openrouter_model,
        ))
        try:
            self._loop.run_until_complete(self._session.run())
        except Exception as e:
            _log.error("loop crashed: %s", e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _shutdown_session(self) -> None:
        if self._session is not None:
            self._session.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    # ───────────────────────────── signals ──

    def _on_text(self, text: str) -> None:
        if self._session is not None:
            self._session.submit_text(text)

    def _on_mute(self) -> None:
        muted = self._state.toggle_mute()
        if self._window is not None:
            self._window.set_mute_text(muted)
        self._bridge.log_line.emit(f"SYS: {'muted' if muted else 'unmuted'}")

    def _on_stop_tool(self) -> None:
        if self._session is not None:
            ok = self._session.request_stop_tool()
            self._bridge.log_line.emit(
                "SYS: stop signal sent" if ok else "SYS: no tool running"
            )

    def _on_interrupt(self) -> None:
        if self._session is not None:
            ok = self._session.request_interrupt()
            self._bridge.log_line.emit(
                "SYS: interrupt sent" if ok else "SYS: nothing to interrupt"
            )

    def _on_reconnect(self) -> None:
        if self._session is not None:
            ok = self._session.request_reconnect()
            self._bridge.log_line.emit(
                "SYS: reconnect requested" if ok else "SYS: reconnect unavailable"
            )

    def _on_log(self, line: str) -> None:
        if self._window is not None:
            self._window.append_log(line)
