"""Окно настроек.

Открывается из шапки главного окна по нажатию на шестерёнку. Все правки
живут локально в копии :class:`AppConfig` до нажатия «Apply»; после Apply
конфиг пишется через :func:`akli.core.config.save` и UI эмитит сигналы,
которые ``AkliApp`` обрабатывает (например — reconnect при смене голоса).

Память (``core_memory.json``, ``state/dialog_summaries/*.md``) правится
прямо в момент клика на кнопку «удалить» — это «живой» раздел, не ждёт
Apply. Так пользователь видит результат сразу и не путается с «надо ли
ещё раз сохранять».

Размер окна / стилизация — под существующую тёмную палитру (theme.py).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from akli.core.config import AppConfig, DIALOG_SUMMARIES_DIR
from akli.memory.store import MemoryStore


# Список доступных голосов Gemini Live. Имена — официальные из docs.
# Если Google добавит — допишем; динамически дёргать API нет смысла, он
# не возвращает список голосов отдельным эндпоинтом.
GEMINI_VOICES = ("Puck", "Charon", "Fenrir", "Aoede", "Kore", "Leda")

# Дефолтный набор моделей OpenRouter. Free-уровень оставлен сверху —
# чтобы тот, кто без билинга, видел рабочие варианты первыми. Пользователь
# может вписать свою модель руками (поле редактируемое).
OPENROUTER_MODELS = (
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o-mini",
)


def _preferred_hostapis(sd) -> set[int] | None:
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        return None
    wasapi = {
        idx for idx, api in enumerate(hostapis)
        if "WASAPI" in str(api.get("name", "")).upper()
    }
    if wasapi:
        return wasapi
    try:
        default_input = int(sd.default.device[0])
        if default_input >= 0:
            return {int(sd.query_devices(default_input).get("hostapi", -1))}
    except Exception:
        pass
    return None


def _clean_device_name(name: str) -> str:
    return " ".join(name.replace("\r", " ").replace("\n", " ").split())


def _skip_device_name(name: str) -> bool:
    low = name.lower()
    return any(
        token in low
        for token in ("loopback", "what u hear", "sound mapper", "primary sound")
    )


def _list_input_devices() -> list[tuple[int, str]]:
    try:
        import sounddevice as sd  # noqa: PLC0415
        hostapis = _preferred_hostapis(sd)
        result: list[tuple[int, str]] = []
        seen: set[str] = set()
        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) <= 0:
                continue
            if (
                hostapis is not None
                and int(dev.get("hostapi", -1)) not in hostapis
            ):
                continue
            name = _clean_device_name(str(dev.get("name", f"dev #{idx}")))
            if not name or _skip_device_name(name):
                continue
            try:
                sd.check_input_settings(
                    device=idx,
                    channels=1,
                    dtype="int16",
                    samplerate=16000,
                )
            except Exception:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append((idx, name))
        return result
    except Exception:
        return []


def _list_output_devices() -> list[tuple[int, str]]:
    try:
        import sounddevice as sd  # noqa: PLC0415
        hostapis = _preferred_hostapis(sd)
        result: list[tuple[int, str]] = []
        seen: set[str] = set()
        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_output_channels", 0)) <= 0:
                continue
            if (
                hostapis is not None
                and int(dev.get("hostapi", -1)) not in hostapis
            ):
                continue
            name = _clean_device_name(str(dev.get("name", f"dev #{idx}")))
            if not name or _skip_device_name(name):
                continue
            try:
                sd.check_output_settings(
                    device=idx,
                    channels=1,
                    dtype="int16",
                    samplerate=24000,
                )
            except Exception:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append((idx, name))
        return result
    except Exception:
        return []


class SettingsWindow(QDialog):
    """Окно настроек. Эмитит сигналы при Apply."""

    # Сигнал «нужен реконнект Live-сессии» — например, поменяли голос
    # или микрофон. ``AkliApp`` слушает и форсирует reconnect.
    reconnect_required = pyqtSignal()
    # Сигнал «конфиг сохранён» — UI обновляет всё что от него зависит.
    config_changed = pyqtSignal(object)  # AppConfig

    def __init__(
        self,
        config:           AppConfig,
        memory:           MemoryStore,
        mic_level_getter: Callable[[], float],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Akli — settings")
        self.setModal(False)
        self.setMinimumSize(640, 540)

        self._config_initial = config
        self._config_working = replace(config)  # копия, правим её
        self._memory         = memory
        self._mic_level_getter = mic_level_getter

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_auth_tab(),   "Keys")
        self._tabs.addTab(self._build_audio_tab(),  "Audio")
        self._tabs.addTab(self._build_voice_tab(),  "Voice")
        self._tabs.addTab(self._build_memory_tab(), "Memory")
        root.addWidget(self._tabs, 1)

        # ─── footer
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("primary")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        btns.addWidget(apply_btn)
        root.addLayout(btns)

        # ─── mic level polling — таймер живёт пока открыто окно
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(80)  # ~12 FPS, хватит за глаза
        self._level_timer.timeout.connect(self._refresh_mic_level)
        self._level_timer.start()

    # ───────────────────────────── tabs ──

    def _build_auth_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(self._hint("Gemini API key (обязательный, для Live + memory):"))
        self._gemini = QLineEdit(self._config_working.gemini_api_key)
        self._gemini.setEchoMode(QLineEdit.EchoMode.Password)
        self._gemini.setPlaceholderText("AIza…")
        layout.addWidget(self._gemini)

        self._or_toggle = QCheckBox("Use OpenRouter as optional text LLM (для memory-pipeline)")
        self._or_toggle.setChecked(self._config_working.use_openrouter)
        self._or_toggle.toggled.connect(self._toggle_or_inputs)
        layout.addWidget(self._or_toggle)

        layout.addWidget(self._hint("OpenRouter API key:"))
        self._or_key = QLineEdit(self._config_working.openrouter_api_key)
        self._or_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._or_key.setPlaceholderText("sk-or-…")
        layout.addWidget(self._or_key)

        layout.addWidget(self._hint("OpenRouter модель (можно выбрать или вписать свою):"))
        self._or_model = QComboBox()
        self._or_model.setEditable(True)
        self._or_model.addItems(OPENROUTER_MODELS)
        cur = self._config_working.openrouter_model
        if cur and self._or_model.findText(cur) < 0:
            self._or_model.insertItem(0, cur)
        self._or_model.setCurrentText(cur or OPENROUTER_MODELS[0])
        layout.addWidget(self._or_model)

        layout.addStretch(1)
        self._toggle_or_inputs(self._or_toggle.isChecked())
        return w

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(self._hint("Микрофон (требует reconnect):"))
        self._mic_combo = QComboBox()
        self._mic_combo.addItem("Default (system)", -1)
        for idx, name in _list_input_devices():
            self._mic_combo.addItem(f"{idx}: {name}", idx)
        self._select_combo_by_data(self._mic_combo, self._config_working.mic_index)
        layout.addWidget(self._mic_combo)

        layout.addWidget(self._hint("Уровень микрофона (dBFS, текущая сессия):"))
        self._level_bar = QProgressBar()
        # Прогресс-бар не поддерживает отрицательные значения нативно,
        # поэтому маппим -60..0 dBFS → 0..60.
        self._level_bar.setRange(0, 60)
        self._level_bar.setValue(0)
        self._level_bar.setTextVisible(True)
        self._level_bar.setFormat("-60 dB")
        layout.addWidget(self._level_bar)

        layout.addWidget(self._hint("Колонки/наушники (требует reconnect):"))
        self._spk_combo = QComboBox()
        self._spk_combo.addItem("Default (system)", -1)
        for idx, name in _list_output_devices():
            self._spk_combo.addItem(f"{idx}: {name}", idx)
        self._select_combo_by_data(self._spk_combo, self._config_working.speaker_index)
        layout.addWidget(self._spk_combo)

        layout.addStretch(1)
        return w

    def _build_voice_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(self._hint("Голос Gemini Live (требует reconnect):"))
        self._voice_combo = QComboBox()
        for v in GEMINI_VOICES:
            self._voice_combo.addItem(v, v)
        cur = self._config_working.gemini_voice_name or GEMINI_VOICES[0]
        idx = self._voice_combo.findData(cur)
        self._voice_combo.setCurrentIndex(max(idx, 0))
        layout.addWidget(self._voice_combo)

        layout.addWidget(self._hint(
            "Puck — мужской по умолчанию. Charon/Fenrir — мужские варианты. "
            "Aoede/Kore/Leda — женские. Смена применится после reconnect."
        ))
        layout.addStretch(1)
        return w

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        outer.addWidget(self._hint("Core memory (факты, доступные модели в каждом запросе):"))
        self._facts_table = QTableWidget(0, 4)
        self._facts_table.setHorizontalHeaderLabels(["Category", "Key", "Value", ""])
        self._facts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._facts_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._facts_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._facts_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._facts_table.verticalHeader().setVisible(False)
        self._facts_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        outer.addWidget(self._facts_table, 1)

        facts_btns = QHBoxLayout()
        refresh = QPushButton("Reload")
        refresh.clicked.connect(self._reload_memory)
        facts_btns.addWidget(refresh)
        facts_btns.addStretch(1)
        clear_all = QPushButton("Clear all facts")
        clear_all.setObjectName("stop")
        clear_all.clicked.connect(self._on_clear_all_facts)
        facts_btns.addWidget(clear_all)
        outer.addLayout(facts_btns)

        # ─── разделитель
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1f2937; background-color: #1f2937;")
        outer.addWidget(sep)

        outer.addWidget(self._hint("Архив диалогов (state/dialog_summaries):"))
        self._summaries_list = QListWidget()
        outer.addWidget(self._summaries_list, 1)

        sum_btns = QHBoxLayout()
        ref2 = QPushButton("Reload")
        ref2.clicked.connect(self._reload_summaries)
        sum_btns.addWidget(ref2)
        sum_btns.addStretch(1)
        del_one = QPushButton("Delete selected")
        del_one.setObjectName("stop")
        del_one.clicked.connect(self._on_delete_selected_summary)
        sum_btns.addWidget(del_one)
        outer.addLayout(sum_btns)

        self._reload_memory()
        self._reload_summaries()
        return w

    # ───────────────────────────── helpers ──

    def _hint(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        return lab

    @staticmethod
    def _select_combo_by_data(combo: QComboBox, value) -> None:
        idx = combo.findData(value)
        combo.setCurrentIndex(max(idx, 0))

    def _toggle_or_inputs(self, on: bool) -> None:
        self._or_key.setEnabled(on)
        self._or_model.setEnabled(on)

    def _refresh_mic_level(self) -> None:
        try:
            db = float(self._mic_level_getter())
        except Exception:
            db = -60.0
        # -60..0 dBFS → 0..60
        bar_val = int(max(0.0, min(60.0, 60.0 + db)))
        self._level_bar.setValue(bar_val)
        self._level_bar.setFormat(f"{db:.0f} dB")

    # ───────────────────────────── memory tab ──

    def _reload_memory(self) -> None:
        self._memory.reload()
        data = self._memory.as_dict()
        rows: list[tuple[str, str, str]] = []
        for cat, items in data.items():
            if not isinstance(items, dict):
                continue
            for key, blob in items.items():
                if isinstance(blob, dict):
                    val = str(blob.get("value", ""))
                else:
                    val = str(blob)
                if not val:
                    continue
                rows.append((str(cat), str(key), val))

        self._facts_table.setRowCount(len(rows))
        for row_idx, (cat, key, val) in enumerate(rows):
            self._facts_table.setItem(row_idx, 0, QTableWidgetItem(cat))
            self._facts_table.setItem(row_idx, 1, QTableWidgetItem(key))
            self._facts_table.setItem(row_idx, 2, QTableWidgetItem(val))
            btn = QPushButton("Delete")
            btn.setObjectName("stop")
            # Замыкания в Python — захватываем по дефолтным аргументам,
            # иначе все кнопки будут ссылаться на последний (cat,key).
            btn.clicked.connect(lambda _checked=False, c=cat, k=key: self._on_delete_fact(c, k))
            self._facts_table.setCellWidget(row_idx, 3, btn)

    def _on_delete_fact(self, cat: str, key: str) -> None:
        # Sentinel None → MemoryStore._merge удалит ключ.
        self._memory.update({cat: {key: None}})
        self._reload_memory()

    def _on_clear_all_facts(self) -> None:
        ans = QMessageBox.question(
            self,
            "Clear all facts",
            "Удалить ВСЕ записи core memory? Это нельзя отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._memory.reset()
        self._reload_memory()

    def _reload_summaries(self) -> None:
        self._summaries_list.clear()
        if not DIALOG_SUMMARIES_DIR.exists():
            return
        # Сортировка по имени = по timestamp в начале (формат "<ts>_<title>.md").
        files = sorted(DIALOG_SUMMARIES_DIR.glob("*.md"))
        for path in files:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self._summaries_list.addItem(item)

    def _on_delete_selected_summary(self) -> None:
        item = self._summaries_list.currentItem()
        if item is None:
            return
        path_str = item.data(Qt.ItemDataRole.UserRole)
        if not path_str:
            return
        path = Path(path_str)
        ans = QMessageBox.question(
            self,
            "Delete summary",
            f"Удалить {path.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        self._reload_summaries()

    # ───────────────────────────── apply ──

    def _on_apply(self) -> None:
        # Собираем working-копию и считаем, требуется ли reconnect.
        # Reconnect требуется при смене голоса/модели/устройств — Live
        # session берёт эти значения при ``_build_config`` и при создании
        # streams. Ключи / OpenRouter — без реконнекта (читаются на
        # следующем memory-pipeline вызове).
        new_cfg = replace(
            self._config_working,
            gemini_api_key     = self._gemini.text().strip(),
            openrouter_api_key = self._or_key.text().strip() if self._or_toggle.isChecked() else "",
            use_openrouter     = bool(self._or_toggle.isChecked() and self._or_key.text().strip()),
            openrouter_model   = self._or_model.currentText().strip()
                                 or self._config_working.openrouter_model,
            gemini_voice_name  = self._voice_combo.currentData()
                                 or self._config_working.gemini_voice_name,
            mic_index          = int(self._mic_combo.currentData()),
            speaker_index      = int(self._spk_combo.currentData()),
        )

        needs_reconnect = (
            new_cfg.gemini_voice_name != self._config_initial.gemini_voice_name
            or new_cfg.mic_index      != self._config_initial.mic_index
            or new_cfg.speaker_index  != self._config_initial.speaker_index
            or new_cfg.gemini_live_model != self._config_initial.gemini_live_model
        )

        self._config_working = new_cfg
        self._config_initial = new_cfg
        self.config_changed.emit(new_cfg)
        if needs_reconnect:
            self.reconnect_required.emit()
