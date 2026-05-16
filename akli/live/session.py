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
from akli.memory.extract import extract_facts
from akli.memory.store import MemoryStore, RecentStore
from akli.memory.transcript import Transcript, read_transcript
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
LIVE_MEMORY_DEBOUNCE_SEC = 1.0


class LiveSession:
    """Голосовая сессия с Gemini. Управляется снаружи через ``run()``."""

    def __init__(
        self,
        config:  AppConfig,
        state:   SpeakingState,
        router:  Router,
        memory:  MemoryStore,
        recent:  RecentStore,
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
        self._manual_interrupt_requested = False
        self._memory_task: asyncio.Task | None = None
        self._memory_dirty = False
        self._memory_last_chars = 0
        # Транскрипт сессии: append-only файл state/dialogs/<ts>.txt.
        # Пишем реплики сразу после финальной транскрипции Live API; факты
        # извлекаются в фоне в этом же запуске и повторно на следующем старте,
        # если приложение упало до обработки.
        self._recent = recent
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
            self._schedule_memory_update()
            self._state.go_thinking()
        except Exception as e:
            _log.warn("text send failed: %s", e)

    def request_stop_tool(self) -> None:
        """Кнопка STOP / явная отмена."""
        self.request_interrupt()

    def request_interrupt(self) -> bool:
        """Немедленно остановить текущий ответ/тулзу из UI."""
        ok = self._state.request_interrupt()
        active = ok or self._session is not None
        if not active:
            return False
        self._manual_interrupt_requested = True
        self._out_buf.clear()
        self._in_buf.clear()
        self._out_has_transcription = False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._abort_live_session)
        return True

    def _abort_live_session(self) -> None:
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
            _log.warn("manual interrupt close failed: %s", e)

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
                if self._manual_interrupt_requested:
                    _log.info("manual interrupt closed live session")
                else:
                    _log.warn("session crashed: %s", e)
                    self._ui_log(f"SYS: connection lost ({e})")

            if self._stop.is_set():
                break

            if self._manual_interrupt_requested:
                self._manual_interrupt_requested = False
                backoff_idx = 0
                delay = 0.2
                self._ui_log("SYS: interrupted, reconnecting.")
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

        await self._refresh_memory_now("pre-connect")
        cfg = self._build_config()
        self._state.reset_for_reconnect()
        self._ensure_streams()
        if self._player is not None:
            self._player.flush()
        # Дочистить старые микро-фреймы, которые могли натолкаться в send-очередь
        # между падением сокета и пересозданием сессии (часть фикса B7).
        self._clear_send_queue()

        model_id = self._config.gemini_live_model or "gemini-live-2.5-flash-preview"
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
        # Скользящее окно из последних N session-summary — даёт модели
        # «о чём говорили на прошлой неделе», без выдачи всего сырого
        # транскрипта. Заполняется фоновой задачей на старте после того,
        # как extract.summarize_session обработает предыдущие диалоги.
        recent_section = self._recent.format_for_prompt()
        instructions = (
            f"{prompt}\n\n"
            f"[CURRENT DATE]\n{now.strftime('%A, %d %B %Y, %H:%M')}\n"
            f"[OS]\n{self._config.os_system or 'unknown'}\n"
            f"{memory_section}"
            f"{recent_section}"
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
        # Закрываем транскрипт — экстракция уйдёт в следующий запуск
        # (или в этом же запуске, если startup-обработчик ещё не дошёл
        # до файла). Сам процессинг никогда не блокирует shutdown.
        if self._memory_task is not None and not self._memory_task.done():
            self._memory_task.cancel()
            try:
                await self._memory_task
            except asyncio.CancelledError:
                pass
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
                if self._manual_interrupt_requested:
                    continue
                if response.data:
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
                    if out_tr is not None:
                        if out_tr.text:
                            self._out_has_transcription = True
                            self._out_buf.append(out_tr.text)

                    # Native-audio модель шлёт текст в model_turn.parts[].text
                    # как побочный продукт аудио-генерации. Пропускаем
                    # «думающие» части (part.thought=True) — они не для
                    # пользователя, это chain-of-thought reasoning модели.
                    mt = sc.model_turn
                    if mt is not None and mt.parts:
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

    def _finalize_user_transcript(self) -> None:
        user_text = "".join(self._in_buf).strip()
        self._in_buf.clear()
        if not user_text:
            return
        self._ui_log(f"You: {user_text}")
        self._transcript.append_user(user_text)
        self._schedule_memory_update()
        self._state.go_thinking()

    async def _on_turn_complete(self) -> None:
        self._finalize_user_transcript()
        model_text = "".join(self._out_buf).strip()
        self._out_buf.clear()
        self._out_has_transcription = False
        if model_text:
            self._ui_log(f"Akli: {model_text}")
            self._transcript.append_assistant(model_text)
            self._schedule_memory_update()

    def _schedule_memory_update(self) -> None:
        self._memory_dirty = True
        if self._memory_task is not None and not self._memory_task.done():
            return
        self._memory_task = asyncio.create_task(self._memory_update_loop())

    async def _memory_update_loop(self) -> None:
        while self._memory_dirty and not self._stop.is_set():
            self._memory_dirty = False
            await asyncio.sleep(LIVE_MEMORY_DEBOUNCE_SEC)
            await self._refresh_memory_now("turn")

    async def _refresh_memory_now(self, reason: str) -> None:
        if not self._config.gemini_api_key and not (
            self._config.use_openrouter and self._config.openrouter_api_key
        ):
            return
        body = read_transcript(self._transcript.path)
        body_len = len(body.strip())
        if body_len < 50 or body_len <= self._memory_last_chars:
            return
        facts = await extract_facts(
            body,
            gemini_api_key=self._config.gemini_api_key,
            openrouter_key=(
                self._config.openrouter_api_key if self._config.use_openrouter else ""
            ),
            openrouter_model=self._config.openrouter_model,
        )
        self._memory_last_chars = body_len
        if facts:
            self._memory.update(facts)
            _log.info("memory updated from live transcript (%s): %d categories", reason, len(facts))
            self._ui_log("SYS: memory updated.")

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
