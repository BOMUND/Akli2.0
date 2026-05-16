"""Конфигурация Akli.

Один JSON-файл ``config/akli.json`` — единственный источник правды для:

* ключа Gemini (обязательный);
* опциональной OpenRouter-интеграции (выключена по умолчанию);
* выбранной целевой ОС (нужно только для подсветки в UI).

Старый ``config/api_keys.json`` поддерживается как fallback на чтение —
если он есть, при первом запуске значения копируются в новый файл.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

from akli.utils.log import get_logger

_log = get_logger("config")


def _base_dir() -> Path:
    """Корень проекта (либо рядом с exe в frozen-режиме)."""
    if bool(sys.__dict__.get("frozen", False)):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = _base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "akli.json"
LEGACY_FILE = CONFIG_DIR / "api_keys.json"
PROMPT_FILE = BASE_DIR / "prompt.txt"
STATE_DIR   = BASE_DIR / "state"
MEMORY_FILE = STATE_DIR / "memory.json"
REMINDERS_FILE = STATE_DIR / "reminders.json"
APPCACHE_FILE  = STATE_DIR / "appcache.json"
# Память: сырые транскрипты сессий (необработанные диалоги) и скользящее
# окно summary прошлых сессий. Хранится прямо в STATE_DIR, не удаляется
# никогда — текст по 50 КБ за час разговора, диск выдержит.
DIALOGS_DIR = STATE_DIR / "dialogs"
RECENT_FILE = STATE_DIR / "recent.json"

LEGACY_PROMPT_FILE = BASE_DIR / "core" / "prompt.txt"
LEGACY_MEMORY_FILE = BASE_DIR / "memory" / "long_term.json"


@dataclass
class AppConfig:
    gemini_api_key:     str  = ""
    openrouter_api_key: str  = ""
    use_openrouter:     bool = False
    openrouter_model:   str  = "google/gemma-3-27b-it:free"
    os_system:          str  = ""   # "windows" | "mac" | "linux"
    # Модель Gemini Live. Оставляем «native-audio-preview-12-2025» — другие
    # варианты либо выключены (вариант *-2.5-flash-preview, см.
    # livekit/agents#4414), либо хуже по голосу. Предыдущие жалобы на
    # эту модель оказались багом на нашей стороне (см. фикс в _recv_loop).
    gemini_live_model:  str  = "gemini-2.5-flash-native-audio-preview-12-2025"

    def is_ready(self) -> bool:
        return bool(self.gemini_api_key) and bool(self.os_system)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# Модели, которые были в предыдущих версиях как дефолт, но больше не работают
# на v1beta. При загрузке старого конфига эти значения заменяем на дефолт.
_BROKEN_LIVE_MODELS = {
    "gemini-live-2.5-flash-preview",        # отключён в dev API
    "gemini-2.0-flash-live-001",            # рабочий, но хуже по голосу
}


def load() -> AppConfig:
    """Читает текущий конфиг. Если его нет — пытается мигрировать со старого."""
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = AppConfig(**{k: raw.get(k, v) for k, v in asdict(AppConfig()).items()})
            if cfg.gemini_live_model in _BROKEN_LIVE_MODELS:
                _log.info("Модель %s больше не доступна, перехожу на %s",
                          cfg.gemini_live_model, AppConfig().gemini_live_model)
                cfg.gemini_live_model = AppConfig().gemini_live_model
                save(cfg)
            return cfg
        except Exception as e:
            _log.warn("akli.json повреждён, использую дефолты: %s", e)

    if LEGACY_FILE.exists():
        try:
            raw = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
            cfg = AppConfig(
                gemini_api_key     = raw.get("gemini_api_key", ""),
                openrouter_api_key = raw.get("openrouter_api_key", ""),
                use_openrouter     = bool(raw.get("openrouter_api_key", "")),
                os_system          = raw.get("os_system", ""),
            )
            _log.info("Мигрирован legacy api_keys.json → akli.json")
            save(cfg)
            return cfg
        except Exception as e:
            _log.warn("api_keys.json повреждён: %s", e)

    return AppConfig()


def save(cfg: AppConfig) -> None:
    payload = json.dumps(asdict(cfg), indent=2, ensure_ascii=False)
    _atomic_write(CONFIG_FILE, payload)
    _log.info("Конфиг сохранён → %s", CONFIG_FILE)


def load_prompt() -> str:
    """Системный промпт. Принимает новое расположение и старое (для миграции)."""
    for path in (PROMPT_FILE, LEGACY_PROMPT_FILE):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return "You are Akli, a fast and direct voice assistant."
