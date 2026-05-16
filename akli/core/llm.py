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

        last_err: Exception | None = None
        for attempt in range(1, OR_RETRIES + 2):
            try:
                resp = requests.post(OR_URL, json=payload, headers=headers, timeout=OR_TIMEOUT)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                last_err = e
                _log.warn("openrouter attempt %d failed: %s", attempt, e)
        raise RuntimeError(f"openrouter failed: {last_err}")


def get_provider(config: AppConfig) -> LLMProvider:
    if not config.use_openrouter or not config.openrouter_api_key:
        return _Disabled()
    return _OpenRouter(config.openrouter_api_key, config.openrouter_model)
