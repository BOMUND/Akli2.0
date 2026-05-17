"""Архив summary прошлых диалогов.

Core memory остаётся маленьким JSON-профилем пользователя. Этот модуль хранит
длинную историю отдельно: один markdown-файл на обработанный диалог.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from akli.core.config import DIALOG_SUMMARIES_DIR
from akli.utils.log import get_logger

_log = get_logger("memory.summaries")

MAX_SUMMARY_CHARS = 16_000
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
SPACE_CHARS = re.compile(r"\s+")


@dataclass(frozen=True)
class SummaryInfo:
    name: str
    title: str
    modified: str
    size: int


class DialogSummaryStore:
    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or DIALOG_SUMMARIES_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def add(self, *, title: str, summary: str, source: str = "") -> Path | None:
        summary = (summary or "").strip()
        if len(summary) < 20:
            return None
        existing = self.find_by_source(source)
        if existing is not None:
            return existing
        title = _clean_title(title) or _derive_title(summary)
        ts = _source_time_prefix(source) or datetime.now().strftime("%Y-%m-%d_%H-%M")
        stem = f"{ts}_{_slug(title)}"
        path = self._dir / f"{stem}.md"
        suffix = 2
        while path.exists():
            path = self._dir / f"{stem}_{suffix}.md"
            suffix += 1
        body = _format_body(title=title, summary=summary, source=source)
        _atomic_write(path, body)
        _log.info("dialog summary saved: %s", path.name)
        return path

    def find_by_source(self, source: str) -> Path | None:
        safe_source = Path(source).name if source else ""
        if not safe_source:
            return None
        marker = f"Source: {safe_source}"
        for path in self._dir.glob("*.md"):
            try:
                if marker in path.read_text(encoding="utf-8"):
                    return path
            except Exception:
                continue
        return None

    def list(self, *, query: str = "", limit: int = 0) -> list[SummaryInfo]:
        query_norm = query.strip().casefold()
        paths = sorted(
            self._dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
            reverse=True,
        )
        items: list[SummaryInfo] = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            title = _read_title(text) or path.stem
            haystack = f"{path.name}\n{title}\n{text[:1000]}".casefold()
            if query_norm and query_norm not in haystack:
                continue
            items.append(SummaryInfo(
                name=path.name,
                title=title,
                modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                size=path.stat().st_size,
            ))
            if limit > 0 and len(items) >= min(limit, 200):
                break
        return items

    def read(self, name: str) -> str:
        safe = Path(name).name
        if not safe or safe != name:
            raise ValueError("Use only the summary file name returned by memory_list_summaries.")
        path = self._dir / safe
        if not path.exists() or path.suffix.lower() != ".md":
            raise FileNotFoundError(f"Summary not found: {safe}")
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > MAX_SUMMARY_CHARS:
            return text[:MAX_SUMMARY_CHARS] + "\n\n[truncated]"
        return text


def _format_body(*, title: str, summary: str, source: str) -> str:
    lines = [f"# {title}", "", f"Date: {datetime.now().isoformat(timespec='seconds')}"]
    if source:
        lines.append(f"Source: {Path(source).name}")
    lines.extend(["", summary.strip(), ""])
    return "\n".join(lines)


def _read_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _clean_title(title: str) -> str:
    title = SPACE_CHARS.sub(" ", (title or "").strip())
    title = INVALID_FILENAME_CHARS.sub(" ", title)
    return title[:80].strip(" ._-")


def _derive_title(summary: str) -> str:
    first = SPACE_CHARS.sub(" ", summary.strip()).split(".", 1)[0]
    return _clean_title(first[:80]) or "dialog"


def _slug(title: str) -> str:
    slug = _clean_title(title).lower()
    slug = SPACE_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-._ ")
    return slug[:60] or "dialog"


def _source_time_prefix(source: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})", Path(source).name)
    if not match:
        return ""
    return f"{match.group(1)}_{match.group(2)}-{match.group(3)}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
