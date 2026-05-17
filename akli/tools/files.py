"""Tool ``file_controller`` — операции с файлами.

Безопасные операции: read/write/list/delete/move/copy/mkdir. Никакого
кода для исполнения процессов. Пути валидируются — отклоняем выход за
пределы заранее разрешённых корней (по умолчанию: домашняя директория
пользователя + ``Desktop`` + ``Downloads`` + ``Documents``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger

_log = get_logger("tools.files")

try:
    from send2trash import send2trash
    _HAS_TRASH = True
except ImportError:
    _HAS_TRASH = False


def _allowed_roots() -> list[Path]:
    home = Path.home()
    extras = []
    for sub in ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos"):
        p = home / sub
        if p.exists():
            extras.append(p)
    return [home, *extras]


_ALLOWED = _allowed_roots()


def _normalize(raw: str) -> Path:
    """Раскрытие user-shortcut'ов и проверка границ."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty path")

    # «рабочий стол», «desktop», «загрузки», «downloads» — алиасы для корней.
    # ВНИМАНИЕ: алиасы возвращаются без прохождения через проверку _ALLOWED.
    # Это безопасно, потому что все цели — поддиректории ``Path.home()``,
    # которая всегда в _ALLOWED. При добавлении нового алиаса, указывающего
    # за пределы home (например ``/tmp``), обязательно прогнать его через
    # ту же проверку границ, что и обычные пути.
    low = text.lower()
    aliases = {
        "рабочий стол":  Path.home() / "Desktop",
        "desktop":       Path.home() / "Desktop",
        "загрузки":      Path.home() / "Downloads",
        "downloads":     Path.home() / "Downloads",
        "документы":     Path.home() / "Documents",
        "documents":     Path.home() / "Documents",
    }
    for k, p in aliases.items():
        if low == k:
            return p

    candidate = Path(os.path.expandvars(os.path.expanduser(text))).resolve()
    for root in _ALLOWED:
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    raise PermissionError(f"path outside allowed roots: {candidate}")


async def _run(params: dict, ctx: ToolContext) -> str:
    operation = (params.get("operation") or "").strip().lower()
    target = params.get("path") or params.get("destination") or "?"
    ctx.heartbeat(f"files: {operation} {target}")
    return await asyncio.to_thread(_dispatch, operation, params, ctx)


def _dispatch(op: str, params: dict, ctx: ToolContext) -> str:
    try:
        if op == "list":
            return _list(params)
        if op == "read":
            return _read(params)
        if op == "write":
            return _write(params)
        if op == "append":
            return _write(params, append=True)
        if op in ("delete", "remove"):
            return _delete(params)
        if op == "mkdir":
            return _mkdir(params)
        if op == "move":
            return _move(params)
        if op == "copy":
            return _copy(params)
        if op == "exists":
            return _exists(params)
        return f"Unknown operation '{op}'. Use list/read/write/append/delete/mkdir/move/copy/exists."
    except PermissionError as e:
        return f"Refused: {e}"
    except Exception as e:
        return f"File error: {e}"


def _list(params: dict) -> str:
    p = _normalize(params.get("path", ""))
    if not p.exists():
        return f"Not found: {p}"
    if not p.is_dir():
        return f"Not a directory: {p}"
    entries = []
    for child in sorted(p.iterdir()):
        kind = "D" if child.is_dir() else "F"
        entries.append(f"  {kind}  {child.name}")
    return f"Listing {p}:\n" + ("\n".join(entries) if entries else "  (empty)")


def _read(params: dict) -> str:
    p = _normalize(params.get("path", ""))
    if not p.is_file():
        return f"Not a file: {p}"
    if p.stat().st_size > 200_000:
        return f"File too large to read in one shot ({p.stat().st_size} bytes)."
    return p.read_text(encoding="utf-8", errors="replace")


def _write(params: dict, *, append: bool = False) -> str:
    p = _normalize(params.get("path", ""))
    content = params.get("content", "")
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(p, mode, encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} chars to {p.name}."


def _delete(params: dict) -> str:
    p = _normalize(params.get("path", ""))
    if not p.exists():
        return f"Already absent: {p}"
    if _HAS_TRASH:
        send2trash(str(p))
        return f"Moved to trash: {p.name}"
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"Deleted: {p.name}"


def _mkdir(params: dict) -> str:
    p = _normalize(params.get("path", ""))
    p.mkdir(parents=True, exist_ok=True)
    return f"Directory ready: {p}"


def _move(params: dict) -> str:
    src = _normalize(params.get("path", ""))
    dst = _normalize(params.get("destination", ""))
    shutil.move(str(src), str(dst))
    return f"Moved {src.name} → {dst}"


def _copy(params: dict) -> str:
    src = _normalize(params.get("path", ""))
    dst = _normalize(params.get("destination", ""))
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return f"Copied {src.name} → {dst}"


def _exists(params: dict) -> str:
    p = _normalize(params.get("path", ""))
    return f"{'Yes' if p.exists() else 'No'}: {p}"


SPEC = make_spec(
    name        = "file_controller",
    description = "Safe file operations within the user's home folder.",
    parameters  = {
        "operation": {
            "type":        "string",
            "description": "One of: list, read, write, append, delete, mkdir, move, copy, exists.",
        },
        "path":        {"type": "string", "description": "File or folder path. Supports aliases: 'desktop', 'рабочий стол', 'downloads', ..."},
        "content":     {"type": "string", "description": "Content for write/append."},
        "destination": {"type": "string", "description": "Destination for move/copy."},
    },
    required = ["operation", "path"],
    run      = _run,
)
