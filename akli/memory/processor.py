"""Фоновая обработка закрытых транскриптов.

Live path только пишет transcript. Этот процессор на старте приложения берёт
закрытые ``state/dialogs/*.txt`` и одним LLM-запросом создаёт summary-файл,
а core memory обновляет только если пользователь явно просил что-то запомнить.
"""

from __future__ import annotations

import time
from pathlib import Path

from akli.memory.extract import analyze_session_memory
from akli.memory.store import MemoryStore
from akli.memory.summaries import DialogSummaryStore
from akli.memory.transcript import (
    is_closed,
    list_unprocessed,
    mark_processed,
    read_transcript,
)
from akli.utils.log import get_logger

_log = get_logger("memory.processor")
# Если transcript не закрыт штатно (нет ``# session closed`` хвоста), считаем
# его «возможно ещё активным» и пропускаем, пока с момента последней записи
# не прошло достаточно времени. При штатном закрытии (см. ``is_closed``)
# обрабатываем сразу — пользователь не должен ждать 30 секунд после
# перезапуска приложения, чтобы summary появился.
ACTIVE_TRANSCRIPT_GRACE_SEC = 30.0
PROCESSING_LOCK_STALE_SEC = 20 * 60.0


def _claim_processing(path: Path) -> Path | None:
    lock = path.with_suffix(".processing")
    try:
        with lock.open("x", encoding="utf-8") as f:
            f.write(str(time.time()))
        return lock
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > PROCESSING_LOCK_STALE_SEC:
                lock.unlink(missing_ok=True)
                return _claim_processing(path)
        except Exception:
            pass
        return None
    except Exception as e:
        _log.warn("memory: cannot claim %s: %s", path.name, e)
        return None


async def _process_single(
    path:           Path,
    *,
    memory:         MemoryStore,
    summaries:      DialogSummaryStore,
    gemini_api_key: str,
    openrouter_key: str,
    openrouter_model: str,
    skip_active_check: bool = False,
) -> str:
    """Обработать один transcript. Возвращает статус для логирования.

    ``skip_active_check=True`` для пути «закрыли только что» — тогда мы
    точно знаем, что файл не пишется параллельно, и age-фильтр не нужен.
    """
    lock: Path | None = None
    try:
        if not skip_active_check:
            closed = is_closed(path)
            if not closed:
                age = time.time() - path.stat().st_mtime
                if age < ACTIVE_TRANSCRIPT_GRACE_SEC:
                    _log.info(
                        "memory: skip %s (активен, %.0fs с последней записи)",
                        path.name, age,
                    )
                    return "skipped_active"
                _log.info(
                    "memory: %s не закрыт штатно но %.0fs тишины — обрабатываю как crash",
                    path.name, age,
                )
        if summaries.find_by_source(path.name) is not None:
            _log.info("memory: %s already has summary, mark done", path.name)
            mark_processed(path)
            return "done"
        lock = _claim_processing(path)
        if lock is None:
            _log.debug("memory: %s already being processed", path.name)
            return "locked"
        body = read_transcript(path)
        if len(body.strip()) < 50:
            _log.info("memory: %s слишком короткий (%d chars), помечаю done",
                      path.name, len(body.strip()))
            mark_processed(path)
            return "done"
        _log.info("memory: обработка %s (%d chars)", path.name, len(body))
        data = await analyze_session_memory(
            body,
            gemini_api_key   = gemini_api_key,
            openrouter_key   = openrouter_key,
            openrouter_model = openrouter_model,
        )
        if not data:
            _log.warn("memory: %s — LLM не вернул данные, retry next run", path.name)
            return "retry"
        summary = str(data.get("summary") or "").strip()
        title = str(data.get("title") or "").strip()
        saved: Path | None = None
        if summary:
            saved = summaries.add(title=title, summary=summary, source=path.name)
            if saved is not None:
                _log.info("memory: summary file ← %s", saved.name)
        facts = data.get("core_facts")
        saved_core = isinstance(facts, dict) and bool(facts)
        if saved_core:
            memory.update(facts)
            _log.info("memory: explicit core facts ← %d categories", len(facts))
        if saved is not None or saved_core:
            mark_processed(path)
            return "done"
        _log.warn("memory: %s — empty summary/facts, retry next run", path.name)
        return "retry"
    except Exception as e:
        _log.warn("memory: processor failed on %s: %s", path.name, e)
        return "error"
    finally:
        if lock is not None:
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass


async def process_pending_transcripts(
    *,
    memory:           MemoryStore,
    gemini_api_key:   str = "",
    openrouter_key:   str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> None:
    files = list_unprocessed()
    if not files:
        return
    _log.info("memory: %d необработанных диалогов в очереди", len(files))

    if not gemini_api_key and not openrouter_key:
        _log.warn("memory: ни Gemini, ни OpenRouter ключа — пропускаем обработку")
        return

    summaries = DialogSummaryStore()
    processed = 0
    skipped_active = 0
    for path in files:
        status = await _process_single(
            path,
            memory=memory,
            summaries=summaries,
            gemini_api_key=gemini_api_key,
            openrouter_key=openrouter_key,
            openrouter_model=openrouter_model,
        )
        if status == "done":
            processed += 1
        elif status == "skipped_active":
            skipped_active += 1
    _log.info(
        "memory: pipeline finished — %d обработано, %d пропущено (активные)",
        processed, skipped_active,
    )


async def process_one_transcript(
    path: Path,
    *,
    memory:           MemoryStore,
    gemini_api_key:   str = "",
    openrouter_key:   str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> str:
    """Обработать ровно один transcript — для немедленной обработки после
    reconnect / teardown. Active-check пропускается, потому что зовущий
    гарантирует, что файл уже закрыт."""
    if not gemini_api_key and not openrouter_key:
        _log.warn("memory: ни Gemini, ни OpenRouter ключа — пропускаем %s", path.name)
        return "no_keys"
    summaries = DialogSummaryStore()
    return await _process_single(
        path,
        memory=memory,
        summaries=summaries,
        gemini_api_key=gemini_api_key,
        openrouter_key=openrouter_key,
        openrouter_model=openrouter_model,
        skip_active_check=True,
    )
