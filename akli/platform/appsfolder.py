"""Поиск установленных Windows-приложений через ``Get-StartApps``.

Заменяет «нажми Win → набери имя → Enter», который ломается на
RU-раскладке (баг ``B3``). Здесь мы напрямую запрашиваем у Windows
список всех приложений (классических + UWP) и ищем нужное по имени.

Результат кешируется в ``state/appcache.json`` — повторный поиск
мгновенный, PowerShell дёргается только при cache miss или ручном
``refresh()``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from akli.core.config import APPCACHE_FILE
from akli.utils.log import get_logger

_log = get_logger("appsfolder")

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
CACHE_TTL_SEC = 24 * 3600
PS_CMD = (
    "Get-StartApps | "
    "Select-Object Name,AppID | "
    "ConvertTo-Json -Compress"
)


class AppsFolder:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or APPCACHE_FILE
        self._cache: list[dict] = []
        self._loaded_at = 0.0

    def _load_cache(self) -> None:
        if not self._path.exists():
            self._cache = []
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._cache = raw.get("apps", [])
            self._loaded_at = raw.get("ts", 0)
        except Exception:
            self._cache = []
            self._loaded_at = 0

    def _save_cache(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"ts": time.time(), "apps": self._cache},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            _log.warn("appcache save failed: %s", e)

    def refresh(self) -> int:
        if not sys.platform.startswith("win"):
            return 0
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_CMD],
                capture_output=True, text=True, timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                _log.warn("Get-StartApps failed: %s", result.stderr.strip())
                return 0
            data = json.loads(result.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            self._cache = [
                {"name": e.get("Name", ""), "aumid": e.get("AppID", "")}
                for e in data
                if isinstance(e, dict) and e.get("AppID")
            ]
            self._loaded_at = time.time()
            self._save_cache()
            _log.info("appcache refreshed: %d entries", len(self._cache))
            return len(self._cache)
        except Exception as e:
            _log.warn("refresh failed: %s", e)
            return 0

    def find(self, query: str) -> tuple[str, str] | None:
        """Возвращает (name, AUMID) для лучшего совпадения, иначе None."""
        if not self._cache:
            self._load_cache()
            if not self._cache or (time.time() - self._loaded_at) > CACHE_TTL_SEC:
                self.refresh()

        q = query.strip().lower()
        if not q:
            return None

        # точное → начало → подстрока
        best: tuple[str, str] | None = None
        for kind in ("exact", "starts", "contains"):
            for entry in self._cache:
                name = entry.get("name", "")
                aumid = entry.get("aumid", "")
                lower = name.lower()
                hit = (
                    (kind == "exact"    and lower == q) or
                    (kind == "starts"   and lower.startswith(q)) or
                    (kind == "contains" and q in lower)
                )
                if hit and aumid:
                    best = (name, aumid)
                    break
            if best:
                return best
        return None
