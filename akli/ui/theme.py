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
QFrame#header {{
    background-color: {PALETTE.bg_panel};
    border:           1px solid #1f2937;
    border-radius:    10px;
}}
QLabel#brand {{
    font-size:   16pt;
    font-weight: 700;
    color:       {PALETTE.accent};
    letter-spacing: 2px;
}}
QPushButton#stop_header {{
    background-color: #5b1a1a;
    color:            #ffe7e7;
    border:           1px solid #7a2828;
    font-weight:      600;
    padding:          8px 18px;
    border-radius:    8px;
}}
QPushButton#stop_header:hover    {{ background-color: #6e2222; }}
QPushButton#stop_header:disabled {{
    background-color: #2a1414;
    color:            #7a3a3a;
    border:           1px solid #3a1818;
}}
QPushButton#settings_btn {{
    background-color: #1c2433;
    border:           1px solid #2a334a;
    border-radius:    8px;
    padding:          6px;
    font-size:        16pt;
}}
QPushButton#settings_btn:hover {{ background-color: #232c40; }}
QTabWidget::pane {{
    border:           1px solid #1f2937;
    border-radius:    8px;
    background-color: {PALETTE.bg_panel};
}}
QTabBar::tab {{
    background-color: #161b22;
    color:            {PALETTE.fg_muted};
    padding:          8px 16px;
    border:           1px solid #1f2937;
    border-bottom:    none;
    border-top-left-radius:  6px;
    border-top-right-radius: 6px;
    margin-right:     2px;
}}
QTabBar::tab:selected {{
    background-color: {PALETTE.bg_panel};
    color:            {PALETTE.fg_primary};
}}
QTabBar::tab:hover {{ color: {PALETTE.fg_primary}; }}
QProgressBar {{
    background-color: #161b22;
    border:           1px solid #1f2937;
    border-radius:    6px;
    text-align:       center;
    color:            {PALETTE.fg_primary};
    height:           18px;
}}
QProgressBar::chunk {{
    background-color: {PALETTE.phase_listening};
    border-radius:    5px;
}}
QTableWidget, QListWidget {{
    background-color: #161b22;
    border:           1px solid #1f2937;
    border-radius:    8px;
    gridline-color:   #1f2937;
}}
QHeaderView::section {{
    background-color: #1c2433;
    color:            {PALETTE.fg_muted};
    padding:          4px 8px;
    border:           none;
    border-right:     1px solid #1f2937;
}}
QComboBox {{
    background-color: {PALETTE.bg_input};
    border:           1px solid #1f2937;
    border-radius:    6px;
    padding:          6px 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {PALETTE.bg_panel};
    border:           1px solid #1f2937;
    selection-background-color: {PALETTE.accent};
    selection-color:  #0a0d12;
}}
"""
