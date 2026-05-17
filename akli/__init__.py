"""Akli 2.0 — голосовой ассистент на базе Gemini Live API.

Package layout::

    akli.core.app         — точка входа: запуск Qt + LiveSession
    akli.core.config      — конфигурация (config/akli.json)
    akli.core.llm         — единый интерфейс LLM (Gemini, опц. OpenRouter)
    akli.live.*           — рантайм Gemini Live (state, audio, session)
    akli.tools.*          — инструменты (apps, scheduler, web, files, ...)
    akli.memory.*         — долговременная память
    akli.ui.*             — PyQt6 интерфейс
    akli.utils.*          — лог, парсер времени, ввод текста, layout
"""

__version__ = "2.0.0"
