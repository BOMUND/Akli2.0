"""HUD-индикатор фазы.

Один кружок, цвет берётся из ``theme.color_for_phase``. Анимация пульса
для SPEAKING и THINKING — простая, через ``QTimer`` и фазовую переменную.
Никакого ``QGraphicsView`` и кастомных шейдеров.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPaintEvent, QPainter, QRadialGradient
from PyQt6.QtWidgets import QWidget

from akli.live.state import Phase
from akli.ui.theme import color_for_phase


class HudOrb(QWidget):
    """Виджет-кружок с текущей фазой."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._phase = Phase.IDLE
        self._t = 0.0
        self.setMinimumSize(220, 220)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_phase(self, phase: Phase) -> None:
        if phase is self._phase:
            return
        self._phase = phase
        self.update()

    def _tick(self) -> None:
        self._t += 0.04
        # Перерисовываем только когда есть смысл — для статичной фазы можно реже,
        # но проще всегда (40 мс ≈ 25 FPS).
        if self._phase in (Phase.SPEAKING, Phase.THINKING, Phase.TOOL):
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        cx, cy = rect.center().x(), rect.center().y()
        base = min(rect.width(), rect.height()) * 0.32

        # пульсация
        if self._phase in (Phase.SPEAKING, Phase.THINKING):
            base += math.sin(self._t * 4.0) * 6
        elif self._phase is Phase.TOOL:
            base += math.sin(self._t * 6.0) * 3

        color = QColor(color_for_phase(self._phase))

        # внешнее свечение
        grad = QRadialGradient(cx, cy, base * 2.2)
        glow = QColor(color)
        glow.setAlpha(40)
        grad.setColorAt(0.0, glow)
        glow_outer = QColor(color)
        glow_outer.setAlpha(0)
        grad.setColorAt(1.0, glow_outer)
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(cx - base * 2.2), int(cy - base * 2.2),
                      int(base * 4.4), int(base * 4.4))

        # ядро
        p.setBrush(color)
        p.drawEllipse(int(cx - base), int(cy - base), int(base * 2), int(base * 2))
