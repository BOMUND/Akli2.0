"""Опциональный текстовый LLM-провайдер.

По умолчанию выключен (``config.use_openrouter = False``) — никаких HTTP
запросов, никаких 404-спамов. Включается флагом, использует одну
выбранную модель (а не цикл по 22 «free»-моделям, который никогда не
работал).

Используется только когда какой-то инструмент явно попросит текстовую
генерацию (например, ``web_search`` в режиме сравнения). Голос/память
идут через Gemini напрямую.
"""

from __future__ import annotations

from typing import Protocol

import requests

from akli.core.config import AppConfig
from akli.utils.log import get_logger

_log = get_logger("llm")

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_TIMEOUT = 30
OR_RETRIES = 1


class LLMProvider(Protocol):
    def chat(self, user: str, *, system: str = "", max_tokens: int = 512) -> str: ...


class _Disabled:
    def chat(self, user: str, *, system: str = "", max_tokens: int = 512) -> str:
        raise RuntimeError("text LLM is disabled (config.use_openrouter=false)")


class _OpenRouter:
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def chat(self, user: str, *, system: str = "", max_tokens: int = 512) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload = {
            "model":      self._model,
            "messages":   messages,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
            "X-Title":       "Akli 2.0",
        }

        _log.info("openrouter: chat → %s (~%d chars)", self._model, len(user))
        last_err: Exception | None = None
        for attempt in range(1, OR_RETRIES + 2):
            try:
                resp = requests.post(OR_URL, json=payload, headers=headers, timeout=OR_TIMEOUT)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                # OpenRouter иногда отдаёт ``message.content = null`` (особенно
                # free-tier модели, которые молча отказали в генерации). Без
                # явной проверки мы получали ``'NoneType' object has no attribute 'strip'``
                # — это выглядело в логах как «attempt N failed» и весь call
                # уходил в retry, хотя ответ просто пустой.
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    raise RuntimeError(f"empty choices: {str(data)[:200]}")
                msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = (msg or {}).get("content") if isinstance(msg, dict) else None
                reply = (content or "").strip()
                if not reply:
                    raise RuntimeError("empty content in response")
                _log.info("openrouter: chat ✓ ответ %d chars", len(reply))
                return reply
            except Exception as e:
                last_err = e
                _log.warn("openrouter attempt %d failed: %s", attempt, e)
        raise RuntimeError(f"openrouter failed: {last_err}")


def get_provider(config: AppConfig) -> LLMProvider:
    if not config.use_openrouter:
        _log.info("openrouter: выключен (use_openrouter=false)")
        return _Disabled()
    if not config.openrouter_api_key:
        _log.warn("openrouter: включён, но ключ пуст — провайдер недоступен")
        return _Disabled()
    _log.info(
        "openrouter: активен, model=%s (используется только как fallback "
        "в текстовых тулзах; голос идёт через Gemini Live)",
        config.openrouter_model,
    )
    return _OpenRouter(config.openrouter_api_key, config.openrouter_model)
