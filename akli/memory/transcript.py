"""Append-only логи диалогов с ассистентом.

Каждая сессия — один файл ``state/dialogs/<ts>_<id>.txt``. Все реплики
ассистента (а потом, когда вернём input-transcription, и пользователя)
тут же дописываются на диск. Если приложение упадёт — мы ничего не
потеряем: на следующем запуске фоновая задача увидит файл без
``.done`` маркера и обработает его (summary + extract).

Никаких лимитов на размер: 1 час голосового разговора = ~30 КБ текста.
Полгода ежедневных бесед — десятки мегабайт.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path

from akli.core.config import DIALOGS_DIR
from akli.utils.log import get_logger

_log = get_logger("memory.transcript")


def _new_path() -> Path:
    ts  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    sid = uuid.uuid4().hex[:8]
    return DIALOGS_DIR / f"{ts}_{sid}.txt"


class Transcript:
    """Один файл = одна сессия. Запись потокобезопасная."""

    def __init__(self, path: Path | None = None) -> None:
        DIALOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._path: Path = path or _new_path()
        self._lock = threading.Lock()
        # Шапка: ts + uname (полезно при разборе).
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(f"# session started {datetime.now().isoformat()}\n")
        except Exception as e:
            _log.warn("transcript open failed: %s", e)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        line = f"{datetime.now().strftime('%H:%M:%S')}  {role.upper():<6}  {text}\n"
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                _log.warn("transcript write failed: %s", e)

    def append_user(self, text: str) -> None:
        self.append("user", text)

    def append_assistant(self, text: str) -> None:
        self.append("akli", text)

    def close(self) -> None:
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(f"# session closed {datetime.now().isoformat()}\n")
            except Exception:
                pass


# ───────────────────────────── batch processing ──

def list_unprocessed() -> list[Path]:
    """Все ``<ts>_<id>.txt`` без сиблинга ``<ts>_<id>.done``.

    Возвращаются отсортированными по имени (= хронологически), чтобы при
    обработке мы сначала разобрали самые старые сессии.
    """
    if not DIALOGS_DIR.exists():
        return []
    out: list[Path] = []
    for p in sorted(DIALOGS_DIR.glob("*.txt")):
        if p.with_suffix(".done").exists():
            continue
        out.append(p)
    return out


def mark_processed(path: Path) -> None:
    try:
        path.with_suffix(".done").write_text(
            datetime.now().isoformat(),
            encoding="utf-8",
        )
    except Exception as e:
        _log.warn("mark_processed failed for %s: %s", path.name, e)


def read_transcript(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        _log.warn("read_transcript failed for %s: %s", path.name, e)
        return ""
