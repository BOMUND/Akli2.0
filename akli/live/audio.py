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
import time
from typing import Final

import sounddevice as sd

from akli.live.state import Phase, SpeakingState
from akli.utils.log import get_logger

_log = get_logger("audio")

SEND_SAMPLE_RATE:    Final = 16000
RECEIVE_SAMPLE_RATE: Final = 24000
CHANNELS:            Final = 1
# Размер микрофонного блока. 2048 фреймов = 128 мс. Раньше было 1024,
# но на медленных каналах (VPN) серверный keepalive ловил таймаут от
# переполнения исходящего буфера, потому что ~16 чанков/сек оказывались
# слишком частыми. 8 чанков/сек куда стабильнее.
CHUNK_FRAMES:        Final = 2048

# Порог RMS, ниже которого фрейм считается «тихим» и не уезжает в сеть.
# 16-битный PCM, диапазон ±32768; 60 — это примерно −55 dBFS, реальный
# фоновый шум комнаты обычно ниже. Это **client-side** оптимизация:
# серверный VAD по-прежнему работает, просто мы не нагружаем VPN
# гигабайтами тишины.
SILENCE_RMS_THRESHOLD: Final = 60
# Сколько подряд «тихих» чанков всё ещё прокидываем после речи —
# нужно, чтобы серверный VAD корректно поймал конец фразы.
SILENCE_TAIL_CHUNKS:   Final = 6
# Сколько секунд между диагностическими логами про микрофон.
MIC_STATS_INTERVAL_SEC: Final = 5.0


def _rms_i16(raw: bytes) -> float:
    """Быстрый RMS по 16-битному PCM без numpy.

    ``audioop`` — стандартная либа CPython, идёт со всеми сборками и
    реализована на C, так что для 128 мс блока работает быстрее, чем
    numpy. На Python 3.13 модуль перенесён в ``audioop`` deprecated; если
    его вдруг нет, падать не хочется — возвращаем 32768 (т.е. «не тишина»,
    чтобы пропустить через сеть и не глушить речь).
    """
    try:
        import audioop  # noqa: PLC0415
        return float(audioop.rms(raw, 2))
    except Exception:
        return 32768.0


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
        self.sent     = 0
        self.silent   = 0
        self.dropped  = 0
        self._silence_streak = 0
        self._last_stats_at  = 0.0

    # sounddevice вызывает это из аудио-потока
    def _on_audio(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            _log.debug("mic status: %s", status)
        if not self._state.can_send_mic():
            return
        raw = bytes(indata)
        rms = _rms_i16(raw)
        if rms < SILENCE_RMS_THRESHOLD:
            self._silence_streak += 1
            # Пропускаем только когда уже отправили достаточно «хвоста»
            # тишины, чтобы серверный VAD понял конец фразы.
            if self._silence_streak > SILENCE_TAIL_CHUNKS:
                self.silent += 1
                self._maybe_log_stats()
                return
        else:
            self._silence_streak = 0
        payload = {
            "data":      raw,
            "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
        }
        self._loop.call_soon_threadsafe(self._enqueue, payload)

    def _enqueue(self, payload: dict) -> None:
        try:
            self._q.put_nowait(payload)
            self.sent += 1
        except asyncio.QueueFull:
            # Очередь забилась — выкидываем самый старый чанк и пихаем
            # свежий. Терять текущую речь хуже, чем терять секундной
            # давности молчание.
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._q.put_nowait(payload)
                self.sent += 1
            except asyncio.QueueFull:
                pass
            self.dropped += 1
            if self.dropped % 50 == 0:
                _log.warn(
                    "mic queue full, dropped %d frames cumulative (queue=%d/%d)",
                    self.dropped, self._q.qsize(), self._q.maxsize,
                )
        self._maybe_log_stats()

    def _maybe_log_stats(self) -> None:
        now = time.monotonic()
        if now - self._last_stats_at < MIC_STATS_INTERVAL_SEC:
            return
        self._last_stats_at = now
        _log.debug(
            "mic stats: sent=%d silent=%d dropped=%d qsize=%d",
            self.sent, self.silent, self.dropped, self._q.qsize(),
        )

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
        _log.info(
            "mic stream stopped (sent=%d silent=%d dropped=%d)",
            self.sent, self.silent, self.dropped,
        )


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
