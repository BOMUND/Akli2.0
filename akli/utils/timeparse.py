"""Парсер «человеческого» времени.

Поддерживает:

* «через N (секунд|минут|часов)», «через час», «через полчаса» — RU;
* «in N (seconds|minutes|hours)» — EN;
* «завтра в HH:MM», «сегодня в HH», «послезавтра …» — RU + EN;
* «HH:MM», «HH», «3pm», «9 утра», «9 вечера» — RU + EN;
* плюс всё, что заведомо понимает ``dateparser``.

Нужен потому, что раньше парсинг делал LLM и регулярно ошибался
(см. баг ``B5``). Здесь — детерминированный Python.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import dateparser

_RU_UNITS = {
    "сек":   1,
    "секунд": 1,
    "секунды": 1,
    "секунду": 1,
    "мин":   60,
    "минут": 60,
    "минуту": 60,
    "минуты": 60,
    "час":   3600,
    "часов": 3600,
    "часа":  3600,
    "часу":  3600,
    "день":  86400,
    "дня":   86400,
    "дней":  86400,
}
_EN_UNITS = {
    "sec":   1, "secs":   1, "second":   1, "seconds":   1,
    "min":   60, "mins":   60, "minute":   60, "minutes":   60,
    "hr":    3600, "hrs":    3600, "hour":    3600, "hours":    3600,
    "day":   86400, "days":   86400,
}

_RE_THROUGH_RU = re.compile(
    r"через\s+(?:(\d+)\s*)?([а-яА-Я]+)(?:\s+(\d+)\s*([а-яА-Я]+))?",
    re.IGNORECASE,
)
_RE_THROUGH_EN = re.compile(
    r"in\s+(\d+)\s*([a-zA-Z]+)(?:\s+(\d+)\s*([a-zA-Z]+))?",
    re.IGNORECASE,
)
_RE_TOMORROW_RU = re.compile(
    r"(после)?завтра(?:\s+(?:в|на))?\s+(\d{1,2})(?::(\d{2}))?\s*(утра|дня|вечера|ночи)?",
    re.IGNORECASE,
)
_RE_BARE_TIME = re.compile(
    r"^\s*(\d{1,2})[:.](\d{2})\s*$",
)


class TimeParseError(ValueError):
    """Не удалось распарсить."""


def parse(text: str, *, now: datetime | None = None) -> datetime:
    """Преобразовать текст вида «через 5 минут» в абсолютный момент."""
    now = now or datetime.now()
    text = (text or "").strip()
    if not text:
        raise TimeParseError("empty time text")

    # 1) Русский «через …»
    m = _RE_THROUGH_RU.search(text)
    if m:
        seconds = _seconds_from_ru(m)
        if seconds is not None:
            return now + timedelta(seconds=seconds)

    # 2) English "in …"
    m = _RE_THROUGH_EN.search(text)
    if m:
        seconds = _seconds_from_en(m)
        if seconds is not None:
            return now + timedelta(seconds=seconds)

    # 3) «завтра в 9 утра» (dateparser его пропускает)
    m = _RE_TOMORROW_RU.search(text)
    if m:
        days = 2 if m.group(1) else 1
        hh = int(m.group(2))
        mm = int(m.group(3)) if m.group(3) else 0
        modifier = (m.group(4) or "").lower()
        if modifier in ("дня", "вечера") and hh < 12:
            hh += 12
        if modifier == "ночи" and hh >= 12:
            hh -= 12
        target_date = (now + timedelta(days=days)).date()
        return datetime(target_date.year, target_date.month, target_date.day, hh, mm)

    # 4) «15:30» — на сегодня (или завтра, если уже прошло)
    m = _RE_BARE_TIME.match(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand

    # 5) dateparser — общий фолбэк
    try:
        parsed = dateparser.parse(
            text,
            languages=["ru", "en"],
            settings={
                "RELATIVE_BASE":    now,
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
    except Exception:
        parsed = None
    if parsed is not None:
        return parsed

    raise TimeParseError(f"cannot parse time: {text!r}")


def _seconds_from_ru(m: re.Match) -> int | None:
    qty1 = m.group(1)
    unit1 = (m.group(2) or "").lower()
    qty2 = m.group(3)
    unit2 = (m.group(4) or "").lower()

    if unit1 in ("полчаса",):
        seconds = 30 * 60
    elif unit1 == "час" and not qty1:
        seconds = 3600
    elif unit1 in _RU_UNITS:
        n = int(qty1) if qty1 else 1
        seconds = n * _RU_UNITS[unit1]
    else:
        return None

    if qty2 and unit2 in _RU_UNITS:
        seconds += int(qty2) * _RU_UNITS[unit2]
    return seconds


def _seconds_from_en(m: re.Match) -> int | None:
    qty1 = int(m.group(1))
    unit1 = m.group(2).lower()
    if unit1 not in _EN_UNITS:
        return None
    seconds = qty1 * _EN_UNITS[unit1]
    qty2 = m.group(3)
    unit2 = (m.group(4) or "").lower()
    if qty2 and unit2 in _EN_UNITS:
        seconds += int(qty2) * _EN_UNITS[unit2]
    return seconds
