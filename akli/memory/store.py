"""Долговременная память — JSON-файл с атомарной записью.

Структура::

    {
      "identity":    {"name":   {"value": "Александр", "ts": "..."}, ...},
      "preferences": {...},
      "notes":       {...}
    }

Никакой LLM здесь — только сериализация. Извлечение фактов лежит
рядом, в :mod:`akli.memory.extract`.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from akli.core.config import LEGACY_MEMORY_FILE, MEMORY_FILE, RECENT_FILE
from akli.utils.log import get_logger

_log = get_logger("memory")

MAX_TOTAL_CHARS = 12_000


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or MEMORY_FILE
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ─────────────────────────────────────────

    def _load(self) -> None:
        # Миграция со старого расположения
        if not self._path.exists() and LEGACY_MEMORY_FILE.exists():
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(
                    LEGACY_MEMORY_FILE.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                _log.info("Мигрирован legacy long_term.json → memory.json")
            except Exception as e:
                _log.warn("legacy memory migration failed: %s", e)

        if not self._path.exists():
            self._data = {}
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except Exception as e:
            _log.warn("memory.json corrupt, reset: %s", e)
            self._data = {}

    def _atomic_save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix = self._path.name + ".",
            suffix = ".tmp",
            dir    = str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # ───────────────────────────── CRUD ──

    def as_dict(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def update(self, patch: dict) -> None:
        """``patch`` имеет ту же структуру, что и data. Сливается deep.

        Ключи, значение которых пустая строка/None, **удаляют** запись.
        """
        if not isinstance(patch, dict) or not patch:
            return
        with self._lock:
            _merge(self._data, patch)
            self._trim_locked()
            try:
                self._atomic_save()
            except Exception as e:
                _log.warn("memory save failed: %s", e)

    def reset(self) -> None:
        with self._lock:
            self._data = {}
            try:
                self._atomic_save()
            except Exception:
                pass

    # ───────────────────────────── формат для промпта ──

    def format_for_prompt(self) -> str:
        with self._lock:
            if not self._data:
                return ""
            lines = ["[LONG-TERM MEMORY]"]
            for cat, items in self._data.items():
                if not items:
                    continue
                lines.append(f"## {cat}")
                if isinstance(items, dict):
                    for key, blob in items.items():
                        val = blob.get("value", "") if isinstance(blob, dict) else str(blob)
                        if val:
                            lines.append(f"- {key}: {val}")
            return "\n".join(lines) + "\n"

    # ───────────────────────────── trimming ──

    def _trim_locked(self) -> None:
        total = sum(
            len(str(v.get("value", "")) if isinstance(v, dict) else v)
            for items in self._data.values() if isinstance(items, dict)
            for v in items.values()
        )
        if total <= MAX_TOTAL_CHARS:
            return
        _log.warn("memory exceeds %d chars (%d), pruning", MAX_TOTAL_CHARS, total)
        # Самый простой эвиктор: удаляем самые старые записи по ``ts``
        flat = []
        for cat, items in self._data.items():
            if isinstance(items, dict):
                for key, blob in items.items():
                    ts = blob.get("ts", "") if isinstance(blob, dict) else ""
                    flat.append((ts, cat, key))
        flat.sort(key=lambda r: r[0])
        for ts, cat, key in flat:
            if total <= MAX_TOTAL_CHARS:
                break
            removed = self._data[cat].pop(key, None)
            if removed:
                size = len(str(removed.get("value", ""))) if isinstance(removed, dict) else 0
                total -= size


class RecentStore:
    """Скользящее окно из последних N session-summary.

    Записывается ``recent.json``::

        {
          "items": [
            {"ts": "2026-05-14T...", "text": "Обсуждали проект X..."},
            ...
          ]
        }
    """

    MAX_ITEMS = 8

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or RECENT_FILE
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = list(data.get("items") or [])
        except Exception as e:
            _log.warn("recent.json corrupt, reset: %s", e)
            self._items = []

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"items": self._items}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def add(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._items.append({
                "ts":   datetime.now().isoformat(timespec="seconds"),
                "text": text,
            })
            # Окно: оставляем только N самых свежих.
            if len(self._items) > self.MAX_ITEMS:
                self._items = self._items[-self.MAX_ITEMS:]
            try:
                self._save_locked()
            except Exception as e:
                _log.warn("recent save failed: %s", e)

    def format_for_prompt(self) -> str:
        with self._lock:
            if not self._items:
                return ""
            lines = ["[RECENT SESSIONS]"]
            for it in self._items[-self.MAX_ITEMS:]:
                ts   = it.get("ts", "")[:10]
                text = it.get("text", "").strip()
                if not text:
                    continue
                lines.append(f"- {ts}: {text}")
            return "\n".join(lines) + "\n"


def _merge(dst: dict, patch: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for k, v in patch.items():
        if v in (None, "", {}):
            dst.pop(k, None)
            continue
        if isinstance(v, dict):
            cur = dst.setdefault(k, {})
            if "value" in v and not any(isinstance(vv, dict) for vv in v.values()):
                cur.update({"value": v["value"], "ts": v.get("ts", now)})
            else:
                _merge(cur, v)
        else:
            dst[k] = {"value": v, "ts": now}
