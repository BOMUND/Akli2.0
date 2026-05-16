"""Цвета и шрифты. Один файл вместо разбросанных по UI констант."""

from __future__ import annotations

from dataclasses import dataclass

from akli.live.state import Phase


@dataclass(frozen=True)
class Palette:
    bg_window:    str = "#0a0d12"
    bg_panel:     str = "#10151c"
    bg_input:     str = "#161b22"
    fg_primary:   str = "#e6edf3"
    fg_muted:     str = "#9ca3af"
    accent:       str = "#7c9cff"

    # фазовые цвета HUD'а
    phase_idle:        str = "#3a4250"
    phase_listening:   str = "#7c9cff"
    phase_thinking:    str = "#c4a3ff"
    phase_speaking:    str = "#7be3a4"
    phase_tool:        str = "#ffc46b"
    phase_muted:       str = "#5c6370"


PALETTE = Palette()


def color_for_phase(phase: Phase) -> str:
    return {
        Phase.IDLE:      PALETTE.phase_idle,
        Phase.LISTENING: PALETTE.phase_listening,
        Phase.THINKING:  PALETTE.phase_thinking,
        Phase.SPEAKING:  PALETTE.phase_speaking,
        Phase.TOOL:      PALETTE.phase_tool,
        Phase.MUTED:     PALETTE.phase_muted,
    }.get(phase, PALETTE.phase_idle)


def label_for_phase(phase: Phase) -> str:
    return {
        Phase.IDLE:      "OFFLINE",
        Phase.LISTENING: "LISTENING",
        Phase.THINKING:  "THINKING",
        Phase.SPEAKING:  "SPEAKING",
        Phase.TOOL:      "RUNNING TOOL",
        Phase.MUTED:     "MUTED",
    }[phase]


GLOBAL_QSS = f"""
QWidget {{
    background-color: {PALETTE.bg_window};
    color:            {PALETTE.fg_primary};
    font-family:      "Segoe UI", "Inter", sans-serif;
    font-size:        12pt;
}}
QFrame#sidebar, QFrame#card {{
    background-color: {PALETTE.bg_panel};
    border:           1px solid #1f2937;
    border-radius:    10px;
}}
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {PALETTE.bg_input};
    border:           1px solid #1f2937;
    border-radius:    8px;
    padding:          8px 10px;
    selection-background-color: {PALETTE.accent};
    color:            {PALETTE.fg_primary};
}}
QPushButton {{
    background-color: #1c2433;
    border:           1px solid #2a334a;
    border-radius:    8px;
    padding:          6px 14px;
    color:            {PALETTE.fg_primary};
}}
QPushButton:hover    {{ background-color: #232c40; }}
QPushButton:pressed  {{ background-color: #1a2030; }}
QPushButton:disabled {{ color: {PALETTE.fg_muted}; }}
QPushButton#primary  {{
    background-color: {PALETTE.accent};
    color:            #0a0d12;
    border:           none;
}}
QPushButton#stop     {{
    background-color: #5b1a1a;
    color:            #ffe7e7;
    border:           1px solid #7a2828;
}}
QPushButton#stop:disabled {{
    background-color: #2a1414;
    color:            #7a3a3a;
}}
QLabel#title       {{ font-size: 22pt; font-weight: 600; }}
QLabel#phase_label {{ font-size: 14pt; font-weight: 500; letter-spacing: 1px; }}
QLabel#hint        {{ color: {PALETTE.fg_muted}; }}
"""
