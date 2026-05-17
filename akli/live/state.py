"""Единый state-machine рантайма.

Фиксит баг ``B2`` (зависший SPEAKING) тем, что снять состояние SPEAKING
можно только когда выполнены **оба** условия:

* пришёл ``turn_complete`` от модели;
* плеер реально дренировал все полученные чанки.

Раньше каждый из этих сигналов независимо дёргал общий флаг — отсюда
гонка. Здесь сигнал об окончании ответа централизован.

Дополнительно поднимаем вотчдог: если SPEAKING долго не получает вообще
никакого аудио-прогресса — принудительно гасим. Если TOOL не получает
heartbeat 60 сек — отменяем asyncio-таск.
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from akli.utils.log import get_logger

_log = get_logger("state")

SPEAKING_STUCK_SECONDS  = 30.0
THINKING_STUCK_SECONDS  = 12.0
TOOL_IDLE_SECONDS       = 60.0
WATCHDOG_INTERVAL       = 0.5
SPEAKING_TAIL_GRACE_SEC = 0.3   # сколько ждём «хвоста» перед LISTENING


class Phase(str, enum.Enum):
    """Грубые фазы жизненного цикла. Имена различимы намеренно."""

    IDLE      = "IDLE"
    LISTENING = "LISTENING"
    THINKING  = "THINKING"
    SPEAKING  = "SPEAKING"
    TOOL      = "TOOL"
    MUTED     = "MUTED"


@dataclass
class _Snapshot:
    phase:             Phase
    chunks_in_flight:  int
    turn_done:         bool
    last_chunk_age:    float
    in_tool:           bool


class SpeakingState:
    """Потокобезопасный держатель текущей фазы.

    Используется одновременно из audio-callback-а sounddevice (отдельный
    поток ОС), из asyncio-loop-а Gemini Live, и из Qt-обработчиков. Поэтому
    замок — ``threading.Lock``: он работает в обоих контекстах и здесь не
    становится узким горлышком (все операции крошечные).
    """

    def __init__(self, on_change: Callable[[Phase], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._phase: Phase = Phase.IDLE
        # Колбэк «надо немедленно очистить очередь плеера». Ставится
        # сессией после старта PlayerStream. Зовётся:
        #  * при сервер-side interrupt (см. on_interrupt);
        #  * когда watchdog принудительно перевёл SPEAKING → LISTENING.
        self._on_player_flush: Callable[[], None] = lambda: None
        self._pre_mute_phase: Phase = Phase.LISTENING

        self._chunks_in_flight = 0
        self._turn_done = True
        self._last_chunk_at = 0.0
        self._last_audio_progress_at = 0.0
        self._thinking_started_at = 0.0

        self._tool_cancel: asyncio.Event | None = None
        self._tool_loop: asyncio.AbstractEventLoop | None = None
        self._tool_started_at = 0.0
        self._last_heartbeat_at = 0.0
        self._tool_name = ""

        self._on_change = on_change or (lambda _p: None)

    # ──────────────────────────────── свойства ──

    @property
    def phase(self) -> Phase:
        with self._lock:
            return self._phase

    @property
    def is_muted(self) -> bool:
        return self.phase is Phase.MUTED

    def snapshot(self) -> _Snapshot:
        with self._lock:
            return _Snapshot(
                phase            = self._phase,
                chunks_in_flight = self._chunks_in_flight,
                turn_done        = self._turn_done,
                last_chunk_age   = time.monotonic() - self._last_chunk_at if self._last_chunk_at else 0.0,
                in_tool          = self._tool_cancel is not None,
            )

    def can_send_mic(self) -> bool:
        """Открыт ли микрофон в текущей фазе.

        * LISTENING / THINKING / TOOL / SPEAKING → открыт.
          - SPEAKING: пускаем аудио, чтобы серверный VAD мог поймать
            «пользователь заговорил» и прервать ответ модели (barge-in).
          - TOOL: голос не отменяет тулзу (см. ``request_stop``), но мы
            хотим, чтобы можно было дополнительно сказать ассистенту что-то
            «пока ищется».
        * MUTED / IDLE → закрыт.
        """
        p = self.phase
        return p in (Phase.LISTENING, Phase.THINKING, Phase.TOOL, Phase.SPEAKING)

    # ──────────────────────────────── переходы ──

    def _switch(self, new: Phase) -> None:
        if self._phase is new:
            return
        _log.info("phase: %s → %s", self._phase.value, new.value)
        self._phase = new
        try:
            self._on_change(new)
        except Exception as e:
            _log.warn("on_change callback raised: %s", e)

    def reset_for_reconnect(self) -> None:
        """Чистим всё перед новой live-сессией (фикс B7)."""
        with self._lock:
            self._chunks_in_flight = 0
            self._turn_done = True
            self._last_chunk_at = 0.0
            self._last_audio_progress_at = 0.0
            self._thinking_started_at = 0.0
            self._tool_cancel = None
            self._tool_loop = None
            self._tool_started_at = 0.0
            self._last_heartbeat_at = 0.0
            self._tool_name = ""
            if self._phase is not Phase.MUTED:
                self._switch(Phase.LISTENING)

    def on_interrupt(self) -> None:
        """Барджин: сервер прислал ``interrupted=True``.

        Сбрасываем счётчик ``chunks_in_flight`` — плеер только что
        выкинул всю невоспроизведённую очередь, считать их играющими
        смысла нет. Иначе ``maybe_finish_speaking`` так и не отпустит
        фазу SPEAKING и watchdog будет ждать 10 сек.
        """
        with self._lock:
            self._chunks_in_flight = 0
            self._turn_done = True
            self._last_chunk_at = 0.0
            self._last_audio_progress_at = 0.0
            self._thinking_started_at = 0.0
            if self._phase in (Phase.THINKING, Phase.SPEAKING):
                self._switch(Phase.LISTENING)

    def set_player_flush(self, fn: Callable[[], None]) -> None:
        """Сессия передаёт сюда ``PlayerStream.flush``."""
        self._on_player_flush = fn or (lambda: None)

    def go_idle(self) -> None:
        with self._lock:
            self._switch(Phase.IDLE)

    def go_listening(self) -> None:
        with self._lock:
            if self._phase is Phase.MUTED:
                return
            self._switch(Phase.LISTENING)

    def go_thinking(self) -> None:
        with self._lock:
            if self._phase in (Phase.MUTED, Phase.TOOL, Phase.SPEAKING):
                return
            self._thinking_started_at = time.monotonic()
            self._switch(Phase.THINKING)

    def toggle_mute(self) -> bool:
        """Возвращает новое состояние ``is_muted``."""
        with self._lock:
            if self._phase is Phase.MUTED:
                self._switch(self._pre_mute_phase if self._pre_mute_phase != Phase.MUTED
                             else Phase.LISTENING)
                return False
            self._pre_mute_phase = self._phase
            self._switch(Phase.MUTED)
            return True

    # ──────────────────────────────── аудио ──

    def on_chunk_enqueued(self) -> None:
        """Сервер прислал звуковой чанк — кладём его в плеер."""
        with self._lock:
            now = time.monotonic()
            self._chunks_in_flight += 1
            self._last_chunk_at = now
            self._last_audio_progress_at = now
            self._thinking_started_at = 0.0
            self._turn_done = False
            if self._phase in (Phase.LISTENING, Phase.THINKING):
                self._switch(Phase.SPEAKING)

    def on_chunk_drained(self) -> None:
        """Плеер закончил проигрывание чанка."""
        with self._lock:
            self._chunks_in_flight = max(0, self._chunks_in_flight - 1)
            self._last_audio_progress_at = time.monotonic()
            self._maybe_finish_speaking_locked()

    def on_turn_complete(self) -> None:
        with self._lock:
            self._turn_done = True
            if self._phase is Phase.THINKING:
                self._thinking_started_at = 0.0
                self._switch(Phase.LISTENING)
                return
            self._maybe_finish_speaking_locked()

    def _maybe_finish_speaking_locked(self) -> None:
        if self._phase is not Phase.SPEAKING:
            return
        if self._chunks_in_flight > 0 or not self._turn_done:
            return
        # Хвост звука уже сыгран — выдержим небольшую паузу, чтобы микрофон
        # не подхватил эхо самого последнего чанка.
        age = time.monotonic() - self._last_chunk_at
        if age < SPEAKING_TAIL_GRACE_SEC:
            return
        self._switch(Phase.LISTENING)

    # ──────────────────────────────── тулзы ──

    def begin_tool(self, name: str, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        with self._lock:
            self._tool_cancel = asyncio.Event()
            self._tool_loop = loop
            self._tool_started_at = time.monotonic()
            self._last_heartbeat_at = self._tool_started_at
            self._tool_name = name
            self._switch(Phase.TOOL)
            return self._tool_cancel

    def end_tool(self) -> None:
        with self._lock:
            self._tool_cancel = None
            self._tool_loop = None
            self._tool_name = ""
            # После tool возвращаемся в LISTENING (если ещё не пришли новые
            # аудио-чанки от модели; в этом случае фаза станет SPEAKING при
            # следующем on_chunk_enqueued).
            if self._phase is Phase.TOOL:
                self._switch(Phase.LISTENING)

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat_at = time.monotonic()

    def request_stop(self) -> bool:
        """UI-кнопка ``STOP`` или явная команда — отменить текущую тулзу.

        Возвращает ``True``, если что-то реально было отменено.
        """
        with self._lock:
            ev = self._tool_cancel
            loop = self._tool_loop
            name = self._tool_name
        if ev is None or loop is None:
            return False
        loop.call_soon_threadsafe(ev.set)
        _log.warn("Stop requested for tool: %s", name)
        return True

    def request_interrupt(self) -> bool:
        """Остановить текущий ответ/размышление/тулзу по кнопке Interrupt."""
        player_flush: Callable[[], None] | None = None
        with self._lock:
            active = self._phase in (Phase.THINKING, Phase.SPEAKING)
            if not active:
                return False
            self._chunks_in_flight = 0
            self._turn_done = True
            self._last_chunk_at = 0.0
            self._last_audio_progress_at = 0.0
            self._thinking_started_at = 0.0
            player_flush = self._on_player_flush
            if self._phase is not Phase.MUTED:
                self._switch(Phase.LISTENING)

        if player_flush is not None:
            try:
                player_flush()
            except Exception as e:
                _log.warn("player flush from interrupt raised: %s", e)
        _log.info("manual interrupt requested")
        return True

    # ──────────────────────────────── вотчдог ──

    async def watchdog(self, stop: asyncio.Event) -> None:
        """Каждые 0.5 сек проверяет «зависшие» состояния и чинит их."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            self._tick()

    def _tick(self) -> None:
        now = time.monotonic()
        forced_listening = False
        tool_cancel: asyncio.Event | None = None
        tool_loop: asyncio.AbstractEventLoop | None = None
        player_flush: Callable[[], None] | None = None

        with self._lock:
            # THINKING-страховка: Gemini иногда завершает ход без аудио или
            # turn_complete теряется в preview Live API. UI не должен висеть.
            if (self._phase is Phase.THINKING and self._thinking_started_at
                    and now - self._thinking_started_at > THINKING_STUCK_SECONDS):
                _log.warn(
                    "watchdog: THINKING висит %.1fs — форс LISTENING",
                    now - self._thinking_started_at,
                )
                self._thinking_started_at = 0.0
                self._switch(Phase.LISTENING)
                forced_listening = True

            # SPEAKING-страховка
            elif (self._phase is Phase.SPEAKING and self._last_audio_progress_at
                    and now - self._last_audio_progress_at > SPEAKING_STUCK_SECONDS):
                _log.warn(
                    "watchdog: SPEAKING без аудио-прогресса %.1fs — форс LISTENING",
                    now - self._last_audio_progress_at,
                )
                self._chunks_in_flight = 0
                self._turn_done = True
                self._switch(Phase.LISTENING)
                forced_listening = True
                player_flush = self._on_player_flush

            # Грейс хвоста уже мог истечь
            elif (self._phase is Phase.SPEAKING and self._chunks_in_flight == 0
                    and self._turn_done):
                self._maybe_finish_speaking_locked()

            # TOOL idle
            elif (self._phase is Phase.TOOL and self._tool_cancel is not None
                    and now - self._last_heartbeat_at > TOOL_IDLE_SECONDS):
                _log.warn(
                    "watchdog: tool '%s' idle %.1fs — отменяю",
                    self._tool_name, now - self._last_heartbeat_at,
                )
                tool_cancel = self._tool_cancel
                tool_loop = self._tool_loop

        if player_flush is not None:
            try:
                player_flush()
            except Exception as e:
                _log.warn("player flush from watchdog raised: %s", e)

        if tool_cancel is not None and tool_loop is not None:
            tool_loop.call_soon_threadsafe(tool_cancel.set)
