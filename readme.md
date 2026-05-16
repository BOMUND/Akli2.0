# Akli 2.0

Голосовой ассистент на основе Gemini Live API.

## Запуск

```powershell
# Установка (один раз)
python -m pip install -r requirements.txt
python -m playwright install chromium

# Запуск
python -m akli
# или совместимый старый запуск
python main.py
```

При первом запуске откроется диалог, который попросит ключ Gemini API.
Конфиг сохраняется в `config/akli.json`. OpenRouter опционален и
выключен по умолчанию.

## Архитектура

```
akli/
├── core/
│   ├── app.py        — точка входа (Qt + LiveSession)
│   ├── config.py     — config/akli.json
│   └── llm.py        — опц. OpenRouter (text fallback)
├── live/             — рантайм Gemini Live
│   ├── state.py      — фазы (LISTENING / SPEAKING / TOOL / ...) + watchdog
│   ├── audio.py      — микрофон / плеер
│   └── session.py    — WebSocket + reconnect + tool dispatch
├── tools/            — инструменты
│   ├── apps.py       — open_app (AppsFolder lookup)
│   ├── keys.py       — type_text (clipboard paste, любой язык)
│   ├── scheduler.py  — remind (threading.Timer + parse естественного времени)
│   ├── files.py      — read/write/list/...
│   ├── web.py        — DuckDuckGo + Gemini grounded fallback
│   ├── browser.py    — Playwright (5 операций)
│   ├── settings.py   — громкость / lock / screenshot
│   └── stubs.py      — file_processor / dev_agent (declared, disabled)
├── memory/
│   ├── store.py      — state/memory.json (atomic, trim)
│   └── extract.py    — извлечение фактов одним вызовом Gemini
├── ui/
│   ├── app.py        — связка Qt ↔ LiveSession
│   ├── window.py     — главное окно
│   ├── setup.py      — стартовый диалог
│   ├── hud.py        — HUD-кружок (фазу видно)
│   └── theme.py      — палитра и QSS
├── platform/
│   ├── dpi.py        — DPI awareness ДО QApplication
│   ├── appsfolder.py — Get-StartApps кэш
│   └── layout.py     — переключение раскладки (fallback)
└── utils/
    ├── log.py        — структурный логгер
    ├── timeparse.py  — «через 5 минут», «tomorrow at 9am»
    └── textinput.py  — clipboard paste
```

Конфигурация и данные:

* `config/akli.json` — ключи и предпочтения.
* `state/memory.json` — долговременная память.
* `state/reminders.json` — отложенные напоминания.
* `state/appcache.json` — кэш установленных приложений.
* `prompt.txt` — системный промпт.

## Платформа

MVP таргетит Windows 10. На macOS/Linux часть инструментов работает
ограниченно (нет DPI, AppsFolder, win10toast).

## Voice

По умолчанию `Puck` (мужской). Сменить — в `akli/live/session.py`.
