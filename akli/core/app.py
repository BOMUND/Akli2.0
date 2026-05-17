"""Точка входа Akli 2.0.

Запуск:

* ``python -m akli`` — рекомендуемый способ;
* ``akli`` — после ``pip install .`` (см. ``pyproject.toml``).

Здесь только:

1. DPI-awareness (важно вызвать до ``QApplication``, фикс ``B4``).
2. Инициализация Qt-приложения через :class:`AkliApp`.
3. ``return exit_code``.

Всё остальное живёт в подмодулях.
"""

from __future__ import annotations

import sys

from akli.platform.dpi import ensure_dpi_awareness
from akli.utils.log import get_logger

_log = get_logger("app")


def main() -> int:
    ensure_dpi_awareness()
    try:
        from akli.ui.app import AkliApp
    except Exception as e:
        _log.error("failed to import UI: %s", e)
        return 2

    try:
        return AkliApp().run()
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _log.error("fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
