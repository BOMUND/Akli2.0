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
from akli.memory.transcript import list_unprocessed, mark_processed, read_transcript
from akli.utils.log import get_logger

_log = get_logger("memory.processor")
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
    for path in files:
        lock: Path | None = None
        try:
            age = time.time() - path.stat().st_mtime
            if age < ACTIVE_TRANSCRIPT_GRACE_SEC:
                _log.debug("memory: skip fresh/active transcript %s", path.name)
                continue
            if summaries.find_by_source(path.name) is not None:
                _log.info("memory: %s already has summary, mark done", path.name)
                mark_processed(path)
                continue
            lock = _claim_processing(path)
            if lock is None:
                _log.debug("memory: %s already being processed", path.name)
                continue

            body = read_transcript(path)
            if len(body.strip()) < 50:
                mark_processed(path)
                continue

            _log.info("memory: обработка %s (%d chars)", path.name, len(body))
            data = await analyze_session_memory(
                body,
                gemini_api_key   = gemini_api_key,
                openrouter_key   = openrouter_key,
                openrouter_model = openrouter_model,
            )
            if not data:
                _log.warn("memory: %s — LLM не вернул данные, retry next run", path.name)
                continue

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
            else:
                _log.warn("memory: %s — empty summary/facts, retry next run", path.name)
        except Exception as e:
            _log.warn("memory: processor failed on %s: %s", path.name, e)
        finally:
            if lock is not None:
                try:
                    lock.unlink(missing_ok=True)
                except Exception:
                    pass
