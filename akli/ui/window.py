"""Главное окно.

Шапка — бренд + «⚙ settings».
Левая колонка — HUD-кружок с фазой + сетка runtime-кнопок.
Правая — активити-лог и текстовый ввод.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from akli.live.state import Phase, SpeakingState
from akli.ui.hud import HudOrb
from akli.ui.theme import label_for_phase


class MainWindow(QMainWindow):
    """Сигналы наружу — текст, mute и runtime-control кнопки."""

    text_submitted      = pyqtSignal(str)
    mute_toggled        = pyqtSignal()
    stop_tool_clicked   = pyqtSignal()
    interrupt_clicked   = pyqtSignal()
    reconnect_clicked   = pyqtSignal()
    settings_clicked    = pyqtSignal()      # ⚙ в шапке

    def __init__(self, state: SpeakingState) -> None:
        super().__init__()
        self._state = state
        self.setWindowTitle("Akli 2.0")
        self.resize(980, 700)
        self.setMinimumSize(820, 580)

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_phase)
        self._timer.start(100)

    # ───────────────────────────── layout ──

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_left(), 1)
        body.addWidget(self._build_right(), 2)
        root.addLayout(body, 1)

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("header")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(10)

        brand = QLabel("Akli")
        brand.setObjectName("brand")
        h.addWidget(brand)
        h.addStretch(1)

        self._header_settings_btn = QPushButton("⚙")
        self._header_settings_btn.setObjectName("settings_btn")
        self._header_settings_btn.setToolTip("Settings")
        self._header_settings_btn.setFixedWidth(40)
        self._header_settings_btn.clicked.connect(self.settings_clicked.emit)
        h.addWidget(self._header_settings_btn)
        return bar

    def _build_left(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidebar")
        v = QVBoxLayout(panel)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(12)

        v.addWidget(QLabel("Akli 2.0", objectName="title"), 0, Qt.AlignmentFlag.AlignHCenter)

        self._orb = HudOrb()
        v.addWidget(self._orb, 1)

        self._phase_label = QLabel(label_for_phase(Phase.IDLE), objectName="phase_label")
        self._phase_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self._phase_label)

        v.addStretch(1)

        controls = QGridLayout()
        controls.setHorizontalSpacing(8)
        controls.setVerticalSpacing(8)

        self._mute_btn = QPushButton("Mute")
        self._mute_btn.clicked.connect(self.mute_toggled.emit)
        controls.addWidget(self._mute_btn, 0, 0)

        self._stop_tool_btn = QPushButton("Stop tool")
        self._stop_tool_btn.setObjectName("stop")
        self._stop_tool_btn.setEnabled(False)
        self._stop_tool_btn.clicked.connect(self.stop_tool_clicked.emit)
        controls.addWidget(self._stop_tool_btn, 0, 1)

        self._interrupt_btn = QPushButton("Interrupt")
        self._interrupt_btn.setObjectName("stop")
        self._interrupt_btn.setEnabled(False)
        self._interrupt_btn.clicked.connect(self.interrupt_clicked.emit)
        controls.addWidget(self._interrupt_btn, 1, 0)

        self._reconnect_btn = QPushButton("Reconnect")
        self._reconnect_btn.setEnabled(False)
        self._reconnect_btn.clicked.connect(self.reconnect_clicked.emit)
        controls.addWidget(self._reconnect_btn, 1, 1)

        v.addLayout(controls)
        return panel

    def _build_right(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("card")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        title = QLabel("Activity")
        title.setObjectName("phase_label")
        v.addWidget(title)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        v.addWidget(self._log, 1)

        self._input = _SubmitLineEdit()
        self._input.setPlaceholderText("Type a message and press Enter…")
        self._input.submitted.connect(self._on_submit)
        v.addWidget(self._input)

        return panel

    # ───────────────────────────── public API ──

    def append_log(self, line: str) -> None:
        self._log.appendPlainText(line)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_mute_text(self, muted: bool) -> None:
        self._mute_btn.setText("Unmute" if muted else "Mute")

    # ───────────────────────────── tick ──

    def _refresh_phase(self) -> None:
        ph = self._state.phase
        self._orb.set_phase(ph)
        self._phase_label.setText(label_for_phase(ph))
        self._stop_tool_btn.setEnabled(ph is Phase.TOOL)
        self._interrupt_btn.setEnabled(self._state.is_model_active())
        self._reconnect_btn.setEnabled(ph is not Phase.IDLE)
        self.set_mute_text(ph is Phase.MUTED)

    # ───────────────────────────── helpers ──

    def _on_submit(self, text: str) -> None:
        text = text.strip()
        if text:
            self.text_submitted.emit(text)


class _SubmitLineEdit(QLineEdit):
    """``QLineEdit`` с сигналом ``submitted`` и автоочисткой на Enter."""

    submitted = pyqtSignal(str)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit(self.text())
            self.clear()
            return
        super().keyPressEvent(event)
