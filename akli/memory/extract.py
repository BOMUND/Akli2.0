"""Извлечение фактов и краткое summary одной сессии.

Раньше: после каждой реплики дёргали Gemini, упирались в квоту 20/день.
Теперь: один проход на всю сессию. Фоновая задача на старте приложения
проходит по необработанным транскриптам (`memory/transcript.py`),
для каждого делает:

* ``summarize_session()`` — 3-5 строк «о чём говорили», кладётся в
  скользящее окно ``recent.json``;
* ``extract_facts()`` — устойчивые факты, мерж в ``memory.json``.

Провайдер выбирается так: OpenRouter (асинхронно через
``asyncio.to_thread``) если включён и есть ключ; иначе Gemini Flash
Lite; иначе вернём пустоту, ничего не упадёт.
"""

from __future__ import annotations

import asyncio
import json
import re

from akli.utils.log import get_logger

_log = get_logger("memory.extract")

EXTRACT_MODEL = "gemini-2.5-flash-lite"

EXTRACT_PROMPT = """Ты — экстрактор фактов о пользователе для голосового ассистента.
Получаешь транскрипт разговора (одна сессия). Решаешь, что стоит сохранить надолго.

СОХРАНЯЙ устойчивые личные факты:
  identity     — имя, возраст, профессия, город, дата рождения
  preferences  — любимая еда, музыка, спорт, технологии
  notes        — другие повторяемые контексты (рабочий проект, увлечения)

ОБНОВЛЯЙ старые значения: если в разговоре всплыло новое значение
(пользователь поменял работу/город/предпочтения) — верни его как
обычно, под тем же ключом. Старое будет затёрто на стороне store.

УДАЛЯЙ устаревшие факты: если пользователь явно отрицает что-то
(«я уже не работаю в X», «больше не люблю Y») — верни ``"value": ""``
под этим ключом, store удалит запись.

НЕ СОХРАНЯЙ:
  - сиюминутные просьбы («открой ютуб»)
  - вопросы без раскрытия личного
  - случайные шутки / реакции

ЯЗЫК: значения — в языке пользователя.

ВОЗВРАЩАЙ строго JSON без markdown, без преамбулы. Если сохранять
нечего — ``{}``.

Пример:
  {"identity":{"name":{"value":"Александр"}},
   "preferences":{"food":{"value":"суши"}},
   "notes":{"job":{"value":""}}}
"""

SUMMARY_PROMPT = """Ты — конспектор разговоров для голосового ассистента.
Получаешь транскрипт одной сессии. Верни 3-5 строк ОЧЕНЬ кратко:
о чём говорили, что важного было, чем закончилось.

Стиль: один абзац, без bullet-points, без markdown. Простой текст.
Язык: тот же, в котором говорили (если смешано — русский).
Если разговор пустой или ничего не было — верни пустую строку.
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
            contents = f"{prompt}\n\n---\n{body[:8000]}",
        )
        return (resp.text or "").strip()
    except Exception as e:
        msg = str(e)
        if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
            _log.debug("gemini extract skipped (quota): %s", msg[:120])
        else:
            _log.warn("gemini extract failed: %s", e)
        return ""


def _openrouter_call(prompt: str, body: str, *, api_key: str, model: str) -> str:
    if not api_key:
        return ""
    try:
        # Локальный импорт, чтобы не тянуть requests в hot path при отсутствии OR.
        from akli.core.llm import _OpenRouter
        provider = _OpenRouter(api_key, model)
        return provider.chat(body[:8000], system=prompt, max_tokens=512)
    except Exception as e:
        _log.warn("openrouter extract failed: %s", e)
        return ""


# ───────────────────────────── публичный API ──

def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    return text


async def extract_facts(
    transcript: str,
    *,
    gemini_api_key: str = "",
    openrouter_key: str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> dict:
    """Достать факты из транскрипта. Возвращаем dict-patch для MemoryStore."""
    body = (transcript or "").strip()
    if len(body) < 50:
        return {}

    text = ""
    if openrouter_key:
        text = await asyncio.to_thread(
            _openrouter_call, EXTRACT_PROMPT, body,
            api_key=openrouter_key, model=openrouter_model,
        )
    if not text and gemini_api_key:
        text = await asyncio.to_thread(_gemini_call, EXTRACT_PROMPT, body, gemini_api_key)

    text = _strip_code_fence(text)
    if not text or text == "{}":
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _log.warn("extract: bad json (%s): %s", e, text[:200])
        return {}
    return data if isinstance(data, dict) else {}


async def summarize_session(
    transcript: str,
    *,
    gemini_api_key: str = "",
    openrouter_key: str = "",
    openrouter_model: str = "google/gemma-3-27b-it:free",
) -> str:
    """3-5 строк о чём была сессия. Строка (для recent.json)."""
    body = (transcript or "").strip()
    if len(body) < 50:
        return ""

    text = ""
    if openrouter_key:
        text = await asyncio.to_thread(
            _openrouter_call, SUMMARY_PROMPT, body,
            api_key=openrouter_key, model=openrouter_model,
        )
    if not text and gemini_api_key:
        text = await asyncio.to_thread(_gemini_call, SUMMARY_PROMPT, body, gemini_api_key)
    return (text or "").strip()
