"""Извлечение фактов из одной пары реплик.

Раньше memory_manager делал **два** прохода через OpenRouter (фильтр +
экстракт). Здесь — один синхронный вызов Gemini, который сам решает,
есть ли что-то достойное сохранения, и сразу возвращает JSON.

Возвращаем структуру под :meth:`MemoryStore.update`.
"""

from __future__ import annotations

import asyncio
import json
import re

from akli.utils.log import get_logger

_log = get_logger("memory.extract")

EXTRACT_MODEL = "gemini-2.5-flash-lite"

PROMPT = """Ты — экстрактор фактов о пользователе для голосового ассистента.
Получаешь одну пару реплик «пользователь → ассистент» и решаешь, стоит ли
сохранить что-то надолго.

СОХРАНЯЙ только устойчивые личные факты:
  identity     — имя, возраст, профессия, город, дата рождения
  preferences  — любимая еда, музыка, спорт, технологии
  notes        — другие повторяемые контексты (например, рабочий проект)

НЕ СОХРАНЯЙ:
  - сиюминутные просьбы («открой ютуб»)
  - вопросы пользователя без раскрытия личного
  - случайные шутки / реакции

ЯЗЫК: сохраняй значения в **том же языке**, в котором пользователь их назвал.
То есть «Меня зовут Александр» → {"identity":{"name":{"value":"Александр"}}}.

ВОЗВРАЩАЙ строго JSON без markdown, без преамбулы. Если сохранять нечего —
возвращай {} (пустой объект).
"""


def extract_facts(user_text: str, model_text: str, api_key: str) -> dict:
    """Синхронный вариант (тесты / скрипты)."""
    if not api_key:
        return {}
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"{PROMPT}\n\n"
            f"User: {user_text[:500]}\n"
            f"Assistant: {model_text[:1000]}"
        )
        resp = client.models.generate_content(
            model    = EXTRACT_MODEL,
            contents = prompt,
        )
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?", "", text)
        text = re.sub(r"```$", "", text).strip()
        if not text or text == "{}":
            return {}
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        msg = str(e)
        # 429 RESOURCE_EXHAUSTED — упёрлись в free-tier лимит дневных
        # запросов на gemini-2.5-flash-lite. Это не баг кода, а особенность
        # квоты; молча скипаем, чтобы не спамить активити-логом.
        if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
            _log.debug("extract skipped (quota): %s", msg[:120])
        else:
            _log.warn("extract failed: %s", e)
        return {}


async def extract_facts_async(*, user_text: str, model_text: str, api_key: str) -> dict:
    return await asyncio.to_thread(extract_facts, user_text, model_text, api_key)
