"""Цикл жизни Gemini Live сессии.

Сравнительно с прежним ``main.AkliLive`` — основные перемены:

* реконнект с экспоненциальным backoff, а не фиксированной паузой;
* перед каждым подключением чистятся очереди и состояние (фикс ``B7``);
* приём сервера живёт в одном цикле с явной обработкой каждого типа
  ответа — без вложенных ``async for`` внутри ``async for``;
* все тулзы прокидываются через ``Router`` (см. :mod:`akli.tools.registry`);
* система instruction строится **на каждый коннект**, чтобы текущее время
  и память были свежими (часть фикса ``B5``).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Callable

from google import genai
from google.genai import types

from akli.core.config import AppConfig, load_prompt
from akli.live.audio import (
    CHUNK_FRAMES,
    MicStream,
    PlayerStream,
    SEND_SAMPLE_RATE,
)
from akli.live.state import Phase, SpeakingState
from akli.memory.store import MemoryStore
from akli.memory.transcript import Transcript
from akli.tools.registry import Router
from akli.utils.log import get_logger

_log = get_logger("live")

VOICE_NAME  = "Puck"       # мужской голос Gemini Live
# 128 слотов × 64 мс = до ~8 сек буфера. Запас на временные замедления
# сети, без переразрастания: backpressure при заполнении значит, что
# либо сеть упала, либо WebSocket в бад-стейте — оба случая мы ловим в
# ``_send_loop`` через 5-сек таймаут на ``send_realtime_input``.
SEND_QUEUE  = 128
BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
SHORT_RUN_RESET_SEC = 30.0    # если сессия прожила дольше — сброс backoff
# Как часто логируем сводку по сессии. Нужно видеть, живы ли циклы.
SESSION_STATS_INTERVAL = 30.0


class LiveSession:
    """Голосовая сессия с Gemini. Управляется снаружи через ``run()``."""

    def __init__(
        self,
        config:  AppConfig,
        state:   SpeakingState,
        router:  Router,
        memory:  MemoryStore,
        ui_log:  Callable[[str], None],
    ) -> None:
        self._config = config
        self._state  = state
        self._router = router
        self._memory = memory
        self._ui_log = ui_log

        self._client: genai.Client | None = None
        self._session = None

        self._loop:   asyncio.AbstractEventLoop | None = None
        self._send_q: asyncio.Queue | None = None
        self._player: PlayerStream | None = None
        self._mic:    MicStream    | None = None

        self._stop = asyncio.Event()
        self._text_pending: list[str] = []
        self._text_lock = asyncio.Lock()

        self._in_buf:  list[str] = []
        self._out_buf: list[str] = []
        self._out_has_transcription = False
        self._manual_reconnect_requested = False
        self._ignore_output_until = 0.0
        # Транскрипт сессии: append-only файл state/dialogs/<ts>.txt.
        # Live path только пишет диалог; summary/core-memory обработка идёт
        # после закрытия transcript на следующем старте приложения через
        # akli.memory.processor (см. ui/app.py).
        self._transcript = Transcript()

    # ───────────────────────────── публичный API ──

    def submit_text(self, text: str) -> None:
        """Отправить текст в модель (из UI). Безопасно из любого треда."""
        text = (text or "").strip()
        if not text or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._queue_text(text), self._loop)

    async def _queue_text(self, text: str) -> None:
        async with self._text_lock:
            self._text_pending.append(text)
            if self._session is not None:
                await self._flush_text_locked()

    async def _flush_text_locked(self) -> None:
        if not self._session or not self._text_pending:
            return
        text = "\n".join(self._text_pending).strip()
        self._text_pending.clear()
        try:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=text)],
                ),
                turn_complete=True,
            )
            self._ui_log(f"You: {text}")
            self._transcript.append_user(text)
            self._state.go_thinking()
        except Exception as e:
            _log.warn("text send failed: %s", e)

    def request_stop_tool(self) -> bool:
        """Отменить только текущую тулзу, не трогая Gemini Live session."""
        return self._state.request_stop()

    def request_interrupt(self) -> bool:
        """Мягко остановить текущий ответ без reconnect и потери context.

        Вызывается из Qt UI-потока. Всё что касается буферов / плеера / VAD-таймера
        — мутируется из event-loop потока, иначе гонка с ``_recv_loop``.
        """
        if self._session is None:
            return False
        ok = self._state.request_interrupt()
        if not ok:
            return False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._apply_interrupt)
        return True

    def _apply_interrupt(self) -> None:
        # ``_in_buf`` намеренно не трогаем: это партиал пользовательской
        # транскрипции (для UI-лога), interrupt прерывает только ответ
        # модели, а не ввод пользователя. На полный сброс — reconnect.
        self._out_buf.clear()
        self._out_has_transcription = False
        # Короткое окно: игнорить любой чанк/текст от сервера, который успел уже
        # вылететь до того, как interrupt дошёл. Сервер по факту вышлет свой
        # ``interrupted=True`` в ближайшие ~100–300 мс — этого хватает.
        self._ignore_output_until = time.monotonic() + 0.5
        if self._player is not None:
            self._player.flush()

    def request_reconnect(self) -> bool:
        """Жёстко пересоздать Gemini Live session вручную.

        Вызывается из Qt UI-потока. Вся работа (буферы, закрытие сокета)
        выполняется в event-loop через ``call_soon_threadsafe``. Сам флаг
        ``_manual_reconnect_requested`` взводится сразу, чтобы ``run()``
        в любом случае увидел: падение инициировали мы, backoff не нужен.
        """
        if self._session is None or self._loop is None:
            return False
        self._manual_reconnect_requested = True
        self._loop.call_soon_threadsafe(self._apply_reconnect)
        return True

    def _apply_reconnect(self) -> None:
        self._out_buf.clear()
        self._in_buf.clear()
        self._out_has_transcription = False
        self._clear_send_queue()
        if self._player is not None:
            self._player.flush()
        asyncio.create_task(self._close_live_session())

    async def _close_live_session(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            await session.close()
        except Exception as e:
            _log.warn("manual reconnect close failed: %s", e)

    def shutdown(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    # ───────────────────────────── рантайм ──

    async def run(self) -> None:
        """Цикл реконнекта. Завершается по ``self._stop``."""
        self._loop = asyncio.get_running_loop()
        backoff_idx = 0

        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._manual_reconnect_requested:
                    _log.info("manual reconnect closed live session")
                else:
                    _log.warn("session crashed: %s", e)
                    self._ui_log(f"SYS: connection lost ({e})")

            if self._stop.is_set():
                break

            if self._manual_reconnect_requested:
                self._manual_reconnect_requested = False
                backoff_idx = 0
                delay = 0.2
                self._ui_log("SYS: reconnecting.")
            else:
                lived = time.monotonic() - connected_at
                if lived >= SHORT_RUN_RESET_SEC:
                    backoff_idx = 0
                else:
                    backoff_idx = min(backoff_idx + 1, len(BACKOFF_SEQ) - 1)
                delay = BACKOFF_SEQ[backoff_idx]
            _log.warn("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

        await self._teardown()
        _log.info("live loop exited")

    async def _run_once(self) -> None:
        if not self._config.gemini_api_key:
            await asyncio.sleep(1.0)
            raise RuntimeError("no Gemini API key configured")

        self._client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=self._config.gemini_api_key,
        )

        self._memory.reload()
        cfg = self._build_config()
        self._state.reset_for_reconnect()
        self._ensure_streams()
        if self._player is not None:
            self._player.flush()
        # Дочистить старые микро-фреймы, которые могли натолкаться в send-очередь
        # между падением сокета и пересозданием сессии (часть фикса B7).
        self._clear_send_queue()

        # Fallback должен совпадать с дефолтом AppConfig — иначе при пустом
        # ``gemini_live_model`` мы рискуем приземлиться на устаревшую модель.
        model_id = self._config.gemini_live_model or AppConfig().gemini_live_model
        async with self._client.aio.live.connect(model=model_id, config=cfg) as session:
            self._session = session
            _log.info("connected: %s, voice=%s", model_id, VOICE_NAME)
            self._ui_log("SYS: Akli online.")

            # На реконнекте дочекинаем накопленный текст пользователя
            async with self._text_lock:
                await self._flush_text_locked()

            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._send_loop())
                    tg.create_task(self._recv_loop())
                    tg.create_task(self._state.watchdog(self._stop))
            finally:
                self._session = None

    # ───────────────────────────── конфигурация ──

    def _build_config(self) -> "types.LiveConnectConfig":
        now = datetime.now()
        prompt = load_prompt()
        memory_section = self._memory.format_for_prompt()
        instructions = (
            f"{prompt}\n\n"
            f"[CURRENT DATE]\n{now.strftime('%A, %d %B %Y, %H:%M')}\n"
            f"[OS]\n{self._config.os_system or 'unknown'}\n"
            f"{memory_section}\n"
            "[DIALOG MEMORY]\n"
            "Older dialog summaries are available via memory_list_summaries "
            "and memory_read_summary tools. Use them when past context may help.\n"
        )

        # Минимальный конфиг: ровно то, что использует Mark37.
        # Отключаем thinking_config с budget=0: native-audio модель по
        # умолчанию режет «мысли» перед каждой репликой (chain-of-thought),
        # это и есть главный источник латентности и фолбэка «зачем-то
        # формирую план повествования» в Activity-логе.
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME),
                ),
            ),
            system_instruction=types.Content(
                role="user",
                parts=[types.Part(text=instructions)],
            ),
            tools=[types.Tool(function_declarations=self._router.declarations())],
            thinking_config=types.ThinkingConfig(
                thinking_budget=0,
                include_thoughts=False,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    prefix_padding_ms=300,
                    silence_duration_ms=500,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
            ),
        )

    # ───────────────────────────── stream lifecycle ──

    def _clear_send_queue(self) -> None:
        if self._send_q is None:
            return
        while not self._send_q.empty():
            try:
                self._send_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _ensure_streams(self) -> None:
        if self._send_q is None:
            self._send_q = asyncio.Queue(maxsize=SEND_QUEUE)
        if self._player is None:
            self._player = PlayerStream(self._state)
            self._player.start()
            # Дать state-watchdog'у доступ к player.flush — он сам решает,
            # когда нужен flush (interrupt от сервера, форс LISTENING).
            self._state.set_player_flush(self._player.flush)
        if self._mic is None:
            self._mic = MicStream(self._state, self._send_q, self._loop)  # type: ignore[arg-type]
            self._mic.start()

    async def _teardown(self) -> None:
        if self._mic is not None:
            self._mic.stop()
            self._mic = None
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._state.go_idle()
        # Закрываем транскрипт — summary/core обработка уйдёт в следующий запуск.
        try:
            self._transcript.close()
        except Exception as e:
            _log.warn("transcript close failed: %s", e)

    # ───────────────────────────── send / recv ──

    async def _send_loop(self) -> None:
        assert self._send_q is not None and self._session is not None
        sent = 0
        last_log = time.monotonic()
        try:
            while True:
                payload = await self._send_q.get()
                if payload is None or self._session is None:
                    return
                # Страхуемся от повисания на send: если WebSocket в бад-стейте,
                # бросаем исключение и TaskGroup подымет реконнект быстрее, чем
                # прилетел бы 20-секундный keepalive с сервера.
                await asyncio.wait_for(
                    self._session.send_realtime_input(audio=payload),
                    timeout=5.0,
                )
                sent += 1
                now = time.monotonic()
                if now - last_log >= SESSION_STATS_INTERVAL:
                    _log.debug(
                        "send loop alive: sent=%d qsize=%d", sent, self._send_q.qsize(),
                    )
                    last_log = now
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _log.warn("send loop timeout (>5s on one frame) — переконнект")
            raise
        except Exception as e:
            _log.warn("send loop error: %s", e)
            raise

    async def _recv_loop(self) -> None:
        """Читает сообщения от сервера в течение всей жизни сессии.

        Важный нюанс SDK ``google-genai``: публичный ``session.receive()``
        внутри делает ``break`` после первого ``turn_complete``. Один вызов
        = один ход разговора. Для непрерывного диалога нужно вызывать
        ``receive()`` в цикле: внешний ``while`` — «следующий ход»,
        внутренний ``async for`` — порции внутри хода.

        Без этого цикла после первого обмена мы переставали читать
        WebSocket: серверный буфер переполнялся, send-сторона TCP
        встречала backpressure, send_realtime_input висел > 5 с —
        и мы переподключались. Новый разговор живёт тоже ровно один ход в
        тех же условиях — цикл с нулевыми последствиями.
        """
        assert self._session is not None
        while self._session is not None:
            async for response in self._session.receive():
                ignore_output = time.monotonic() < self._ignore_output_until
                if response.data and not ignore_output:
                    if self._player is not None:
                        self._player.enqueue(response.data)

                sc = response.server_content
                if sc is not None:
                    # interrupted=True означает, что сервер сам решил прервать
                    # генерацию — обычно потому, что услышал пользователя
                    # (server-side barge-in). Нам нужно:
                    #  1. Немедленно очистить очередь плеера, иначе он будет
                    #     доигрывать N секунд старого аудио (главный баг,
                    #     из-за которого перебивание «не работало»).
                    #  2. Сбросить фазу на LISTENING без ожидания
                    #     turn_complete (его при interrupt может не быть).
                    if sc.interrupted:
                        _log.info("server interrupted current turn — flushing player")
                        if self._player is not None:
                            self._player.flush()
                        self._state.on_interrupt()
                        # Буфер ответа не сохраняем: это оборванная реплика.
                        self._out_buf.clear()
                        self._out_has_transcription = False
                        continue

                    in_tr = sc.input_transcription
                    if in_tr is not None:
                        if in_tr.text:
                            self._in_buf.append(in_tr.text)
                        if in_tr.finished:
                            self._finalize_user_transcript()

                    out_tr = sc.output_transcription
                    if out_tr is not None and not ignore_output:
                        if out_tr.text:
                            if not self._out_has_transcription:
                                # Первая транскрипция в ходу: отбрасываем
                                # «черновик» из ``parts[].text``, который
                                # мог попасть в буфер чуть раньше. Иначе
                                # один и тот же ответ модели окажется в
                                # Activity-логе дважды (через parts[] +
                                # через transcription).
                                self._out_buf.clear()
                                self._out_has_transcription = True
                            self._out_buf.append(out_tr.text)

                    # Native-audio модель шлёт текст в model_turn.parts[].text
                    # как побочный продукт аудио-генерации. Пропускаем
                    # «думающие» части (part.thought=True) — они не для
                    # пользователя, это chain-of-thought reasoning модели.
                    mt = sc.model_turn
                    if mt is not None and mt.parts and not ignore_output:
                        for part in mt.parts:
                            if part.thought:
                                continue
                            if part.text and not self._out_has_transcription:
                                self._out_buf.append(part.text)
                    if sc.turn_complete:
                        self._state.on_turn_complete()
                        await self._on_turn_complete()

                tc = response.tool_call
                if tc is not None and tc.function_calls:
                    await self._handle_tool_calls(tc.function_calls)

    def _finalize_user_transcript(self, *, mark_thinking: bool = True) -> None:
        user_text = "".join(self._in_buf).strip()
        self._in_buf.clear()
        if not user_text:
            return
        self._ui_log(f"You: {user_text}")
        self._transcript.append_user(user_text)
        if mark_thinking:
            self._state.go_thinking()

    async def _on_turn_complete(self) -> None:
        self._finalize_user_transcript(mark_thinking=False)
        model_text = "".join(self._out_buf).strip()
        self._out_buf.clear()
        self._out_has_transcription = False
        if model_text:
            self._ui_log(f"Akli: {model_text}")
            self._transcript.append_assistant(model_text)

    async def _handle_tool_calls(self, function_calls: list) -> None:
        responses = []
        for fc in function_calls:
            args = dict(fc.args) if fc.args else {}
            try:
                payload = await self._router.dispatch(fc.name, args)
            except asyncio.CancelledError:
                _log.warn("dispatch cancelled at top level for %s", fc.name)
                payload = {"result": "Tool was cancelled."}
            except Exception as e:
                _log.error("dispatch raised at top level: %s", e)
                payload = {"result": f"Tool failed: {e}"}
            responses.append(types.FunctionResponse(
                id       = fc.id,
                name     = fc.name,
                response = payload,
            ))

        if self._session is None:
            return
        try:
            await self._session.send_tool_response(function_responses=responses)
        except Exception as e:
            _log.warn("tool_response send failed: %s", e)
