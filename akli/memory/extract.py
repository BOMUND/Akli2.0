"""Post-session memory analysis.

LLM is never called from the hot voice-turn path. This module is used by the
background processor after a transcript is closed or on next app startup.
"""

from __future__ import annotations

import asyncio
import json
import re

from akli.utils.log import get_logger

_log = get_logger("memory.extract")

EXTRACT_MODEL = "gemini-2.5-flash-lite"

SESSION_MEMORY_PROMPT = """Ты — обработчик памяти голосового ассистента.
Получаешь транскрипт одной завершённой сессии.

Верни строго ровно JSON объект, без пояснений, без markdown, без ```:
{
  "title": "короткое название диалога 3-8 слов",
  "summary": "полезное краткое summary диалога без мусора",
  "core_facts": {
    "identity": {"key": {"value": "..."}},
    "preferences": {"key": {"value": "..."}},
    "notes": {"key": {"value": "..."}}
  }
}

Правила:
- summary: 4-10 предложений. Сохраняй смысл, решения, важный контекст и итоги.
- core_facts: заполняй ТОЛЬКО если пользователь явно просил запомнить/сохранить
  факт (например: "запомни", "сохрани в память", "не забудь").
- Не сохраняй в core_facts случайные команды, временные просьбы, эмоции, шутки,
  обычный ход диалога и факты, которые пользователь НЕ просил запомнить.
- ДЛЯ УДАЛЕНИЯ ФАКТА: если пользователь явно попросил удалить/забыть факт—
  верни этот ключ с выбором из этих вариантов:
    {"notes":{"old_fact":{"value":""}}}             ← пустая строка
    {"notes":{"old_fact":null}}                       ← null
  НЕ пиши слово DELETE в value и не оставляй ключ с любым непустым текстом
  вместо самого факта — он будет сохранён как валидный новый факт.
- Если сохранять/удалять в core_facts нечего — верни пустой объект {}.
- Язык title/summary/value — язык пользователя, обычно русский.
"""


# ───────────────────────────── провайдеры ──

def _gemini_call(prompt: str, body: str, api_key: str) -> str:
    if not api_key:
        return ""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model    = EXTRACT_MODEL,
            contents = f"{prompt}\n\n---\n{body[:12000]}",
        )
        return (resp.text or "").strip()
    except Exception as e:
        msg = str(e)
        if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
            _log.debug("gemini memory analysis skipped (quota): %s", msg[:120])
        else:
            _log.warn("gemini memory analysis failed: %s", e)
        return ""


def _openrouter_call(prompt: str, body: str, *, api_key: str, model: str) -> str:
    if not api_key:
        return ""
    try:
        from akli.core.llm import _OpenRouter
        provider = _OpenRouter(api_key, model)
        return provider.chat(body[:12000], system=prompt, max_tokens=900)
    except Exception as e:
        _log.warn("openrouter memory analysis failed: %s", e)
        return ""


# ───────────────────────────── публичный API ──

def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    return text


async def _try_provider(
    call, *, label: str, api_key: str, model: str, body: str,
) -> dict | None:
    """Дёрнуть LLM-провайдера и попробовать спарсить JSON.

    Возвращает dict при успехе, ``None`` при любой проблеме (пустой
    ответ / мусорный JSON / исключение в сети). Вызывающий код решает,
    пробовать ли следующего провайдера.
    """
    if label == "openrouter":
        text = await asyncio.to_thread(
            call, SESSION_MEMORY_PROMPT, body,
            api_key=api_key, model=model,
        )
    else:
        text = await asyncio.to_thread(call, SESSION_MEMORY_PROMPT, body, api_key)
    text = _strip_code_fence(text)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _log.warn("%s analysis: bad json (%s): %s", label, e, text[:300])
        return None
    if not isinstance(data, dict):
        _log.warn("%s analysis: not a json object", label)
        return None
    return data


async def analyze_session_memory(
    transcript: str,
    *,
    gemini_api_key: str = "",
    openrouter_key: str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> dict:
    body = (transcript or "").strip()
    if len(body) < 50:
        return {}

    # OpenRouter — приоритетный провайдер: у Gemini free-tier лимит 20/день,
    # его лучше беречь под голосовую сессию. OpenRouter (gemma:free и т.п.)
    # ходит асинхронно через ``asyncio.to_thread`` и не упирается в ту же
    # квоту. Если OpenRouter недоступен / квота кончилась / вернул мусорный
    # JSON — fallback на Gemini.
    data: dict | None = None
    if openrouter_key:
        data = await _try_provider(
            _openrouter_call,
            label="openrouter",
            api_key=openrouter_key,
            model=openrouter_model,
            body=body,
        )
    if data is None and gemini_api_key:
        data = await _try_provider(
            _gemini_call,
            label="gemini",
            api_key=gemini_api_key,
            model="",
            body=body,
        )
    if data is None:
        return {}
    if not isinstance(data, dict):
        return {}
    core = data.get("core_facts")
    if not isinstance(core, dict):
        data["core_facts"] = {}
    for key in ("title", "summary"):
        if not isinstance(data.get(key), str):
            data[key] = ""
    return data


async def extract_facts(
    transcript: str,
    *,
    gemini_api_key: str = "",
    openrouter_key: str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> dict:
    data = await analyze_session_memory(
        transcript,
        gemini_api_key=gemini_api_key,
        openrouter_key=openrouter_key,
        openrouter_model=openrouter_model,
    )
    return data.get("core_facts", {}) if isinstance(data, dict) else {}


async def summarize_session(
    transcript: str,
    *,
    gemini_api_key: str = "",
    openrouter_key: str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> str:
    data = await analyze_session_memory(
        transcript,
        gemini_api_key=gemini_api_key,
        openrouter_key=openrouter_key,
        openrouter_model=openrouter_model,
    )
    return str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
