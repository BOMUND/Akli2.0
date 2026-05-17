"""Стартовый диалог конфигурации.

Поля:
* Gemini API key (**обязательно**)
* ОС (выпадающий список — мы хитро отдаём только Windows 10 в фокусе MVP)
* Чекбокс «Use OpenRouter as text LLM» — выключен по умолчанию
* Поле OpenRouter API key — становится активным только если чекбокс включён

Без этого диалога UI не запустится. Никаких «warning, set up later».
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from akli.core.config import AppConfig


class SetupDialog(QDialog):
    """Возвращает заполненный :class:`AppConfig` или None при отмене."""

    def __init__(self, current: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Akli 2.0 — initial setup")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Welcome to Akli 2.0")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "Provide your Gemini API key to begin. Everything else is optional."
        ))

        # Gemini key
        layout.addWidget(self._label("Gemini API key:"))
        self._gemini = QLineEdit(current.gemini_api_key)
        self._gemini.setEchoMode(QLineEdit.EchoMode.Password)
        self._gemini.setPlaceholderText("AIza…")
        layout.addWidget(self._gemini)

        # OS
        layout.addWidget(self._label("Operating system:"))
        self._os = QComboBox()
        # Порядок/имена совпадают с докой ``AppConfig.os_system`` ("windows" | "mac" | "linux").
        # Раньше было "macos" — миграция в ``config.load_config`` сохраняет "mac",
        # но новые установки из диалога раньше писали "macos" — любой будущий
        # ``if os == "mac":`` тихо отвалился бы для них.
        self._os.addItems(["windows", "mac", "linux"])
        if current.os_system:
            idx = self._os.findText(current.os_system)
            if idx >= 0:
                self._os.setCurrentIndex(idx)
        layout.addWidget(self._os)

        # OpenRouter
        self._use_or = QCheckBox("Use OpenRouter as optional text LLM")
        self._use_or.setChecked(current.use_openrouter)
        self._use_or.toggled.connect(self._toggle_or_inputs)
        layout.addWidget(self._use_or)

        self._or_key_label = self._label("OpenRouter API key:")
        layout.addWidget(self._or_key_label)
        self._or_key = QLineEdit(current.openrouter_api_key)
        self._or_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._or_key.setPlaceholderText("sk-or-…")
        layout.addWidget(self._or_key)

        self._or_model_label = self._label("OpenRouter model:")
        layout.addWidget(self._or_model_label)
        self._or_model = QLineEdit(current.openrouter_model)
        layout.addWidget(self._or_model)

        # buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        btns.addWidget(save)
        layout.addLayout(btns)

        self._toggle_or_inputs(self._use_or.isChecked())

    def _label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("hint")
        return lab

    def _toggle_or_inputs(self, on: bool) -> None:
        self._or_key.setEnabled(on)
        self._or_key_label.setEnabled(on)
        self._or_model.setEnabled(on)
        self._or_model_label.setEnabled(on)

    def _on_save(self) -> None:
        if not self._gemini.text().strip():
            self._gemini.setStyleSheet("border: 1px solid #ff5c5c;")
            return
        self.accept()

    def result_config(self, current: AppConfig) -> AppConfig:
        # ``replace`` сохраняет все остальные поля текущего конфига как есть
        # (``gemini_live_model``, ``gemini_voice_name``, ``mic_index``,
        # ``speaker_index``, и всё будущее что не редактируется в диалоге).
        # Без этого повторный setup (после сброса ``os_system``) тихо
        # возвращал модель/голос/устройства к дефолтам датакласса.
        return replace(
            current,
            gemini_api_key     = self._gemini.text().strip(),
            openrouter_api_key = self._or_key.text().strip() if self._use_or.isChecked() else "",
            use_openrouter     = bool(self._use_or.isChecked() and self._or_key.text().strip()),
            openrouter_model   = self._or_model.text().strip() or current.openrouter_model,
            os_system          = self._os.currentText(),
        )
