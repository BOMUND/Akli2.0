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
# Размер микрофонного блока. 1024 фреймов = 64 мс. Mark37 и оригинальный
# `live-api`-туториал Google используют 1024 — на этом значении латентность
# на «нормальном» канале минимальна. На медленном канале была идея
# уплотнить чанки до 2048 ради меньшего числа send-ов, но это давало
# +64 мс к латентности **каждого** фрейма, и реальный баг лежал в
# другом месте (см. фикс recv-loop). Возвращаемся к 1024.
CHUNK_FRAMES:        Final = 1024

# Echo-gate: пока модель говорит, считаем «фон» (RMS последних 0.5 с
# тишины). Если текущий чанк громче фона + EHO_GATE_DB — пользователь
# реально заговорил, и мы отдаём аудио серверу (его VAD сам прервёт
# модель). Иначе — дропаем, чтобы не получить эхо от динамиков.
ECHO_GATE_DB:        Final = 0.0    # Любой голос не тише фона пропускаем.
                                    # Жёсткий порог гасил бардж-ин: при
                                    # громких динамиках пользователь не
                                    # пробивал baseline и Gemini не слышал
                                    # начало перебивания.
ECHO_BASELINE_HALF:  Final = 0.5    # сек, окно для расчёта фона
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


def _log10_safe(x: float) -> float:
    """log10 для положительного аргумента; при x≤0 возвращает большое
    отрицательное (как «тишина»). Дешевле, чем math.log10 + try/except."""
    if x <= 0:
        return -10.0
    import math  # noqa: PLC0415
    return math.log10(x)


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
        self._last_stats_at  = 0.0

        # Скользящий baseline RMS — используем только пока модель говорит,
        # чтобы отличить «эхо динамиков» от настоящего голоса. Считаем
        # экспоненциальное среднее по полусекундному окну и берём *самый
        # тихий* участок как baseline.
        chunks_per_sec = SEND_SAMPLE_RATE / max(CHUNK_FRAMES, 1)
        self._baseline_window = max(int(chunks_per_sec * ECHO_BASELINE_HALF), 4)
        self._baseline_buf: list[float] = []
        self._baseline_rms: float = 0.0

    # sounddevice вызывает это из аудио-потока
    def _on_audio(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            _log.debug("mic status: %s", status)

        # Базовый гейт: фаза должна разрешать в принципе шевелить микро
        # (то есть не MUTED и не IDLE). SPEAKING тоже разрешён — иначе
        # пользователь не сможет перебить ответ голосом.
        phase = self._state.phase
        if phase in (Phase.IDLE, Phase.MUTED):
            return

        raw = bytes(indata)
        rms = _rms_i16(raw)

        if phase is Phase.SPEAKING:
            # Во время речи модели применяем echo-gate. Baseline — это
            # фоновый шум комнаты + динамика. Голос пользователя обычно
            # минимум +12 dB над этим фоном.
            self._update_baseline(rms)
            if not self._is_voice_above_baseline(rms):
                self.silent += 1
                self._maybe_log_stats()
                return

        payload = {
            "data":      raw,
            "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
        }
        self._loop.call_soon_threadsafe(self._enqueue, payload)

    def _update_baseline(self, rms: float) -> None:
        buf = self._baseline_buf
        buf.append(rms)
        if len(buf) > self._baseline_window:
            buf.pop(0)
        # Baseline = минимум по окну. Тихий конец слова и обычный фон
        # дают примерно одинаковое значение, и это и есть точка отсчёта.
        if buf:
            self._baseline_rms = min(buf)

    def _is_voice_above_baseline(self, rms: float) -> bool:
        # Защита от деления: если baseline ≈ 0, считаем фон неинформативным
        # и сразу пускаем чанк (серверный VAD разберётся).
        if self._baseline_rms < 1.0:
            return True
        ratio_db = 20.0 * _log10_safe(rms / self._baseline_rms)
        return ratio_db >= ECHO_GATE_DB

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
        # Очередь плеера без лимита: модель может прислать ответ
        # одним всплеском, и нам важно проиграть его целиком, а не дропать
        # чанки на пол-фразы. RAM-overhead пренебрежим: 30 сек речи —
        # пара сотен КБ.
        self._inbox: _stdqueue.Queue = _stdqueue.Queue()
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
        # Очередь без лимита — Full сюда не прилетит.
        self._inbox.put_nowait(chunk)
        self._state.on_chunk_enqueued()

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
