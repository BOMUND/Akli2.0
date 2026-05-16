"""Фоновая обработка неразобранных транскриптов.

Запускается на старте приложения. Находит все ``state/dialogs/*.txt``
без сиблинга ``.done``, для каждого делает:

1. ``summarize_session`` → строка → ``recent.json`` (скользящее окно).
2. ``extract_facts`` → dict-patch → ``memory.json`` (merge).
3. Помечает файл как ``.done``.

Любая ошибка по конкретному файлу — лог + пропуск (файл останется
без ``.done`` и попробуем ещё раз на следующем запуске).
"""

from __future__ import annotations

import asyncio
import time

from akli.memory.extract import extract_facts, summarize_session
from akli.memory.store import MemoryStore, RecentStore
from akli.memory.transcript import list_unprocessed, mark_processed, read_transcript
from akli.utils.log import get_logger

_log = get_logger("memory.processor")
ACTIVE_TRANSCRIPT_GRACE_SEC = 30.0


async def process_pending_transcripts(
    *,
    memory:           MemoryStore,
    recent:           RecentStore,
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

    for path in files:
        try:
            age = time.time() - path.stat().st_mtime
            if age < ACTIVE_TRANSCRIPT_GRACE_SEC:
                _log.debug("memory: skip fresh/active transcript %s", path.name)
                continue
            body = read_transcript(path)
            if len(body.strip()) < 50:
                # Пустой/короткий — нет смысла дёргать LLM, сразу помечаем done.
                mark_processed(path)
                continue

            _log.info("memory: обработка %s (%d chars)", path.name, len(body))

            # summary + extract — параллельно, оба запроса асинхронные.
            summary_task = asyncio.create_task(summarize_session(
                body,
                gemini_api_key   = gemini_api_key,
                openrouter_key   = openrouter_key,
                openrouter_model = openrouter_model,
            ))
            facts_task = asyncio.create_task(extract_facts(
                body,
                gemini_api_key   = gemini_api_key,
                openrouter_key   = openrouter_key,
                openrouter_model = openrouter_model,
            ))
            summary, facts = await asyncio.gather(
                summary_task, facts_task, return_exceptions=True,
            )

            if isinstance(summary, str) and summary:
                recent.add(summary)
                _log.info("memory: recent ← %d chars", len(summary))
            elif isinstance(summary, Exception):
                _log.warn("memory: summary failed for %s: %s", path.name, summary)

            if isinstance(facts, dict) and facts:
                memory.update(facts)
                _log.info("memory: facts ← %d categories", len(facts))
            elif isinstance(facts, Exception):
                _log.warn("memory: extract failed for %s: %s", path.name, facts)

            # Помечаем как done только если хотя бы один из двух запросов
            # вернул осмысленный результат. Если оба упали с исключением
            # (например, нет интернета прямо сейчас) — оставляем без
            # .done, попробуем на следующем запуске.
            both_failed = (
                isinstance(summary, Exception) and isinstance(facts, Exception)
            )
            if both_failed:
                _log.warn("memory: %s — обе LLM-операции упали, retry next run",
                          path.name)
            else:
                mark_processed(path)
        except Exception as e:
            _log.warn("memory: processor failed on %s: %s", path.name, e)
