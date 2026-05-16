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

from akli.config import AppConfig, load_prompt
from akli.live.audio import (
    CHUNK_FRAMES,
    MicStream,
    PlayerStream,
    SEND_SAMPLE_RATE,
)
from akli.live.state import Phase, SpeakingState
from akli.memory.store import MemoryStore
from akli.memory.extract import extract_facts_async
from akli.tools.registry import Router
from akli.utils.log import get_logger

_log = get_logger("live")

VOICE_NAME  = "Puck"       # мужской голос Gemini Live
# Было 32 слота — на VPN очередь забивалась за ~2 сек, потом дропы.
# 128 слотов × 128 мс = до ~16 сек буфера — достаточно для переживания
# временных замедлений, но не вызывает проблемы backpressure.
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

        # Хэндл для возобновления сессии без потери контекста после обрыва.
        # Сервер сам присылает его в ``session_resumption_update``.
        self._resume_handle: str | None = None

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
            self._state.go_thinking()
        except Exception as e:
            _log.warn("text send failed: %s", e)

    def request_stop_tool(self) -> None:
        """Кнопка STOP / явная отмена."""
        self._state.request_stop()

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
                _log.warn("session crashed: %s", e)
                self._ui_log(f"SYS: connection lost ({e})")

            if self._stop.is_set():
                break

            lived = time.monotonic() - connected_at
            if lived >= SHORT_RUN_RESET_SEC:
                backoff_idx = 0
            else:
                backoff_idx = min(backoff_idx + 1, len(BACKOFF_SEQ) - 1)
            delay = BACKOFF_SEQ[backoff_idx]
            _log.warn("reconnecting in %.0fs", delay)
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

        cfg = self._build_config()
        self._state.reset_for_reconnect()
        self._ensure_streams()
        if self._player is not None:
            self._player.flush()
        # Дочистить старые микро-фреймы, которые могли натолкаться в send-очередь
        # между падением сокета и пересозданием сессии (часть фикса B7).
        if self._send_q is not None:
            while not self._send_q.empty():
                try:
                    self._send_q.get_nowait()
                except asyncio.QueueEmpty:
                    break

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
        instructions = (
            f"{prompt}\n\n"
            f"[CURRENT DATE]\n{now.strftime('%A, %d %B %Y, %H:%M')}\n"
            f"[OS]\n{self._config.os_system or 'unknown'}\n"
            f"{memory_section}"
        )

        kwargs: dict = dict(
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
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            # Сжимаем контекст на 128k токенах, чтобы не ловить принудительный
            # разрыв по лимиту сессии — рекомендация из доков «live-api troubleshooting».
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=25_000,
                sliding_window=types.SlidingWindow(target_tokens=12_800),
            ),
        )
        # Первый коннект — handle пуст, сервер пришлёт свежий. На реконнекте —
        # подключаемся с предыдущим, чтобы не терять состояние разговора.
        kwargs["session_resumption"] = types.SessionResumptionConfig(
            handle=self._resume_handle,
        )
        return types.LiveConnectConfig(**kwargs)

    # ───────────────────────────── stream lifecycle ──

    def _ensure_streams(self) -> None:
        if self._send_q is None:
            self._send_q = asyncio.Queue(maxsize=SEND_QUEUE)
        if self._player is None:
            self._player = PlayerStream(self._state)
            self._player.start()
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
        assert self._session is not None
        async for response in self._session.receive():
            if response.data:
                if self._player is not None:
                    self._player.enqueue(response.data)

            sc = response.server_content
            if sc is not None:
                if sc.output_transcription and sc.output_transcription.text:
                    self._out_buf.append(sc.output_transcription.text)
                if sc.input_transcription and sc.input_transcription.text:
                    self._in_buf.append(sc.input_transcription.text)
                if sc.turn_complete:
                    self._state.on_turn_complete()
                    await self._on_turn_complete()

            # Хэндл возобновления сессии — запоминаем, чтобы переконнект был
            # «прозрачным» и мы не теряли историю разговора.
            sru = getattr(response, "session_resumption_update", None)
            if sru is not None and getattr(sru, "resumable", False) and sru.new_handle:
                self._resume_handle = sru.new_handle

            tc = response.tool_call
            if tc is not None and tc.function_calls:
                await self._handle_tool_calls(tc.function_calls)

    async def _on_turn_complete(self) -> None:
        user_text = "".join(self._in_buf).strip()
        model_text = "".join(self._out_buf).strip()
        self._in_buf.clear()
        self._out_buf.clear()
        if user_text:
            self._ui_log(f"You: {user_text}")
        if model_text:
            self._ui_log(f"Akli: {model_text}")
        if len(user_text) >= 5 and len(model_text) >= 5:
            # Память — fire-and-forget. Любая ошибка только в лог.
            asyncio.create_task(self._extract_memory(user_text, model_text))

    async def _extract_memory(self, user_text: str, model_text: str) -> None:
        try:
            patch = await extract_facts_async(
                user_text     = user_text,
                model_text    = model_text,
                api_key       = self._config.gemini_api_key,
            )
            if patch:
                self._memory.update(patch)
                _log.info("memory: +%d categories", len(patch))
        except Exception as e:
            _log.warn("memory extract failed: %s", e)

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
