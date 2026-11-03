"""Custom widgets for data input."""

from PySide6.QtCore import QEvent, QObject, QPointF, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QWidget,
    QHBoxLayout,
    QLineEdit,
    QComboBox,
    QLabel,
    QPushButton,
    QSpinBox,
)

from ...i18n import tr
from ...models import Length, LengthIdentifier, Wire
from ...models.length import LengthError
from ...models.wire import VALID_STRANDS, CU_STRANDS_DEFAULTS, AL_STRANDS_DEFAULTS, AL_STRANDS_DEFAULT, REVERSE_WIRE_SIZE_MAP


class _ComboBoxScrollBlocker(QObject):
    """Keep wheel input from changing a combo while still scrolling its page."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            parent = obj.parent()
            while parent is not None and not isinstance(
                parent, QAbstractScrollArea
            ):
                parent = parent.parent()
            if isinstance(parent, QAbstractScrollArea) and isinstance(
                event, QWheelEvent
            ):
                viewport = parent.viewport()
                local_pos = viewport.mapFromGlobal(
                    event.globalPosition().toPoint()
                )
                forwarded = QWheelEvent(
                    QPointF(local_pos),
                    event.globalPosition(),
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                )
                QApplication.sendEvent(viewport, forwarded)
            event.accept()
            return True
        return super().eventFilter(obj, event)


def disable_combo_scroll(combo: QComboBox) -> QComboBox:
    """Prevent wheel edits on *combo* and forward them to its scroll area.

    调用方式：combo = disable_combo_scroll(QComboBox())
    """
    combo.installEventFilter(_ComboBoxScrollBlocker(combo))
    return combo


class LengthEdit(QWidget):
    """Widget for editing Length values."""

    value_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._identifier_options: list[tuple[str, str]] = []
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._line_edit = QLineEdit()
        self._line_edit.setPlaceholderText(tr("widgets.length_placeholder", "e.g., 1000mm, 1m, A+B, 1S2R3.3inch"))
        self._line_edit.editingFinished.connect(self._on_edit_finished)
        
        self._label = QLabel(tr("widgets.length_unit_default", "mm"))
        self._label.setStyleSheet("color: gray;")
        
        layout.addWidget(self._line_edit)
        layout.addWidget(self._label)
    
    def set_identifier_options(self, identifier_options: list[tuple[str, str]]) -> None:
        """Set identifier options for inline validation."""
        self._identifier_options = identifier_options

    def get_text(self) -> str:
        """Get current text value."""
        return self._line_edit.text().strip()

    def set_text(self, text: str) -> None:
        """Set text value."""
        self._text = text.strip()
        if self._text:
            self._line_edit.setText(self._text)
            try:
                length = self._build_length(self._text)
                mm = length.to_mm()
                ft = length.to_ft()
                self._label.setText(f"= {ft:.2f} ft ({mm:.1f} mm)")
            except ValueError:
                self._label.setText(tr("widgets.length_needs_identifier", "(needs identifier)"))
        else:
            self._line_edit.clear()
            self._label.setText(tr("widgets.length_unit_default", "mm"))
    
    def _on_edit_finished(self) -> None:
        text = self._line_edit.text().strip()
        # 文本未变化时跳过，避免因焦点切换触发无意义的 value_changed
        if text == self._text:
            return
        self._text = text
        if not text:
            self._label.setText(tr("widgets.length_unit_default", "mm"))
        else:
            try:
                length = self._build_length(text)
                mm = length.to_mm()
                ft = length.to_ft()
                self._label.setText(f"= {ft:.2f} ft ({mm:.1f} mm)")
            except LengthError as e:
                self._label.setText(tr("widgets.length_error", "(error: {error})").format(
                    error=tr(f"length.error.{e.code}", str(e)).format(**e.params)))
                return
        self.value_changed.emit()

    def _build_length(self, text: str) -> Length:
        identifiers = [
            LengthIdentifier(name=name, length=Length(length_str))
            for name, length_str in self._identifier_options
        ]
        for identifier in identifiers:
            identifier.length.set_identifiers(identifiers)
        return Length(text, identifiers=identifiers)


class WireEdit(QWidget):
    """Widget for editing Wire values - shows wire size and strands.

    Material is set per-field by the caller (defaults to "Cu").
    When the wire size changes, the strands combo automatically updates
    to the inferred default for that material+size combination.
    """

    value_changed = Signal()

    WIRE_SIZES = [
        "none",
        "14AWG", "12AWG", "10AWG", "8AWG", "6AWG",
        "4AWG", "2AWG", "1/0AWG", "2/0AWG", "3/0AWG",
        "4/0AWG", "300MCM", "350MCM", "400MCM", "500MCM",
        "600MCM", "750MCM", "900MCM", "1000MCM",
    ]

    STRANDS_VALUES = sorted(VALID_STRANDS, key=lambda s: (s != "none", s))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._wire: Wire | None = None
        self._material: str = "Cu"
        self._strands_readonly: bool = False
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._size_combo = disable_combo_scroll(QComboBox())
        self._size_combo.addItems(self.WIRE_SIZES)
        self._size_combo.setCurrentIndex(0)
        self._size_combo.currentTextChanged.connect(self._on_size_changed)
        
        self._strands_combo = disable_combo_scroll(QComboBox())
        self._strands_combo.addItems(self.STRANDS_VALUES)
        self._strands_combo.setCurrentIndex(0)
        self._strands_combo.currentTextChanged.connect(self._on_strands_changed)
        
        self._strands_label = QLabel()
        self._strands_label.setStyleSheet("color: gray; padding-left: 4px;")
        self._strands_label.hide()
        
        layout.addWidget(QLabel(tr("widgets.wire_size_label", "Size:")))
        layout.addWidget(self._size_combo)
        layout.addWidget(QLabel(tr("widgets.wire_strands_label", "Strands:")))
        layout.addWidget(self._strands_combo)
        layout.addWidget(self._strands_label)
        layout.addStretch()

    @property
    def size_combo(self) -> QComboBox:
        return self._size_combo

    @property
    def strands_combo(self) -> QComboBox:
        return self._strands_combo

    @property
    def material(self) -> str:
        return self._material

    @material.setter
    def material(self, value: str) -> None:
        if value not in ("Cu", "Al"):
            raise ValueError(f"Unsupported material: {value}")
        self._material = value

    @property
    def strands_readonly(self) -> bool:
        return self._strands_readonly

    @strands_readonly.setter
    def strands_readonly(self, value: bool) -> None:
        if self._strands_readonly == value:
            return
        self._strands_readonly = value
        if value:
            self._strands_label.setText(self._strands_combo.currentText())
            self._strands_combo.hide()
            self._strands_label.show()
        else:
            self._strands_combo.show()
            self._strands_label.hide()

    @staticmethod
    def _infer_strands(size: str, material: str) -> str:
        if size == "none":
            return "none"
        if material == "Al":
            if size in AL_STRANDS_DEFAULTS:
                return AL_STRANDS_DEFAULTS[size]
            awg = REVERSE_WIRE_SIZE_MAP.get(size, size)
            return AL_STRANDS_DEFAULTS.get(awg, AL_STRANDS_DEFAULT)
        if material == "Cu":
            if size in CU_STRANDS_DEFAULTS:
                return CU_STRANDS_DEFAULTS[size]
            awg = REVERSE_WIRE_SIZE_MAP.get(size, size)
            return CU_STRANDS_DEFAULTS.get(awg, "none")
        return "none"
    
    def set_value(self, size: str, strands: str = "none") -> None:
        if size:
            try:
                self._wire = Wire(wiresize=size, material=self._material, strands=strands)
            except ValueError:
                self._wire = None
            idx = self._size_combo.findText(size)
            if idx >= 0:
                self._size_combo.setCurrentIndex(idx)
            else:
                self._size_combo.setCurrentIndex(0)
            idx = self._strands_combo.findText(strands)
            if idx >= 0:
                self._strands_combo.setCurrentIndex(idx)
            else:
                self._strands_combo.setCurrentIndex(0)
        else:
            self._wire = None
            self._size_combo.setCurrentIndex(0)
            self._strands_combo.setCurrentIndex(0)

    def _on_size_changed(self, size: str) -> None:
        default_strands = self._infer_strands(size, self._material)
        idx = self._strands_combo.findText(default_strands)
        if idx >= 0:
            self._strands_combo.setCurrentIndex(idx)
        else:
            self._strands_combo.setCurrentIndex(0)
        if self._strands_readonly:
            self._strands_label.setText(default_strands)
        self._rebuild_wire(size, default_strands)
        self.value_changed.emit()

    def _on_strands_changed(self, strands: str) -> None:
        size = self._size_combo.currentText()
        self._rebuild_wire(size, strands)
        self.value_changed.emit()

    def _rebuild_wire(self, size: str, strands: str) -> None:
        if size == "none":
            self._wire = None
        else:
            try:
                self._wire = Wire(wiresize=size, material=self._material, strands=strands)
            except ValueError:
                self._wire = None


class RackingsEdit(QWidget):
    """Widget for selecting or creating Rackings."""

    value_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rackings_names: list[str] = []
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._combo = disable_combo_scroll(QComboBox())
        self._combo.setEditable(True)
        self._combo.activated.connect(lambda: self._on_changed())
        self._combo.lineEdit().editingFinished.connect(self._on_changed)
        layout.addWidget(self._combo)
    
    def set_rackings_names(self, rackings_names: list[str]) -> None:
        """Set available rackings names."""
        self._rackings_names = rackings_names
        current = self._combo.currentText()
        self._combo.clear()
        self._combo.addItem(tr("widgets.rackings_none", "-- None --"))
        for name in rackings_names:
            self._combo.addItem(name)
        self._combo.setCurrentText(current)
    
    def get_value(self) -> str:
        """Get current rackings name."""
        text = self._combo.currentText()
        none_label = tr("widgets.rackings_none", "-- None --")
        if text == none_label or text == "-- None --":
            return ""
        return text
    
    def set_value(self, name: str) -> None:
        """Set rackings name."""
        idx = self._combo.findText(name)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        else:
            self._combo.setCurrentText(name)
    
    def _on_changed(self) -> None:
        self.value_changed.emit()


class LynxPresetEdit(QWidget):
    """Widget for selecting or creating LYNX presets."""

    value_changed = Signal(str)
    create_new_pressed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preset_names: list[str] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._combo = disable_combo_scroll(QComboBox())
        self._combo.setEditable(False)
        self._combo.currentTextChanged.connect(self._on_changed)
        layout.addWidget(self._combo, 1)
        self._create_btn = QPushButton("+")
        self._create_btn.setToolTip(tr("widgets.lynx_preset_create_tooltip", "Create New LYNX Preset"))
        self._create_btn.setFixedWidth(30)
        self._create_btn.clicked.connect(self._on_create_clicked)
        layout.addWidget(self._create_btn)

    def set_options(self, preset_names: list[str]) -> None:
        """Set available LYNX preset names."""
        self._preset_names = preset_names
        current = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem(tr("widgets.lynx_preset_none", "-- None --"))
        for name in preset_names:
            self._combo.addItem(name)
        self._combo.blockSignals(False)
        # Restore current value if it still exists
        idx = self._combo.findText(current)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

    def get_value(self) -> str:
        """Get current LYNX preset name."""
        text = self._combo.currentText()
        none_label = tr("widgets.lynx_preset_none", "-- None --")
        if text == none_label or text == "-- None --":
            return ""
        return text

    def set_value(self, name: str) -> None:
        """Set LYNX preset name."""
        if not name:
            self._combo.setCurrentIndex(0)
            return
        idx = self._combo.findText(name)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

    def _on_changed(self, text: str) -> None:
        none_label = tr("widgets.lynx_preset_none", "-- None --")
        if text == none_label or text == "-- None --":
            self.value_changed.emit("")
        else:
            self.value_changed.emit(text)

    def _on_create_clicked(self) -> None:
        self.create_new_pressed.emit()


class ProjectTypeEdit(QComboBox):
    """ComboBox for project type selection."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.addItems(["IBEX", "LYNX"])
        # The owning model is the source of truth.  Do not silently display the
        # first option before set_value() receives the stored value.
        self.setCurrentIndex(-1)
    
    def get_value(self) -> str:
        return self.currentText()
    
    def set_value(self, value: str) -> None:
        idx = self.findText(value)
        self.setCurrentIndex(idx)


class ProductPresetsEdit(QWidget):
    """Widget for editing product presets string."""

    value_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._line_edit = QLineEdit()
        self._line_edit.setPlaceholderText(tr("widgets.product_presets_placeholder", "e.g., ABCD (uppercase letters)"))
        self._line_edit.textChanged.connect(self._on_changed)
        
        layout.addWidget(self._line_edit)
    
    def get_value(self) -> str:
        return self._line_edit.text().upper()
    
    def set_value(self, value: str) -> None:
        self._line_edit.setText(value)
    
    def _on_changed(self) -> None:
        text = self._line_edit.text()
        upper_text = "".join(c for c in text if c.isalpha()).upper()
        if text != upper_text:
            self._line_edit.setText(upper_text)
        self.value_changed.emit()
