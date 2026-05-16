"""Аудио-стримы с явным разделением входа и выхода.

Раньше всё это жило одной кашей в ``main.AkliLive`` — три async-таска
дёргали один и тот же флаг ``_is_speaking``. Здесь:

* ``MicStream`` — пишет с микрофона, гейтит подачу по ``SpeakingState``;
* ``PlayerStream`` — читает чанки из очереди, сигналит «дренировано»
  обратно в state. На реконнекте :meth:`PlayerStream.flush` чистит хвост.

Никаких глобальных переменных, никаких общих флагов мимо ``state``.
"""

from __future__ import annotations

import asyncio
import queue as _stdqueue
import threading
from typing import Final

import sounddevice as sd

from akli.live.state import Phase, SpeakingState
from akli.utils.log import get_logger

_log = get_logger("audio")

SEND_SAMPLE_RATE:    Final = 16000
RECEIVE_SAMPLE_RATE: Final = 24000
CHANNELS:            Final = 1
CHUNK_FRAMES:        Final = 1024


class MicStream:
    """Захват с микрофона и проброс кадров в ``send_q``.

    ``send_q`` — это asyncio-очередь, доступная live-сессии. Так как
    sounddevice вызывает callback в собственном потоке, проброс в очередь
    идёт через ``loop.call_soon_threadsafe`` — иначе будут гонки внутри
    asyncio-планировщика.

    Если очередь переполнена (``QueueFull``), кадр **тихо** отбрасывается,
    но инкрементится счётчик ``dropped``. Это лучше, чем падать или
    блокироваться: модель просто не получит миллисекунду звука.
    """

    def __init__(
        self,
        state:    SpeakingState,
        send_q:   asyncio.Queue,
        loop:     asyncio.AbstractEventLoop,
        *,
        device:   int | None = None,
    ) -> None:
        self._state  = state
        self._q      = send_q
        self._loop   = loop
        self._device = device
        self._stream: sd.RawInputStream | None = None
        self.dropped = 0

    # sounddevice вызывает это из аудио-потока
    def _on_audio(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            _log.debug("mic status: %s", status)
        if not self._state.can_send_mic():
            return
        # bytes(indata) — копия буфера. Без копии sounddevice переиспользует
        # буфер и мы рискуем отправить мусор.
        payload = {
            "data":      bytes(indata),
            "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
        }
        self._loop.call_soon_threadsafe(self._enqueue, payload)

    def _enqueue(self, payload: dict) -> None:
        try:
            self._q.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 50 == 0:
                _log.warn("mic queue full, dropped %d frames cumulative", self.dropped)

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.RawInputStream(
            samplerate = SEND_SAMPLE_RATE,
            channels   = CHANNELS,
            dtype      = "int16",
            blocksize  = CHUNK_FRAMES,
            callback   = self._on_audio,
            device     = self._device,
        )
        self._stream.start()
        _log.info("mic stream started @ %d Hz", SEND_SAMPLE_RATE)

    def stop(self) -> None:
        s = self._stream
        self._stream = None
        if s is None:
            return
        try:
            s.stop()
            s.close()
        except Exception as e:
            _log.warn("mic stop error: %s", e)
        _log.info("mic stream stopped (dropped frames: %d)", self.dropped)


class PlayerStream:
    """Проигрывание ответных чанков через blocking-write в отдельном потоке.

    Это синхронный поток (а не asyncio-таск), потому что
    ``sd.RawOutputStream.write`` блокирует, пока буфер устройства не
    освободится. Если делать через ``asyncio.to_thread`` на каждый чанк —
    плодим тонну тредов. Один долгоживущий поток с обычной очередью проще
    и стабильнее.
    """

    def __init__(self, state: SpeakingState, *, device: int | None = None) -> None:
        self._state  = state
        self._device = device
        self._inbox: _stdqueue.Queue = _stdqueue.Queue(maxsize=256)
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target = self._run,
            name   = "AkliPlayer",
            daemon = True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        # Будит .get(), если он спит.
        try:
            self._inbox.put_nowait(b"")
        except _stdqueue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def flush(self) -> None:
        """Сбрасывает невоспроизведённый хвост (нужно при реконнекте, фикс B7)."""
        cleared = 0
        while True:
            try:
                self._inbox.get_nowait()
                cleared += 1
            except _stdqueue.Empty:
                break
        if cleared:
            _log.info("player flushed %d pending chunks", cleared)

    def enqueue(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            self._inbox.put_nowait(chunk)
            self._state.on_chunk_enqueued()
        except _stdqueue.Full:
            _log.warn("player queue full, dropping chunk (this should not happen)")

    # ──────────────────────────── рантайм потока ──

    def _run(self) -> None:
        try:
            stream = sd.RawOutputStream(
                samplerate = RECEIVE_SAMPLE_RATE,
                channels   = CHANNELS,
                dtype      = "int16",
                blocksize  = CHUNK_FRAMES,
                device     = self._device,
            )
            stream.start()
        except Exception as e:
            _log.error("player open failed: %s", e)
            return

        _log.info("player thread started @ %d Hz", RECEIVE_SAMPLE_RATE)
        try:
            while not self._stop_flag.is_set():
                try:
                    chunk = self._inbox.get(timeout=0.5)
                except _stdqueue.Empty:
                    continue
                if not chunk:
                    continue
                try:
                    stream.write(chunk)
                except Exception as e:
                    _log.warn("player write failed: %s", e)
                finally:
                    self._state.on_chunk_drained()
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            _log.info("player thread exited")
