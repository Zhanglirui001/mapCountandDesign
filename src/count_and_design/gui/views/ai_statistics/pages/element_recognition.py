"""Agent3 detection review and editable project-tree view."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QBrush,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyleOptionGraphicsItem,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .....application.ordering import (
    move_selected_items,
    natural_text_key,
    sort_selected_slots,
)
from .....application.ai_statistics import (
    ElementRecognitionRequest,
    ElementRecognitionResult,
    InvDraft,
    NodeStringTableRow,
    NodeDraft,
    PanelOrientation,
    RackingEntryDraft,
    RecognitionIssue,
    ReviewedTree,
    RowDraft,
    SourceReference,
    TrackerDefinition,
    build_agent3_debug_payload,
    recalculate_node,
    replace_table_rows_and_recalculate,
    resolve_table_string_count,
    summarize_node_rows,
)
from .....i18n import tr
from .....application.ai_statistics.element_recognition.issues import format_issue_message
from .....models import Node as ProjectNode
from .....models import Row as ProjectRow
from ...editable_table import ClickToEditTableWidget
from ...ordered_selection import OrderedTreeWidget
from ..widgets import WorkflowStagePage
from .orientation_options import (
    PANEL_ORIENTATION_OPTIONS,
    panel_orientation_text,
)


_COLORS = {
    "node": QColor("#2563eb"),
    "tracker": QColor("#059669"),
    "box": QColor("#d97706"),
    "table": QColor("#a855f7"),
    "issue": QColor("#dc2626"),
    "highlight": QColor("#00e5ff"),
}
_ORIENTATIONS = (
    PanelOrientation.UNKNOWN,
    *(
        PanelOrientation.from_value(code)
        for code, _english, _chinese in PANEL_ORIENTATION_OPTIONS
    ),
)
_PATH_ROLE = Qt.ItemDataRole.UserRole
_ENTITY_ROLE = Qt.ItemDataRole.UserRole + 1
_KIND_ROLE = Qt.ItemDataRole.UserRole + 2
_ISSUE_ENTITY_ROLE = Qt.ItemDataRole.UserRole
_ISSUE_PAGE_ROLE = Qt.ItemDataRole.UserRole + 1
_ISSUE_SEVERITY_ROLE = Qt.ItemDataRole.UserRole + 2
_ISSUE_KEY_ROLE = Qt.ItemDataRole.UserRole + 3
_ISSUE_MUTED_ROLE = Qt.ItemDataRole.UserRole + 4

_ZERO_OFFSET_RE = re.compile(
    r"^[+-]?(?:0+(?:\.0*)?|\.0+)(?:[a-zA-Z_]+)?$"
)

_UNSET_PAGE_SIGNATURE = object()


def _orientation_from_value(value: str) -> PanelOrientation:
    try:
        return PanelOrientation.from_value(value)
    except ValueError:
        return PanelOrientation.UNKNOWN


def _exec_context_menu(menu: QMenu, global_position) -> QAction | None:
    """Show a context menu and return the action chosen by the user.

    The blocking call is isolated here so tests can drive a menu without
    nesting an event loop.
    """

    return menu.exec(global_position)


class NoWheelComboBox(QComboBox):
    """Combo box whose value cannot be changed by the mouse wheel."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    """Integer editor whose value cannot be changed by the mouse wheel."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Decimal editor whose value cannot be changed by the mouse wheel."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelScrollBar(QScrollBar):
    """Navigation handle that only responds to direct pointer interaction."""

    def wheelEvent(self, event) -> None:
        event.accept()


class DetectionRectItem(QGraphicsRectItem):
    """Detection rectangle with a bold solid selection treatment."""

    def __init__(self, rect: QRectF) -> None:
        super().__init__(rect)
        self._highlight_pen = QPen(_COLORS["highlight"], 6)
        self._highlight_pen.setStyle(Qt.PenStyle.SolidLine)
        self._highlight_pen.setCosmetic(True)
        color = _COLORS["highlight"]
        self._highlight_brush = QBrush(
            QColor(color.red(), color.green(), color.blue(), 55)
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        base_option = QStyleOptionGraphicsItem(option)
        base_option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, base_option, widget)
        if not self.isSelected():
            return
        painter.save()
        painter.setPen(self._highlight_pen)
        painter.setBrush(self._highlight_brush)
        painter.drawRect(self.rect())
        painter.restore()


class DetectionCanvas(QGraphicsView):
    """Read-only zoomable drawing canvas with selectable detections."""

    entity_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setHorizontalScrollBar(
            NoWheelScrollBar(Qt.Orientation.Horizontal, self)
        )
        self.setVerticalScrollBar(
            NoWheelScrollBar(Qt.Orientation.Vertical, self)
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setRenderHints(self.renderHints())
        self.setBackgroundBrush(QBrush(QColor("#20242a")))
        self._rects: dict[str, QGraphicsRectItem] = {}
        self._last_centered_entity_id = ""
        self._rendered_image: bytes | None = None
        self._rendered_signature: object = _UNSET_PAGE_SIGNATURE

    def show_page(self, png_bytes: bytes, page=None) -> None:
        """Draw a page, reusing the current scene when nothing changed.

        Editing the structure tree re-publishes the same page and image.
        Rebuilding the scene would re-decode a multi-megapixel PNG, recreate
        every detection item, and reset the user's zoom, pan, and selection,
        so an identical payload keeps the current preview untouched.
        """

        signature = self._page_signature(page)
        if (
            png_bytes is self._rendered_image
            and signature == self._rendered_signature
        ):
            return
        self._scene.clear()
        self._rects.clear()
        self._last_centered_entity_id = ""
        pixmap = QPixmap()
        pixmap.loadFromData(png_bytes, "PNG")
        if not pixmap.isNull():
            self._scene.addItem(QGraphicsPixmapItem(pixmap))
        for detection in getattr(page, "detections", ()):
            box = detection.bbox
            item = DetectionRectItem(
                QRectF(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1)
            )
            color = (
                _COLORS["issue"]
                if detection.issues
                else _COLORS.get(detection.kind.lower(), QColor("#7c3aed"))
            )
            item.setPen(QPen(color, 3))
            item.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 18)))
            item.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, True)
            item.setData(0, detection.entity_id)
            item.setToolTip(
                f"{detection.kind}: {detection.label or detection.entity_id} "
                f"({detection.confidence:.0%})"
            )
            self._scene.addItem(item)
            self._rects[detection.entity_id] = item
        table_boxes = tuple(getattr(page, "table_bboxes", ()) or ())
        if not table_boxes:
            single_box = getattr(page, "table_bbox", None)
            table_boxes = (single_box,) if single_box is not None else ()
        source = str(getattr(page, "table_bbox_source", ""))
        image_width = int(getattr(page, "image_width", 0))
        image_height = int(getattr(page, "image_height", 0))
        # Every crop is read by the model, so each one is labelled with its
        # position in the batch instead of repeating "MinerU".
        for index, table_box in enumerate(table_boxes):
            self._add_table_detection(
                table_box,
                source,
                image_width,
                image_height,
                label_override=self._table_detection_label(
                    source, index, len(table_boxes)
                ),
            )
        self._scene.setSceneRect(self._scene.itemsBoundingRect())
        self.fit_page()
        self._rendered_image = png_bytes
        self._rendered_signature = signature

    @staticmethod
    def _table_detection_label(source: str, index: int, total: int) -> str:
        """Return the table-box label shown for one crop."""

        if total > 1:
            return tr(
                "ai_statistics.element_recognition.table_bbox_region",
                "Table region {index}/{total}",
            ).format(index=index + 1, total=total)
        return (
            tr(
                "ai_statistics.element_recognition.table_bbox_yolo",
                "YOLO detection",
            )
            if source.casefold() == "yolo"
            else tr(
                "ai_statistics.element_recognition.table_bbox_mineru",
                "MinerU detection",
            )
        )

    @staticmethod
    def _page_signature(page) -> object:
        """Return only the canvas-relevant content of one page result."""

        if page is None:
            return None
        table_boxes = tuple(getattr(page, "table_bboxes", ()) or ())
        if not table_boxes:
            single_box = getattr(page, "table_bbox", None)
            table_boxes = (single_box,) if single_box is not None else ()
        source = str(getattr(page, "table_bbox_source", ""))
        return (
            int(getattr(page, "image_width", 0)),
            int(getattr(page, "image_height", 0)),
            table_boxes,
            tuple(
                # Resolved labels keep a language switch from leaving stale
                # text on an otherwise unchanged page.
                DetectionCanvas._table_detection_label(
                    source, index, len(table_boxes)
                )
                for index in range(len(table_boxes))
            ),
            tuple(
                (
                    detection.entity_id,
                    detection.kind,
                    detection.confidence,
                    detection.bbox,
                    detection.label,
                    tuple(detection.issues),
                )
                for detection in getattr(page, "detections", ())
            ),
        )

    def _add_table_detection(
        self,
        box,
        source: str,
        image_width: int,
        image_height: int,
        *,
        label_override: str = "",
    ) -> None:
        rect = QRectF(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1)
        item = QGraphicsRectItem(rect)
        color = _COLORS["table"]
        pen = QPen(color, 3)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        item.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 12)))
        item.setZValue(0.5)
        item.setData(1, "table_detection")
        normalized_source = source.casefold()
        label_text = label_override or self._table_detection_label(
            source, 0, 1
        )
        label = QGraphicsSimpleTextItem(label_text, item)
        font = label.font()
        positive_dimensions = tuple(
            value for value in (image_width, image_height) if value > 0
        )
        shortest_side = min(positive_dimensions) if positive_dimensions else 1000
        font.setPixelSize(max(10, round(shortest_side * 0.012)))
        font.setBold(False)
        label.setFont(font)
        label.setBrush(QBrush(color))
        label.setPen(QPen(Qt.PenStyle.NoPen))
        label_rect = label.boundingRect()
        label.setPos(
            rect.right() - label_rect.width() - 2.0,
            rect.top() + 2.0,
        )
        label.setData(1, "table_detection_label")
        label.setData(2, normalized_source or "mineru")
        item.setToolTip(label_text)
        self._scene.addItem(item)

    def fit_page(self) -> None:
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(
                self._scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

    def clear_page(self) -> None:
        self._scene.clear()
        self._rects.clear()
        self._last_centered_entity_id = ""
        self._rendered_image = None
        self._rendered_signature = _UNSET_PAGE_SIGNATURE

    def clear_selection(self) -> None:
        """Drop the detection selection without moving zoom or scrollbars."""

        self._scene.clearSelection()
        for rect in self._rects.values():
            rect.setZValue(0)

    def select_entity(self, entity_id: str) -> None:
        self._scene.clearSelection()
        for rect in self._rects.values():
            rect.setZValue(0)
        item = self._rects.get(entity_id)
        if item is not None:
            item.setSelected(True)
            item.setZValue(1)
            if entity_id != self._last_centered_entity_id:
                self.centerOn(item)
                self._last_centered_entity_id = entity_id

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        for item in self._scene.selectedItems():
            entity_id = item.data(0)
            if entity_id:
                self.select_entity(str(entity_id))
                self.entity_selected.emit(str(entity_id))
                break

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            factor = 1.2 if delta > 0 else 1 / 1.2
            self.scale(factor, factor)
        event.accept()


class StructureTreeWidget(OrderedTreeWidget):
    """Count's ordered tree adapted to this page's item-data roles."""

    @staticmethod
    def _item_scope(item: QTreeWidgetItem) -> tuple[object, object]:
        parent = item.parent()
        return item.data(0, _KIND_ROLE), id(parent) if parent is not None else "root"


class IssueListWidget(QListWidget):
    """Issue list that can divide selected-page results from all others."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._divider_before_row: int | None = None

    def set_divider_before_row(self, row: int | None) -> None:
        self._divider_before_row = row
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        row = self._divider_before_row
        if row is None or row <= 0 or row >= self.count():
            return
        rect = self.visualItemRect(self.item(row))
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawLine(rect.left() + 8, rect.top(), rect.right() - 8, rect.top())


class IssueItemDelegate(QStyledItemDelegate):
    """Render only the issue-level prefix in its severity color."""

    _PREFIX_COLORS = {
        "error": QColor("#b91c1c"),
        "warning": QColor("#b45309"),
    }
    _MESSAGE_COLOR = QColor("#0f172a")
    _MUTED_COLOR = QColor("#94a3b8")

    def paint(self, painter: QPainter, option, index) -> None:
        styled_option = QStyleOptionViewItem(option)
        self.initStyleOption(styled_option, index)
        muted = bool(index.data(_ISSUE_MUTED_ROLE))
        text = styled_option.text
        prefix_end = text.find("]") + 1
        if prefix_end <= 0:
            super().paint(painter, option, index)
            return

        prefix = text[:prefix_end]
        message = text[prefix_end:].lstrip()
        styled_option.text = ""
        widget = styled_option.widget
        style = widget.style() if widget is not None else None
        if style is None:
            super().paint(painter, option, index)
            return
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            styled_option,
            painter,
            widget,
        )
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText,
            styled_option,
            widget,
        )
        metrics = QFontMetrics(styled_option.font)
        prefix_width = metrics.horizontalAdvance(prefix)
        gap_width = metrics.horizontalAdvance(" ")
        alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        painter.save()
        painter.setFont(styled_option.font)
        painter.setPen(
            self._MUTED_COLOR
            if muted
            else self._PREFIX_COLORS.get(
                str(index.data(_ISSUE_SEVERITY_ROLE)),
                styled_option.palette.color(QPalette.ColorRole.Text),
            )
        )
        painter.drawText(text_rect, alignment, prefix)

        message_rect = text_rect.adjusted(prefix_width + gap_width, 0, 0, 0)
        # Keep selected issue text dark on the deliberately light selection
        # background.  Some platform palettes otherwise supply white
        # HighlightedText, making the message disappear.
        painter.setPen(self._MUTED_COLOR if muted else self._MESSAGE_COLOR)
        painter.drawText(
            message_rect,
            alignment,
            metrics.elidedText(
                message,
                styled_option.textElideMode,
                max(0, message_rect.width()),
            ),
        )
        painter.restore()


class ElementRecognitionPage(WorkflowStagePage):
    """Review Agent3 boxes/issues and edit its independent structure draft."""

    review_changed = Signal(object)
    inv_settings_changed = Signal(str, object, float)
    project_settings_changed = Signal(object, float)
    table_review_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "ElementRecognitionPage",
            "ai_statistics.element_recognition.confirm",
            "Confirm Elements",
            parent,
        )
        self._request: ElementRecognitionRequest | None = None
        self._result: ElementRecognitionResult | None = None
        self._tree = ReviewedTree()
        self._page_images: dict[int, bytes] = {}
        self._page_entries: tuple[tuple[str, int], ...] = ()
        self._table_rows_snapshot: tuple[NodeStringTableRow, ...] = ()
        self._tree_updating = False
        self._property_updating = False
        self._issue_count = 0
        self._issue_total = 0
        self._issue_query = ""
        # Muted issues are a review convenience, not part of the recognized
        # result: they stay in this page and are dropped with the result.
        self._muted_issue_keys: set[tuple[object, ...]] = set()
        self._clipboard: InvDraft | NodeDraft | RowDraft | None = None
        self._shortcuts: list[QShortcut] = []
        self._setup_ui()
        self._setup_shortcuts()
        self.retranslate()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("agent3MainSplitter")
        # Both panes may be dragged to any width, including fully collapsed.
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.addWidget(self._build_canvas_panel())
        self.main_splitter.addWidget(self._build_review_panel())
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([800, 400])
        root.addWidget(self.main_splitter, 1)
        self.append_confirm_footer(root)

    def _build_canvas_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        # Ignore the toolbar's size hints so the handle can put the canvas at
        # any width, down to a fully collapsed pane.
        panel.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("agent3PreviewToolbar")
        controls = QHBoxLayout(toolbar)
        controls.setContentsMargins(8, 6, 8, 6)
        controls.setSpacing(6)
        self.page_combo = NoWheelComboBox()
        self.page_combo.currentIndexChanged.connect(self._show_selected_page)
        controls.addWidget(self.page_combo, 1)
        self.fit_button = QPushButton()
        self.fit_button.setObjectName("agent3CompactButton")
        self.fit_button.clicked.connect(self._fit_canvas)
        controls.addWidget(self.fit_button)
        self.issues_button = QPushButton()
        self.issues_button.setObjectName("agent3IssueButton")
        self.issues_button.clicked.connect(self._show_issues_dialog)
        self.issues_button.hide()
        controls.addWidget(self.issues_button)
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("agent3CompactButton")
        self.settings_button.clicked.connect(self._show_settings_dialog)
        controls.addWidget(self.settings_button)
        self.table_button = QPushButton()
        self.table_button.setObjectName("agent3CompactButton")
        self.table_button.setCheckable(True)
        self.table_button.toggled.connect(self._toggle_table_review)
        controls.addWidget(self.table_button)
        self.debug_export_button = QPushButton()
        self.debug_export_button.setObjectName("agent3CompactButton")
        self.debug_export_button.clicked.connect(self._export_debug_json)
        self.debug_export_button.setEnabled(False)
        controls.addWidget(self.debug_export_button)
        layout.addWidget(toolbar)

        self.canvas = DetectionCanvas()
        self.canvas.entity_selected.connect(self._select_entity_in_tree)
        layout.addWidget(self.canvas, 1)

        self.settings_dialog = QDialog(self)
        self.settings_dialog.setModal(False)
        self.settings_dialog.resize(760, 420)
        settings_layout = QVBoxLayout(self.settings_dialog)
        settings_layout.setContentsMargins(12, 12, 12, 12)
        settings_layout.setSpacing(10)
        project_settings = QWidget()
        project_form = QFormLayout(project_settings)
        project_form.setContentsMargins(0, 0, 0, 0)
        self.project_orientation = NoWheelComboBox()
        self._populate_orientation_combo(self.project_orientation)
        self.project_snap_step = NoWheelDoubleSpinBox()
        self.project_snap_step.setRange(0.5, 1000.0)
        self.project_snap_step.setSingleStep(0.5)
        self.project_snap_step.setDecimals(1)
        self.project_snap_step.setValue(1.0)
        self.project_apply = QPushButton()
        self.project_apply.clicked.connect(self._apply_project_settings)
        self.project_orientation_label = QLabel()
        self.project_snap_label = QLabel()
        project_form.addRow(self.project_orientation_label, self.project_orientation)
        project_form.addRow(self.project_snap_label, self.project_snap_step)
        project_form.addRow(self.project_apply)
        settings_layout.addWidget(project_settings)

        self.mapping_table = QTableWidget(0, 3)
        self.mapping_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.mapping_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mapping_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        settings_layout.addWidget(self.mapping_table, 1)
        settings_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        settings_buttons.rejected.connect(self.settings_dialog.close)
        settings_layout.addWidget(settings_buttons)

        self.issues_dialog = QDialog(self)
        self.issues_dialog.setModal(False)
        self.issues_dialog.resize(560, 280)
        issues_layout = QVBoxLayout(self.issues_dialog)
        issue_search_layout = QHBoxLayout()
        issue_search_layout.setContentsMargins(0, 0, 0, 0)
        issue_search_layout.setSpacing(6)
        self.issue_search = QLineEdit()
        self.issue_search.setClearButtonEnabled(True)
        self.issue_search.returnPressed.connect(self._apply_issue_search)
        self.issue_search.textChanged.connect(self._clear_issue_search_if_empty)
        issue_search_layout.addWidget(self.issue_search, 1)
        self.issue_search_button = QPushButton()
        self.issue_search_button.setObjectName("agent3CompactButton")
        self.issue_search_button.clicked.connect(self._apply_issue_search)
        issue_search_layout.addWidget(self.issue_search_button)
        issues_layout.addLayout(issue_search_layout)
        self.issues = IssueListWidget()
        self.issues.setObjectName("agent3IssueList")
        self.issues.setItemDelegate(IssueItemDelegate(self.issues))
        self.issues.itemClicked.connect(self._select_issue)
        self.issues.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.issues.customContextMenuRequested.connect(
            self._show_issue_context_menu
        )
        issues_layout.addWidget(self.issues)
        issue_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        issue_buttons.rejected.connect(self.issues_dialog.close)
        issues_layout.addWidget(issue_buttons)
        return panel

    def _build_review_panel(self) -> QWidget:
        self.review_splitter = QSplitter(Qt.Orientation.Vertical)
        self.review_splitter.setObjectName("agent3ReviewSplitter")
        # All three panes may be dragged to any height, including fully
        # collapsed.
        self.review_splitter.setChildrenCollapsible(True)
        # The main splitter owns this pane's width, so no width floor.
        self.review_splitter.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.structure_tree = StructureTreeWidget()
        self.structure_tree.setObjectName("agent3StructureTree")
        self.structure_tree.setColumnCount(1)
        # Any height is allowed; overflowing content scrolls instead.
        self.structure_tree.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        self.structure_tree.reorder_requested.connect(self._reorder_selected)
        self.structure_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.structure_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.structure_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.structure_tree.customContextMenuRequested.connect(
            self._show_tree_context_menu
        )
        self.structure_tree.currentItemChanged.connect(self._show_properties)
        self.structure_tree.itemChanged.connect(self._rename_item)
        self.review_splitter.addWidget(self.structure_tree)

        property_panel = QFrame()
        property_panel.setObjectName("panel")
        property_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        property_layout = QVBoxLayout(property_panel)
        property_layout.setContentsMargins(0, 0, 0, 0)
        self.property_stack = QStackedWidget()
        self.property_empty = QLabel()
        self.property_empty.setObjectName("emptyHint")
        self.property_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.property_stack.addWidget(self.property_empty)

        self.property_scroll = QScrollArea()
        self.property_scroll.setObjectName("agent3PropertyScroll")
        self.property_scroll.setWidgetResizable(True)
        self.property_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.property_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        property_content = QWidget()
        property_content_layout = QVBoxLayout(property_content)
        property_content_layout.setContentsMargins(10, 8, 10, 10)
        property_content_layout.setSpacing(8)
        self.properties = QWidget()
        properties_layout = QVBoxLayout(self.properties)
        properties_layout.setContentsMargins(0, 0, 0, 0)
        properties_layout.setSpacing(8)
        self.property_title = QLabel()
        self.property_title.setObjectName("agent3PropertyTitle")
        properties_layout.addWidget(self.property_title)
        self.properties_form = QFormLayout()
        self.properties_form.setContentsMargins(0, 0, 0, 0)
        self.properties_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.properties_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows
        )
        properties_layout.addLayout(self.properties_form)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._apply_properties)
        self.orientation_combo = NoWheelComboBox()
        self._populate_orientation_combo(self.orientation_combo)
        self.snap_step = NoWheelDoubleSpinBox()
        self.snap_step.setRange(0.5, 1000.0)
        self.snap_step.setSingleStep(0.5)
        self.snap_step.setDecimals(1)
        self.node_strings = NoWheelSpinBox()
        self.node_strings.setRange(0, 100000)
        self.node_strings.setEnabled(False)
        self.tracker_counts = QLineEdit()
        self.tracker_counts.setEnabled(False)
        self.box_position = NoWheelDoubleSpinBox()
        self.box_position.setRange(-100000, 100000)
        self.box_position.setSingleStep(0.5)
        self.box_position.editingFinished.connect(self._apply_properties)
        self.recalculate_button = QPushButton()
        self.recalculate_button.clicked.connect(self._apply_settings)
        self.property_labels = {
            "name": QLabel(),
            "orientation": QLabel(),
            "snap": QLabel(),
            "strings": QLabel(),
            "trackers": QLabel(),
            "box": QLabel(),
        }
        for label in self.property_labels.values():
            label.setWordWrap(True)
        self.properties_form.addRow(self.property_labels["name"], self.name_edit)
        self.properties_form.addRow(
            self.property_labels["orientation"], self.orientation_combo
        )
        self.properties_form.addRow(self.property_labels["snap"], self.snap_step)
        self.properties_form.addRow(self.property_labels["box"], self.box_position)
        self.properties_form.addRow(
            self.property_labels["strings"], self.node_strings
        )
        self.properties_form.addRow(
            self.property_labels["trackers"], self.tracker_counts
        )
        self.properties_form.addRow(self.recalculate_button)
        property_content_layout.addWidget(self.properties)

        self.racking_table = ClickToEditTableWidget(0, 2)
        self.racking_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.racking_table.itemChanged.connect(self._apply_racking_edits)
        property_content_layout.addWidget(self.racking_table, 1)
        property_content_layout.addStretch()
        self.property_scroll.setWidget(property_content)
        self.property_stack.addWidget(self.property_scroll)
        property_layout.addWidget(self.property_stack)
        self.review_splitter.addWidget(property_panel)
        self.table_review_panel = self._build_table_review_panel()
        self.review_splitter.addWidget(self.table_review_panel)
        self.table_review_panel.hide()
        self.review_splitter.setStretchFactor(0, 11)
        self.review_splitter.setStretchFactor(1, 9)
        self.review_splitter.setStretchFactor(2, 6)
        self.review_splitter.setSizes([360, 300, 0])
        return self.review_splitter

    def _build_table_review_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.table_review_title = QLabel()
        self.table_review_title.setObjectName("agent3PropertyTitle")
        layout.addWidget(self.table_review_title)
        self.table_review_table = ClickToEditTableWidget(0, 2)
        self.table_review_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table_review_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table_review_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.table_review_table, 1)
        actions = QHBoxLayout()
        self.table_add_button = QPushButton()
        self.table_add_button.clicked.connect(self._add_table_row)
        self.table_delete_button = QPushButton()
        self.table_delete_button.clicked.connect(self._delete_table_rows)
        self.table_recalculate_button = QPushButton()
        self.table_recalculate_button.clicked.connect(
            self._apply_table_rows_and_recalculate
        )
        actions.addWidget(self.table_add_button)
        actions.addWidget(self.table_delete_button)
        actions.addStretch()
        actions.addWidget(self.table_recalculate_button)
        layout.addLayout(actions)
        return panel

    def _setup_shortcuts(self) -> None:
        bindings = (
            ("F2", self._rename_selected),
            ("Delete", self._delete_selected),
            ("Alt+F", self._add_child),
            ("Alt+D", self._delete_selected),
            ("Alt+C", self._copy_selected),
            ("Alt+V", self._paste_to_selected),
            ("Alt+1", lambda: self._expand_to_level(1)),
            ("Alt+2", lambda: self._expand_to_level(2)),
            ("Alt+3", lambda: self._expand_to_level(3)),
            ("Alt+Up", lambda: self._move_selected(-1)),
            ("Alt+Down", lambda: self._move_selected(1)),
        )
        for sequence, slot in bindings:
            shortcut = QShortcut(
                QKeySequence(sequence),
                self if sequence.startswith("Alt+") else self.structure_tree,
            )
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _populate_orientation_combo(self, combo: QComboBox) -> None:
        current = combo.currentData() if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        for orientation in _ORIENTATIONS:
            text = (
                tr(
                    "ai_statistics.element_recognition.orientation.unknown",
                    "Unknown",
                )
                if orientation is PanelOrientation.UNKNOWN
                else panel_orientation_text(orientation.value)
            )
            combo.addItem(
                text,
                orientation,
            )
        if current in _ORIENTATIONS:
            combo.setCurrentIndex(_ORIENTATIONS.index(current))
        combo.blockSignals(False)

    def _fit_canvas(self) -> None:
        self.canvas.fit_page()

    def _show_settings_dialog(self) -> None:
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _toggle_table_review(self, visible: bool) -> None:
        self.table_review_panel.setVisible(visible)
        if visible:
            sizes = self.review_splitter.sizes()
            if len(sizes) == 3 and sizes[2] == 0 and sum(sizes) > 0:
                # Reveal the table pane with a quarter of the height taken
                # from the taller pane, keeping the user's arrangement.
                share = sum(sizes) // 4
                first, second = sizes[0], sizes[1]
                if first >= second:
                    first = max(0, first - share)
                else:
                    second = max(0, second - share)
                self.review_splitter.setSizes([first, second, share])

    def _populate_table_rows(self, page=None) -> None:
        rows = tuple(getattr(page, "table_rows", ()) or ())
        if rows != self._table_rows_snapshot:
            # Repopulate only when the recognized rows changed, so an
            # unrelated tree update neither wipes a pending manual edit nor
            # resets the user's scroll position.
            self._table_rows_snapshot = rows
            scroll = self.table_review_table.verticalScrollBar().value()
            self.table_review_table.blockSignals(True)
            self.table_review_table.setRowCount(0)
            for row in rows:
                index = self.table_review_table.rowCount()
                self.table_review_table.insertRow(index)
                self.table_review_table.setItem(
                    index, 0, QTableWidgetItem(row.node)
                )
                self.table_review_table.setItem(
                    index, 1, QTableWidgetItem(str(row.string_number))
                )
            self.table_review_table.blockSignals(False)
            self.table_review_table.verticalScrollBar().setValue(scroll)
        enabled = page is not None and self._request is not None
        self.table_add_button.setEnabled(enabled)
        self.table_delete_button.setEnabled(enabled)
        self.table_recalculate_button.setEnabled(enabled)

    def _add_table_row(self) -> None:
        row = self.table_review_table.rowCount()
        self.table_review_table.insertRow(row)
        self.table_review_table.setItem(row, 0, QTableWidgetItem(""))
        self.table_review_table.setItem(row, 1, QTableWidgetItem("0"))
        self.table_review_table.setCurrentCell(row, 0)
        self.table_review_table.editItem(self.table_review_table.item(row, 0))

    def _delete_table_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.table_review_table.selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            self.table_review_table.removeRow(row)

    def _apply_table_rows_and_recalculate(self) -> None:
        if self._result is None or self._request is None:
            return
        page_data = self.page_combo.currentData()
        if page_data is None:
            return
        rows: list[NodeStringTableRow] = []
        try:
            for index in range(self.table_review_table.rowCount()):
                name_item = self.table_review_table.item(index, 0)
                count_item = self.table_review_table.item(index, 1)
                name = name_item.text().strip() if name_item is not None else ""
                count = int(
                    count_item.text().strip() if count_item is not None else ""
                )
                if not name or count < 0:
                    raise ValueError
                rows.append(NodeStringTableRow(name, count))
        except ValueError:
            QMessageBox.warning(
                self,
                tr(
                    "ai_statistics.element_recognition.table_invalid_title",
                    "Invalid table row",
                ),
                tr(
                    "ai_statistics.element_recognition.table_invalid_message",
                    "Each row needs a Node name and a non-negative integer string count.",
                ),
            )
            return
        try:
            updated = replace_table_rows_and_recalculate(
                self._result,
                self._request,
                int(page_data),
                rows,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Agent3", str(exc))
            return
        self._result = updated
        self._tree = updated.reviewed_tree
        self._render_tree()
        self._show_selected_page()
        self.table_review_changed.emit(updated)

    def _show_issues_dialog(self) -> None:
        # Muted issues still need the dialog: it is the only place to
        # restore them.
        if not self._issue_total:
            return
        self.issues_dialog.show()
        self.issues_dialog.raise_()
        self.issues_dialog.activateWindow()

    def debug_export_payload(self) -> dict[str, object]:
        """Return the current self-contained Agent3 diagnostic document."""

        if self._result is None:
            return {}
        return build_agent3_debug_payload(
            self._request,
            self._result,
            self._page_images,
        )

    def _export_debug_json(self) -> None:
        if self._result is None:
            return
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            tr(
                "ai_statistics.element_recognition.debug_export_title",
                "Export Agent3 Debug JSON",
            ),
            "agent3-debug.json",
            tr(
                "ai_statistics.element_recognition.json_filter",
                "JSON Files (*.json)",
            ),
        )
        if not file_path:
            return
        target = Path(file_path)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        try:
            target.write_text(
                json.dumps(
                    self.debug_export_payload(),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self,
                tr(
                    "ai_statistics.element_recognition.debug_export_failed",
                    "Debug Export Failed",
                ),
                str(exc),
            )
            return
        QMessageBox.information(
            self,
            tr(
                "ai_statistics.element_recognition.debug_exported",
                "Debug JSON Exported",
            ),
            tr(
                "ai_statistics.element_recognition.debug_exported_message",
                "Agent3 debug JSON saved to:\n{path}",
            ).format(path=str(target)),
        )

    def set_request(self, request: ElementRecognitionRequest | None) -> None:
        request_changed = request != self._request
        self._request = request
        if request_changed or self.mapping_table.rowCount() == 0:
            self._rebuild_settings()
        if self._result is None:
            self._refresh_page_options()

    def _rebuild_settings(self) -> None:
        self.mapping_table.setRowCount(0)
        if self._request is None:
            return
        request = self._request
        self.project_orientation.setCurrentIndex(
            _ORIENTATIONS.index(request.default_panel_orientation)
        )
        self.project_snap_step.setValue(
            request.default_offset_snap_step_strings
        )
        settings = {item.inv_name: item for item in request.inv_settings}
        for inv_name in request.expected_inv_names:
            row = self.mapping_table.rowCount()
            self.mapping_table.insertRow(row)
            self.mapping_table.setItem(row, 0, QTableWidgetItem(inv_name))
            inv_setting = settings.get(inv_name)
            orientation = (
                inv_setting.panel_orientation
                if inv_setting is not None
                and inv_setting.panel_orientation is not None
                else request.default_panel_orientation
            )
            snap_step = (
                inv_setting.offset_snap_step_strings
                if inv_setting is not None
                else request.default_offset_snap_step_strings
            )
            orientation_combo = NoWheelComboBox()
            self._populate_orientation_combo(orientation_combo)
            orientation_combo.setCurrentIndex(_ORIENTATIONS.index(orientation))
            snap_editor = NoWheelDoubleSpinBox()
            snap_editor.setRange(0.5, 1000.0)
            snap_editor.setSingleStep(0.5)
            snap_editor.setDecimals(1)
            snap_editor.setValue(snap_step)
            orientation_combo.currentIndexChanged.connect(
                lambda _index, inv=inv_name, combo=orientation_combo, snap=snap_editor: self.inv_settings_changed.emit(
                    inv, combo.currentData(), snap.value()
                )
            )
            snap_editor.editingFinished.connect(
                lambda inv=inv_name, combo=orientation_combo, snap=snap_editor: self.inv_settings_changed.emit(
                    inv, combo.currentData(), snap.value()
                )
            )
            self.mapping_table.setCellWidget(row, 1, orientation_combo)
            self.mapping_table.setCellWidget(row, 2, snap_editor)

    def set_result(
        self,
        result: ElementRecognitionResult | None,
        page_images: dict[int, bytes] | None = None,
    ) -> None:
        current_page = self.page_combo.currentData()
        previous_result = self._result
        self._result = result
        self._page_images = page_images or {}
        self.debug_export_button.setEnabled(result is not None)
        if result is None:
            self._tree = ReviewedTree()
            self._render_tree()
            self._refresh_page_options(current_page)
            self._populate_issues()
            self._populate_table_rows()
            return
        # Tree edits publish the updated tree through the workflow before the
        # editing method calls _render_tree itself.  Rebuilding here as well
        # would duplicate that work and discard the in-place selection and
        # scroll position.
        tree_changed = previous_result is None or (
            result.reviewed_tree is not self._tree
        )
        if tree_changed:
            selected_path, selected_entity, expanded = self._capture_tree_state()
        else:
            selected_path, selected_entity, expanded = None, "", None
        self._tree = result.reviewed_tree
        self._refresh_page_options(current_page)
        if tree_changed:
            self._render_tree(selected_path, selected_entity, expanded)
        self._populate_issues()
        self._show_selected_page()

    def reveal_inv(
        self,
        inv_name: str,
        page: int | None = None,
        recognition_id: str = "",
    ) -> bool:
        """Open one INV's page and select its branch.

        Agent4 calls this when a comparison row is activated so the user
        lands on the drawing that produced the row instead of searching for
        it in the page combo.
        """

        self._show_page_number(
            page if page is not None else self._page_number_for_inv(inv_name)
        )
        item = self._inv_item(inv_name, recognition_id)
        if item is None:
            return False
        self.structure_tree.setCurrentItem(item)
        return True

    def reveal_node(
        self,
        inv_name: str,
        node_name: str,
        recognition_id: str = "",
        page: int | None = None,
    ) -> bool:
        """Open the Node's page and select it in the structure tree.

        A Node with a live recognition link is selected through the canvas
        entity, which also switches to the page owning its detection. Nodes
        that lost the link (manual additions or edits) fall back to the
        INV/name path so the row still lands on the right branch. A page
        supplied by the caller (Agent4's table-only rows) wins over the INV
        lookup so the row opens the page that produced it.
        """

        if recognition_id:
            self._select_entity_in_tree(recognition_id)
            if self._item_for_entity(recognition_id) is not None:
                return True
        page_number = (
            page if page is not None else self._page_number_for_inv(inv_name)
        )
        item = self._node_item(inv_name, node_name)
        if item is None:
            # An Agent4 table-only row has no structure Node to select; open
            # its page so the drawing and the reviewed table stay inspectable.
            self._show_page_number(page_number)
            return False
        self._show_page_number(page_number)
        self.structure_tree.setCurrentItem(item)
        return True

    def _refresh_page_options(self, preferred_page=None) -> None:
        current_page = (
            preferred_page
            if preferred_page is not None
            else self.page_combo.currentData()
        )
        options: list[tuple[int, str]] = []
        if self._result is not None:
            options = [
                (page.page_number, page.inv_name) for page in self._result.pages
            ]
        elif self._request is not None:
            options = [
                (page.page_number, "") for page in self._request.pages
            ]

        # Rebuilding the combo on every publish flashes the popup and re-runs
        # the translation lookup; only a changed option list needs it.
        entries: list[tuple[str, int]] = []
        for page_number, inv_name in options:
            label = tr(
                "ai_statistics.element_recognition.page_label",
                "Page {page}",
            ).format(page=page_number)
            if inv_name:
                label = f"{label} — {inv_name}"
            entries.append((label, page_number))
        if tuple(entries) != self._page_entries:
            self._page_entries = tuple(entries)
            self.page_combo.blockSignals(True)
            self.page_combo.clear()
            for label, page_number in entries:
                self.page_combo.addItem(label, page_number)
            self.page_combo.blockSignals(False)
        preferred_index = self.page_combo.findData(current_page)
        if (
            preferred_index >= 0
            and preferred_index != self.page_combo.currentIndex()
        ):
            self.page_combo.blockSignals(True)
            self.page_combo.setCurrentIndex(preferred_index)
            self.page_combo.blockSignals(False)
        self._show_selected_page()

    def _show_selected_page(self) -> None:
        if self.page_combo.currentIndex() < 0:
            self.canvas.clear_page()
            self._populate_table_rows()
            return
        page_number = int(self.page_combo.currentData())
        page = (
            next(
                (
                    item
                    for item in self._result.pages
                    if item.page_number == page_number
                ),
                None,
            )
            if self._result is not None
            else None
        )
        image = self._page_images.get(page_number, b"")
        if not image and self._request is not None:
            request_page = next(
                (
                    item
                    for item in self._request.pages
                    if item.page_number == page_number
                ),
                None,
            )
            image = request_page.png_bytes if request_page is not None else b""
        self.canvas.show_page(image, page)
        self._populate_table_rows(page)
        current = self.structure_tree.currentItem()
        entity_id = current.data(0, _ENTITY_ROLE) if current is not None else ""
        if entity_id:
            self.canvas.select_entity(str(entity_id))
        else:
            # An item without a recognition link must not keep another Node's
            # detection selected: every later preview click would emit that
            # stale entity and navigate straight back to the other Node.
            self.canvas.clear_selection()

    def _populate_issues(self) -> None:
        selected_key = None
        selected_item = self.issues.currentItem()
        if selected_item is not None:
            selected_key = selected_item.data(_ISSUE_KEY_ROLE)
        scroll = self.issues.verticalScrollBar().value()
        self.issues.clear()
        if self._result is None:
            self._issue_count = 0
            self._issue_total = 0
            self._muted_issue_keys.clear()
            self.issues_button.hide()
            self.issues_dialog.hide()
            return
        unique_issues = self._result.all_issues

        self._issue_total = len(unique_issues)
        active_issues = [
            issue
            for issue in unique_issues
            if self._issue_key(issue) not in self._muted_issue_keys
        ]
        muted_issues = [
            issue
            for issue in unique_issues
            if self._issue_key(issue) in self._muted_issue_keys
        ]
        # The button counts only issues the user still cares about; search
        # narrows the list but never the counter.
        self._issue_count = len(active_issues)
        query = self._issue_query.casefold()
        if query:
            active_issues = [
                issue
                for issue in active_issues
                if query in self._issue_search_text(issue)
            ]
            muted_issues = [
                issue
                for issue in muted_issues
                if query in self._issue_search_text(issue)
            ]

        selected_page = self._selected_issue_page()
        relevant = [
            issue
            for issue in active_issues
            if selected_page is not None and issue.page_number == selected_page
        ]
        other = [issue for issue in active_issues if issue not in relevant]
        relevant.sort(key=self._issue_sort_key)
        other.sort(key=self._issue_sort_key)
        muted_issues.sort(key=self._issue_sort_key)
        self._add_issue_items(relevant)
        self._add_issue_items(other)
        # Muted issues sink below every active issue so the muted state is
        # not just a color change.
        self._add_issue_items(muted_issues)
        if selected_key is not None:
            for row in range(self.issues.count()):
                item = self.issues.item(row)
                if item.data(_ISSUE_KEY_ROLE) == selected_key:
                    self.issues.setCurrentItem(item)
                    break
        self.issues.set_divider_before_row(len(relevant) if relevant and other else None)
        self.issues.verticalScrollBar().setValue(scroll)
        self.issues_button.setVisible(self._issue_total > 0)
        self._update_issue_button_text()

    def _apply_issue_search(self) -> None:
        self._issue_query = self.issue_search.text().strip()
        self._populate_issues()

    def _clear_issue_search_if_empty(self, text: str) -> None:
        if not text and self._issue_query:
            self._issue_query = ""
            self._populate_issues()

    def _selected_issue_page(self) -> int | None:
        current = self.structure_tree.currentItem()
        if current is None or self._result is None:
            return None
        path_data = current.data(0, _PATH_ROLE)
        if path_data is not None:
            value = self._draft_at(tuple(path_data))
            if value is not None:
                page_number = getattr(
                    getattr(value, "source", None), "page_number", None
                )
                if page_number is None:
                    page_number = getattr(value, "page", None)
                if page_number is not None:
                    return int(page_number)
        entity_id = str(current.data(0, _ENTITY_ROLE) or "")
        return next(
            (
                page.page_number
                for page in self._result.pages
                if any(
                    detection.entity_id == entity_id
                    for detection in page.detections
                )
            ),
            None,
        )

    def _issue_search_text(self, issue: RecognitionIssue) -> str:
        severity = tr(
            f"ai_statistics.element_recognition.severity.{issue.severity.value}",
            issue.severity.value,
        )
        page_label = (
            tr(
                "ai_statistics.element_recognition.page_label",
                "Page {page}",
            ).format(page=issue.page_number)
            if issue.page_number is not None
            else ""
        )
        return " ".join(
            (
                issue.code,
                issue.message,
                format_issue_message(issue, tr),
                issue.severity.value,
                severity,
                page_label,
                str(issue.page_number or ""),
                issue.entity_id,
            )
        ).casefold()

    @staticmethod
    def _issue_sort_key(issue: RecognitionIssue) -> tuple[object, ...]:
        severity_rank = 0 if issue.severity.value == "error" else 1
        return (
            severity_rank,
            issue.page_number if issue.page_number is not None else 10**9,
            issue.code.casefold(),
            issue.message.casefold(),
        )

    @staticmethod
    def _issue_key(issue: RecognitionIssue) -> tuple[object, ...]:
        """Return the identity that one issue keeps across publications."""

        return issue.key

    def _add_issue_items(self, issues: list[RecognitionIssue]) -> None:
        for issue in issues:
            severity = tr(
                f"ai_statistics.element_recognition.severity.{issue.severity.value}",
                issue.severity.value,
            )
            page_label = (
                tr(
                    "ai_statistics.element_recognition.page_label",
                    "Page {page}",
                ).format(page=issue.page_number)
                if issue.page_number is not None
                else tr(
                    "ai_statistics.element_recognition.project_issue",
                    "Project",
                )
            )
            list_item = QListWidgetItem(
                f"[{severity}] {page_label} · {format_issue_message(issue, tr)}"
            )
            key = self._issue_key(issue)
            muted = key in self._muted_issue_keys
            list_item.setData(_ISSUE_ENTITY_ROLE, issue.entity_id)
            list_item.setData(_ISSUE_PAGE_ROLE, issue.page_number)
            list_item.setData(_ISSUE_SEVERITY_ROLE, issue.severity.value)
            list_item.setData(_ISSUE_KEY_ROLE, key)
            list_item.setData(_ISSUE_MUTED_ROLE, muted)
            tooltip_parts = [
                part
                for part in (issue.code, page_label, issue.entity_id)
                if part
            ]
            if muted:
                tooltip_parts.append(
                    tr(
                        "ai_statistics.element_recognition.issue_muted",
                        "Ignored",
                    )
                )
            list_item.setToolTip(" · ".join(tooltip_parts))
            self.issues.addItem(list_item)

    def _capture_tree_state(
        self,
    ) -> tuple[tuple[int, ...] | None, str, set[tuple[int, ...]]]:
        current = self.structure_tree.currentItem()
        selected_path = (
            tuple(current.data(0, _PATH_ROLE))
            if current is not None
            else None
        )
        selected_entity = (
            str(current.data(0, _ENTITY_ROLE) or "")
            if current is not None
            else ""
        )
        expanded = {
            tuple(item.data(0, _PATH_ROLE))
            for item in QTreeWidgetItemIteratorCompat(self.structure_tree)
            if item.isExpanded()
        }
        return selected_path, selected_entity, expanded

    def _render_tree(
        self,
        select_path: tuple[int, ...] | None = None,
        select_entity: str = "",
        expanded_paths: set[tuple[int, ...]] | None = None,
        selected_paths: set[tuple[int, ...]] | None = None,
    ) -> None:
        old_path, old_entity, old_expanded = self._capture_tree_state()
        explicit_path = select_path is not None
        if select_path is None:
            select_path = old_path
        if not select_entity and not explicit_path:
            select_entity = old_entity
        if expanded_paths is None:
            expanded_paths = old_expanded
        had_tree = self.structure_tree.topLevelItemCount() > 0
        vertical_scroll = self.structure_tree.verticalScrollBar().value()
        horizontal_scroll = self.structure_tree.horizontalScrollBar().value()
        self._tree_updating = True
        self.structure_tree.blockSignals(True)
        self.structure_tree.clear()
        for inv_index, inv in enumerate(self._tree.invs):
            inv_item = self._tree_item(inv.name, "INV", (inv_index,), inv.recognition_id)
            self.structure_tree.addTopLevelItem(inv_item)
            for node_index, node in enumerate(inv.nodes):
                node_item = self._tree_item(
                    node.name, "Node", (inv_index, node_index), node.recognition_id
                )
                inv_item.addChild(node_item)
                for row_index, row in enumerate(node.rows):
                    row_item = self._tree_item(
                        self._row_tree_label(row),
                        "Row",
                        (inv_index, node_index, row_index),
                        row.recognition_id,
                    )
                    node_item.addChild(row_item)
            inv_item.setExpanded(
                (inv_index,) in expanded_paths or not had_tree
            )
            node_item_count = inv_item.childCount()
            for node_index in range(node_item_count):
                node_item = inv_item.child(node_index)
                node_item.setExpanded((inv_index, node_index) in expanded_paths)
        item = self._item_for_entity(select_entity) if select_entity else None
        if item is None and select_path is not None:
            item = self._item_for_path(select_path)
        self.structure_tree.blockSignals(False)
        self._tree_updating = False
        if selected_paths:
            self.structure_tree.apply_selected_ids(tuple(sorted(selected_paths)))
            item = self.structure_tree.currentItem()
        elif item is not None:
            self.structure_tree.setCurrentItem(item)
        if item is not None:
            self._show_properties(item)
        else:
            self._show_properties(None)
        # clear() sent the scrollbars back to the top; an edit must not make
        # the user lose their place in a long structure.
        self.structure_tree.verticalScrollBar().setValue(vertical_scroll)
        self.structure_tree.horizontalScrollBar().setValue(horizontal_scroll)

    @staticmethod
    def _row_tree_label(row: RowDraft) -> str:
        offsets = [
            entry.length_str.strip()
            for entry in row.racking_entries
            if entry.length_str.strip()
            and not _ZERO_OFFSET_RE.fullmatch(
                entry.length_str.replace(" ", "")
            )
        ]
        return f"{row.name} ({', '.join(offsets)})" if offsets else row.name

    def _tree_item(
        self, name: str, kind: str, path: tuple[int, ...], entity_id: str
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem((name,))
        item.setData(0, _PATH_ROLE, path)
        item.setData(0, _ENTITY_ROLE, entity_id)
        item.setData(0, _KIND_ROLE, kind)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        return item

    def _item_for_entity(self, entity_id: str) -> QTreeWidgetItem | None:
        if not entity_id:
            return None
        return next(
            (
                item
                for item in QTreeWidgetItemIteratorCompat(self.structure_tree)
                if item.data(0, _ENTITY_ROLE) == entity_id
            ),
            None,
        )

    def _inv_item(
        self,
        inv_name: str,
        recognition_id: str = "",
    ) -> QTreeWidgetItem | None:
        if recognition_id:
            item = self._item_for_entity(recognition_id)
            if item is not None:
                return item
        return next(
            (
                self.structure_tree.topLevelItem(index)
                for index in range(self.structure_tree.topLevelItemCount())
                if self.structure_tree.topLevelItem(index).text(0) == inv_name
            ),
            None,
        )

    def _node_item(self, inv_name: str, node_name: str) -> QTreeWidgetItem | None:
        inv_item = self._inv_item(inv_name)
        if inv_item is None:
            return None
        return next(
            (
                inv_item.child(index)
                for index in range(inv_item.childCount())
                if inv_item.child(index).text(0) == node_name
            ),
            None,
        )

    def _page_number_for_inv(self, inv_name: str) -> int | None:
        if self._result is None:
            return None
        return next(
            (
                page.page_number
                for page in self._result.pages
                if page.inv_name == inv_name
            ),
            None,
        )

    def _show_page_number(self, page_number: int | None) -> None:
        if page_number is None:
            return
        index = self.page_combo.findData(page_number)
        if index >= 0:
            self.page_combo.setCurrentIndex(index)

    def _item_for_path(self, path: tuple[int, ...]) -> QTreeWidgetItem | None:
        if not path or path[0] >= self.structure_tree.topLevelItemCount():
            return None
        item = self.structure_tree.topLevelItem(path[0])
        for index in path[1:]:
            if index >= item.childCount():
                return None
            item = item.child(index)
        return item

    def _selected_path(self) -> tuple[int, ...] | None:
        item = self.structure_tree.currentItem()
        return tuple(item.data(0, _PATH_ROLE)) if item is not None else None

    def _object_at(self, path: tuple[int, ...]):
        inv = self._tree.invs[path[0]]
        if len(path) == 1:
            return inv
        node = inv.nodes[path[1]]
        if len(path) == 2:
            return node
        return node.rows[path[2]]

    def _draft_at(self, path: tuple[int, ...] | None):
        """Return the draft at *path*, or None when it no longer exists.

        An edit publishes its updated tree through the workflow before the
        structure widget is rebuilt, so the widget can hold a path that no
        longer addresses the current tree.  Readers of the current item must
        therefore validate the path instead of indexing blindly.
        """

        if not path:
            return None
        if path[0] >= len(self._tree.invs):
            return None
        inv = self._tree.invs[path[0]]
        if len(path) == 1:
            return inv
        if path[1] >= len(inv.nodes):
            return None
        node = inv.nodes[path[1]]
        if len(path) == 2:
            return node
        if path[2] >= len(node.rows):
            return None
        return node.rows[path[2]]

    def _replace_at(self, path: tuple[int, ...], value) -> None:
        invs = list(self._tree.invs)
        if len(path) == 1:
            invs[path[0]] = value
        else:
            inv = invs[path[0]]
            nodes = list(inv.nodes)
            if len(path) == 2:
                if isinstance(value, NodeDraft):
                    value = self._with_tracker_summary(value)
                nodes[path[1]] = value
            else:
                node = nodes[path[1]]
                rows = list(node.rows)
                rows[path[2]] = value
                nodes[path[1]] = self._with_tracker_summary(
                    replace(node, rows=tuple(rows))
                )
            invs[path[0]] = replace(inv, nodes=tuple(nodes))
        self._tree = replace(self._tree, invs=tuple(invs))
        self.review_changed.emit(self._tree)

    def _tracker_definitions(self) -> tuple[TrackerDefinition, ...]:
        return (
            self._request.tracker_definitions
            if self._request is not None
            else ()
        )

    def _table_rows_for_node(
        self,
        node: NodeDraft,
    ) -> tuple[NodeStringTableRow, ...]:
        """Return the reviewed OCR table rows that can hold the Node's count.

        A recognized Node resolves against its own page only. A user-created
        Node (manual Add or paste) carries no page anchor, so its name is
        looked up across every reviewed table; ``resolve_table_string_count``
        accepts the count only when the normalized name matches exactly one
        row, which keeps an ambiguous name unresolved instead of guessing.
        """

        if self._result is None:
            return ()
        page_number = node.source.page_number
        if page_number is not None:
            return next(
                (
                    page.table_rows
                    for page in self._result.pages
                    if page.page_number == page_number
                ),
                (),
            )
        return tuple(
            row
            for page in self._result.pages
            for row in page.table_rows
        )

    def _with_table_ocr_reference(self, node: NodeDraft) -> NodeDraft:
        """Re-resolve the Node's OCR count after its name was edited.

        The table-recognition count is keyed by the Node name, so a manual
        correction must look the row up again; otherwise the edited tree keeps
        the failed recognition's empty value and Agent4 receives no OCR
        reference for the corrected Node.
        """

        count, present = resolve_table_string_count(
            node.name,
            self._table_rows_for_node(node),
        )
        if not present:
            return node
        return replace(
            node,
            ocr_string_count=count,
            has_ocr_string_count=True,
        )

    def _with_tracker_summary(self, node: NodeDraft) -> NodeDraft:
        """Refresh row-derived Tracker totals without touching OCR provenance."""

        _, tracker_counts = summarize_node_rows(
            node,
            tracker_definitions=self._tracker_definitions(),
        )
        return replace(node, tracker_counts=tracker_counts)

    def _show_properties(self, current: QTreeWidgetItem | None, _previous=None) -> None:
        self._populate_issues()
        path = self._selected_path()
        if path is None:
            self.property_stack.setCurrentWidget(self.property_empty)
            return
        value = self._object_at(path)
        self.property_stack.setCurrentWidget(self.property_scroll)
        self._property_updating = True
        self.name_edit.setText(value.name)
        is_node = isinstance(value, NodeDraft)
        is_inv = isinstance(value, InvDraft)
        is_row = isinstance(value, RowDraft)
        kind = "inv" if is_inv else "node" if is_node else "row"
        self.property_title.setText(
            tr(
                f"ai_statistics.element_recognition.{kind}_properties",
                f"{kind.upper() if kind == 'inv' else kind.title()} Properties",
            )
        )
        self._set_property_row_visible(self.name_edit, True)
        self._set_property_row_visible(self.orientation_combo, is_inv or is_node)
        self._set_property_row_visible(self.snap_step, is_inv or is_node)
        self._set_property_row_visible(self.node_strings, is_node)
        self._set_property_row_visible(self.tracker_counts, is_node)
        self._set_property_row_visible(self.box_position, is_node)
        self.properties_form.setRowVisible(
            self.recalculate_button,
            is_inv or is_node,
        )
        orientation_value = getattr(value, "panel_orientation", "") or "unknown"
        orientation = _orientation_from_value(orientation_value)
        self.orientation_combo.setCurrentIndex(_ORIENTATIONS.index(orientation))
        self.snap_step.setValue(getattr(value, "offset_snap_step_strings", 1.0))
        self.box_position.setEnabled(is_node)
        string_count, tracker_counts = (
            summarize_node_rows(
                value,
                tracker_definitions=self._tracker_definitions(),
            )
            if is_node
            else (0, ())
        )
        self.node_strings.setValue(string_count)
        self.tracker_counts.setText(
            ", ".join(
                f"{quantity.tracker_type}={quantity.tracker_count}"
                for quantity in tracker_counts
            )
        )
        self.box_position.setValue(getattr(value, "box_position", 0.0))
        self._property_updating = False
        self.racking_table.setVisible(is_row)
        self._render_rackings(value if is_row else None)
        entity_id = current.data(0, _ENTITY_ROLE) if current else ""
        if entity_id:
            self._focus_entity_on_canvas(str(entity_id), value)
        else:
            # A manually added or pasted item carries no recognition link;
            # keeping the previously selected detection box would make the
            # next preview click jump back to that other Node.
            self.canvas.clear_selection()

    def _set_property_row_visible(self, widget: QWidget, visible: bool) -> None:
        self.properties_form.setRowVisible(widget, visible)

    def _focus_entity_on_canvas(self, entity_id: str, value=None) -> None:
        page_number = None
        if self._result is not None:
            page_number = next(
                (
                    page.page_number
                    for page in self._result.pages
                    if any(
                        detection.entity_id == entity_id
                        for detection in page.detections
                    )
                ),
                None,
            )
        if page_number is None and value is not None:
            page_number = getattr(getattr(value, "source", None), "page_number", None)
        if page_number is not None:
            page_index = self.page_combo.findData(page_number)
            if page_index >= 0 and page_index != self.page_combo.currentIndex():
                self.page_combo.setCurrentIndex(page_index)
        self.canvas.select_entity(entity_id)

    def _rename_item(self, item: QTreeWidgetItem, column: int) -> None:
        if self._tree_updating or column != 0:
            return
        path = tuple(item.data(0, _PATH_ROLE))
        value = self._object_at(path)
        name = item.text(0).strip()
        if isinstance(value, RowDraft):
            label = self._row_tree_label(value)
            suffix = label[len(value.name) :]
            if suffix and name.endswith(suffix):
                name = name[: -len(suffix)].strip()
        updated = replace(value, name=name or value.name)
        if isinstance(updated, NodeDraft) and updated.name != value.name:
            updated = self._with_table_ocr_reference(updated)
        self._replace_at(path, updated)
        self._render_tree(path)

    def _apply_properties(self) -> None:
        if self._property_updating:
            return
        path = self._selected_path()
        if path is None:
            return
        value = self._object_at(path)
        changes = {"name": self.name_edit.text().strip() or value.name}
        if isinstance(value, NodeDraft):
            # String/tracker totals are derived from rows, while the OCR
            # presence flag records table-recognition provenance. A property
            # edit re-resolves that reference by name but must not rewrite the
            # row-derived totals.
            changes.update(
                box_position=self.box_position.value(),
            )
        updated = replace(value, **changes)
        if isinstance(updated, NodeDraft) and updated.name != value.name:
            updated = self._with_table_ocr_reference(updated)
        self._replace_at(path, updated)
        self._render_tree(path)

    def _apply_settings(self) -> None:
        path = self._selected_path()
        if path is None or len(path) > 2:
            return
        orientation = self.orientation_combo.currentData()
        snap_step = self.snap_step.value()
        value = self._object_at(path)
        if isinstance(value, NodeDraft):
            pending = replace(
                value,
                name=self.name_edit.text().strip() or value.name,
                box_position=self.box_position.value(),
            )
            # Keep the full Agent3 result synchronized with edits made in the
            # tree. A renamed Node re-resolves this page's reviewed string
            # count and rebuilds its geometry from the matching table row;
            # an unchanged name keeps the Node's Rows, so Apply never discards
            # manually edited Racking Entries (and the string total Agent4
            # derives from them). Limit the rebuild to the current Node so
            # Apply never overwrites manual work on its siblings.
            rebuilt = pending
            updated_result = None
            page_number = pending.source.page_number
            if (
                self._result is not None
                and self._request is not None
                and pending.recognition_id
                and page_number is not None
                and pending.name != value.name
            ):
                invs = list(self._tree.invs)
                inv = invs[path[0]]
                nodes = list(inv.nodes)
                nodes[path[1]] = pending
                invs[path[0]] = replace(inv, nodes=tuple(nodes))
                pending_tree = replace(self._tree, invs=tuple(invs))
                base_result = replace(self._result, reviewed_tree=pending_tree)
                page_result = next(
                    (
                        page
                        for page in base_result.pages
                        if page.page_number == page_number
                    ),
                    None,
                )
                if page_result is not None:
                    updated_result = replace_table_rows_and_recalculate(
                        base_result,
                        self._request,
                        page_number,
                        page_result.table_rows,
                        target_node_id=pending.recognition_id,
                    )
                    rebuilt = updated_result.reviewed_tree.invs[path[0]].nodes[
                        path[1]
                    ]
            rebuilt = recalculate_node(
                replace(rebuilt, box_position=pending.box_position),
                orientation,
                snap_step,
                tracker_definitions=self._tracker_definitions(),
            )
            rebuilt = self._with_table_ocr_reference(rebuilt)
            self._replace_at(path, rebuilt)
            if updated_result is not None:
                self._result = replace(updated_result, reviewed_tree=self._tree)
        else:
            nodes = tuple(
                recalculate_node(
                    node,
                    orientation,
                    snap_step,
                    inherit_orientation=True,
                    inherit_snap_step=True,
                    tracker_definitions=self._tracker_definitions(),
                )
                for node in value.nodes
            )
            updated = replace(
                value,
                name=self.name_edit.text().strip() or value.name,
                nodes=nodes,
                panel_orientation=orientation.value,
                offset_snap_step_strings=snap_step,
                inherits_panel_orientation=False,
                inherits_offset_snap_step=False,
            )
            self._replace_at(path, updated)
            self.inv_settings_changed.emit(
                updated.name,
                orientation,
                snap_step,
            )
        self._render_tree(path)

    def _apply_project_settings(self) -> None:
        orientation = self.project_orientation.currentData()
        snap_step = self.project_snap_step.value()
        if self._result is not None:
            answer = QMessageBox.question(
                self,
                tr(
                    "ai_statistics.element_recognition.recalculate_title",
                    "Recalculate derived values",
                ),
                tr(
                    "ai_statistics.element_recognition.recalculate_project_confirm",
                    "This replaces manually edited P/N racking names and offsets for all inherited Nodes. Continue?",
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            invs = []
            for inv in self._tree.invs:
                inv_orientation = (
                    orientation
                    if inv.inherits_panel_orientation
                    else _orientation_from_value(inv.panel_orientation)
                )
                inv_snap_step = (
                    snap_step
                    if inv.inherits_offset_snap_step
                    else inv.offset_snap_step_strings
                )
                nodes = []
                for node in inv.nodes:
                    node_orientation = (
                        inv_orientation
                        if node.inherits_panel_orientation
                        else _orientation_from_value(node.panel_orientation)
                    )
                    node_snap_step = (
                        inv_snap_step
                        if node.inherits_offset_snap_step
                        else node.offset_snap_step_strings
                    )
                    nodes.append(
                        recalculate_node(
                            node,
                            node_orientation,
                            node_snap_step,
                            inherit_orientation=node.inherits_panel_orientation,
                            inherit_snap_step=node.inherits_offset_snap_step,
                            tracker_definitions=self._tracker_definitions(),
                        )
                    )
                invs.append(
                    replace(
                        inv,
                        nodes=tuple(nodes),
                        panel_orientation=inv_orientation.value,
                        offset_snap_step_strings=inv_snap_step,
                    )
                )
            self._tree = replace(self._tree, invs=tuple(invs))
            self.review_changed.emit(self._tree)
            self._render_tree()
        self.project_settings_changed.emit(orientation, snap_step)

    def _render_rackings(self, row: RowDraft | None) -> None:
        self.racking_table.blockSignals(True)
        self.racking_table.setRowCount(0)
        if row is not None:
            for entry in row.racking_entries:
                index = self.racking_table.rowCount()
                self.racking_table.insertRow(index)
                self.racking_table.setItem(index, 0, QTableWidgetItem(entry.rackings))
                self.racking_table.setItem(index, 1, QTableWidgetItem(entry.length_str))
        self.racking_table.blockSignals(False)

    def _apply_racking_edits(self, _item: QTableWidgetItem) -> None:
        path = self._selected_path()
        if path is None or len(path) != 3:
            return
        row = self._object_at(path)
        entries = []
        for index, old in enumerate(row.racking_entries):
            rackings = self.racking_table.item(index, 0)
            offset = self.racking_table.item(index, 1)
            entries.append(
                replace(
                    old,
                    rackings=rackings.text() if rackings else old.rackings,
                    length_str=offset.text() if offset else old.length_str,
                )
            )
        self._replace_at(
            path,
            replace(row, racking_entries=tuple(entries), rackings=tuple(e.rackings for e in entries)),
        )
        self._render_tree(path)

    def _selected_paths(self) -> tuple[tuple[int, ...], ...]:
        return tuple(
            sorted(
                tuple(item.data(0, _PATH_ROLE))
                for item in self.structure_tree.selectedItems()
            )
        )

    def _selected_sibling_paths(self) -> tuple[tuple[int, ...], ...]:
        paths = self._selected_paths()
        if not paths:
            return ()
        parent = paths[0][:-1]
        return paths if all(path[:-1] == parent for path in paths) else ()

    def _siblings_at(self, parent_path: tuple[int, ...]) -> tuple:
        if not parent_path:
            return self._tree.invs
        parent = self._object_at(parent_path)
        if isinstance(parent, InvDraft):
            return parent.nodes
        if isinstance(parent, NodeDraft):
            return parent.rows
        return ()

    def _replace_siblings(self, parent_path: tuple[int, ...], values: tuple) -> None:
        if not parent_path:
            self._tree = replace(self._tree, invs=values)
            self.review_changed.emit(self._tree)
            return
        parent = self._object_at(parent_path)
        field = "nodes" if isinstance(parent, InvDraft) else "rows"
        self._replace_at(parent_path, replace(parent, **{field: values}))

    @staticmethod
    def _name_sort_key(value) -> tuple[tuple[int, object], ...]:
        return natural_text_key(value.name)

    def _sort_selected(self, descending: bool = False) -> None:
        paths = self._selected_sibling_paths()
        if len(paths) < 2:
            return
        parent_path = paths[0][:-1]
        values = list(self._siblings_at(parent_path))
        positions = [path[-1] for path in paths]
        selected = [values[index] for index in positions]
        if not sort_selected_slots(
            values,
            selected,
            self._name_sort_key,
            descending=descending,
        ):
            return
        self._replace_siblings(parent_path, tuple(values))
        self._render_tree(paths[0], selected_paths=set(paths))

    def _reorder_selected(
        self,
        target_path: tuple[int, ...] | None,
        after: bool,
    ) -> None:
        paths = self._selected_sibling_paths()
        if not paths:
            return
        parent_path = paths[0][:-1]
        if any(path[:-1] != parent_path for path in paths) or (
            target_path is not None and target_path[:-1] != parent_path
        ):
            return
        values = list(self._siblings_at(parent_path))
        positions = [path[-1] for path in paths]
        if any(index >= len(values) for index in positions):
            return
        moving = [values[index] for index in positions]
        moving_ids = {id(value) for value in moving}
        target = values[target_path[-1]] if target_path is not None else None
        if not move_selected_items(values, moving, target, after=after):
            return
        self._replace_siblings(parent_path, tuple(values))
        new_positions = {
            (*parent_path, index)
            for index, value in enumerate(values)
            if id(value) in moving_ids
        }
        first = min(new_positions) if new_positions else parent_path
        self._render_tree(first, selected_paths=new_positions)

    def _add_child(self) -> None:
        path = self._selected_path()
        if path is None:
            self._add_inv()
        elif len(path) == 1:
            self._add_node()
        elif len(path) == 2:
            self._add_row()

    def _copy_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        self._clipboard = self._copy_without_recognition_links(self._object_at(path))

    def _copy_without_recognition_links(self, value):
        """Return a clipboard copy detached from all recognition references.

        The user renames every pasted item, so the copy is a new business
        object: it keeps only the editable structure and drops the source's
        page anchor, detection annotations, recognition id, INV page, and
        table OCR reference. A detached Node still resolves an OCR count by
        its (later typed) name, exactly like a manually added Node.
        """

        if isinstance(value, InvDraft):
            return replace(
                value,
                page=None,
                recognition_id="",
                source=SourceReference(),
                nodes=tuple(
                    self._copy_without_recognition_links(node) for node in value.nodes
                ),
            )
        if isinstance(value, NodeDraft):
            return replace(
                value,
                recognition_id="",
                source=SourceReference(),
                annotations=(),
                ocr_string_count=0,
                has_ocr_string_count=False,
                rows=tuple(
                    self._copy_without_recognition_links(row) for row in value.rows
                ),
            )
        return replace(
            value,
            recognition_id="",
            source=SourceReference(),
        )

    def _paste_to_selected(self) -> None:
        value = self._clipboard
        if value is None:
            return
        path = self._selected_path()
        if isinstance(value, InvDraft):
            created = replace(value, name=f"INV {len(self._tree.invs) + 1}")
            self._tree = replace(self._tree, invs=(*self._tree.invs, created))
            self.review_changed.emit(self._tree)
            self._render_tree((len(self._tree.invs) - 1,))
        elif isinstance(value, NodeDraft) and path is not None and len(path) == 1:
            inv = self._object_at(path)
            created = replace(value, name=f"Node {len(inv.nodes) + 1}")
            self._replace_at(path, replace(inv, nodes=(*inv.nodes, created)))
            self._render_tree((*path, len(inv.nodes)))
        elif isinstance(value, RowDraft) and path is not None and len(path) == 2:
            node = self._object_at(path)
            created = replace(value, name=str(len(node.rows) + 1))
            self._replace_at(path, replace(node, rows=(*node.rows, created)))
            self._render_tree((*path, len(node.rows)))

    def _expand_to_level(self, level: int) -> None:
        for inv_index in range(self.structure_tree.topLevelItemCount()):
            inv_item = self.structure_tree.topLevelItem(inv_index)
            inv_item.setExpanded(level >= 2)
            for node_index in range(inv_item.childCount()):
                inv_item.child(node_index).setExpanded(level >= 3)

    def _show_tree_context_menu(self, position) -> None:
        item = self.structure_tree.itemAt(position)
        menu = QMenu(self.structure_tree)
        if item is None:
            menu.addAction(
                tr("ai_statistics.element_recognition.add_inv", "Add INV"),
                self._add_inv,
            )
        else:
            if not item.isSelected():
                self.structure_tree.clearSelection()
                self.structure_tree.setCurrentItem(item)
            path = self._selected_path()
            if path is None:
                return
            selected_paths = self._selected_sibling_paths()
            if len(selected_paths) >= 2:
                sort_menu = menu.addMenu(
                    tr("ordering.sort", "Sort")
                )
                sort_menu.addAction(
                    tr("ordering.ascending", "Ascending"),
                    lambda: self._sort_selected(False),
                )
                sort_menu.addAction(
                    tr("ordering.descending", "Descending"),
                    lambda: self._sort_selected(True),
                )
                menu.exec(self.structure_tree.viewport().mapToGlobal(position))
                return
            if len(path) == 1:
                menu.addAction(
                    tr("ai_statistics.element_recognition.add_node", "Add Node"),
                    self._add_node,
                )
            elif len(path) == 2:
                menu.addAction(
                    tr("ai_statistics.element_recognition.add_row", "Add Row"),
                    self._add_row,
                )
            menu.addAction(
                tr("ai_statistics.element_recognition.rename", "Rename"),
                self._rename_selected,
            )
            move_up = menu.addAction(
                tr("ai_statistics.element_recognition.move_up", "Move Up"),
                lambda: self._move_selected(-1),
            )
            move_down = menu.addAction(
                tr("ai_statistics.element_recognition.move_down", "Move Down"),
                lambda: self._move_selected(1),
            )
            move_up.setEnabled(path[-1] > 0)
            move_down.setEnabled(path[-1] < self._sibling_count(path) - 1)
            menu.addSeparator()
            menu.addAction(
                tr("ai_statistics.element_recognition.delete", "Delete"),
                self._delete_selected,
            )
        menu.exec(self.structure_tree.viewport().mapToGlobal(position))

    def _sibling_count(self, path: tuple[int, ...]) -> int:
        if len(path) == 1:
            return len(self._tree.invs)
        inv = self._tree.invs[path[0]]
        if len(path) == 2:
            return len(inv.nodes)
        return len(inv.nodes[path[1]].rows)

    def _rename_selected(self) -> None:
        item = self.structure_tree.currentItem()
        if item is not None:
            self.structure_tree.editItem(item, 0)

    def _add_inv(self) -> None:
        inv = InvDraft(name=f"INV {len(self._tree.invs) + 1}")
        self._tree = replace(self._tree, invs=(*self._tree.invs, inv))
        self.review_changed.emit(self._tree)
        self._render_tree((len(self._tree.invs) - 1,))

    def _add_node(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        inv_index = path[0]
        inv = self._tree.invs[inv_index]
        previous_name = inv.nodes[-1].name if inv.nodes else None
        node = NodeDraft(name=ProjectNode.sequence_name(previous_name))
        self._replace_at((inv_index,), replace(inv, nodes=(*inv.nodes, node)))
        self._render_tree((inv_index, len(inv.nodes)))

    def _add_row(self) -> None:
        path = self._selected_path()
        if path is None or len(path) < 2:
            return
        node_path = path[:2]
        node = self._object_at(node_path)
        previous_name = node.rows[-1].name if node.rows else None
        # A manually added Row always starts with one empty Racking Entry,
        # matching the project Row model, so the racking table stays editable.
        row = RowDraft(
            name=ProjectRow.sequence_name(previous_name),
            racking_entries=(RackingEntryDraft(),),
        )
        self._replace_at(node_path, replace(node, rows=(*node.rows, row)))
        self._render_tree((*node_path, len(node.rows)))

    def _delete_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        if QMessageBox.question(
            self,
            tr("ai_statistics.element_recognition.delete", "Delete"),
            tr(
                "ai_statistics.element_recognition.delete_confirm",
                "Delete the selected branch?",
            ),
        ) != QMessageBox.StandardButton.Yes:
            return
        invs = list(self._tree.invs)
        fallback: tuple[int, ...] | None
        if len(path) == 1:
            del invs[path[0]]
            fallback = (
                (min(path[0], len(invs) - 1),)
                if invs
                else None
            )
        elif len(path) == 2:
            inv = invs[path[0]]
            nodes = list(inv.nodes)
            del nodes[path[1]]
            invs[path[0]] = replace(inv, nodes=tuple(nodes))
            fallback = (path[0],)
        else:
            inv = invs[path[0]]
            nodes = list(inv.nodes)
            node = nodes[path[1]]
            rows = list(node.rows)
            del rows[path[2]]
            nodes[path[1]] = self._with_tracker_summary(
                replace(node, rows=tuple(rows))
            )
            invs[path[0]] = replace(inv, nodes=tuple(nodes))
            fallback = path[:2]
        self._tree = replace(self._tree, invs=tuple(invs))
        self.review_changed.emit(self._tree)
        self._render_tree(fallback)

    def _move_selected(self, delta: int) -> None:
        path = self._selected_path()
        if path is None:
            return
        index = path[-1]
        target = index + delta
        if target < 0:
            return
        invs = list(self._tree.invs)
        if len(path) == 1:
            values = invs
        elif len(path) == 2:
            inv = invs[path[0]]
            values = list(inv.nodes)
        else:
            inv = invs[path[0]]
            nodes = list(inv.nodes)
            node = nodes[path[1]]
            values = list(node.rows)
        if target >= len(values):
            return
        values[index], values[target] = values[target], values[index]
        if len(path) == 1:
            invs = values
        elif len(path) == 2:
            invs[path[0]] = replace(inv, nodes=tuple(values))
        else:
            nodes[path[1]] = replace(node, rows=tuple(values))
            invs[path[0]] = replace(inv, nodes=tuple(nodes))
        self._tree = replace(self._tree, invs=tuple(invs))
        self.review_changed.emit(self._tree)
        new_path = (*path[:-1], target)
        self._render_tree(new_path)

    def _select_entity_in_tree(self, entity_id: str) -> None:
        original_entity_id = entity_id
        if self._result is not None:
            page = next(
                (
                    page
                    for page in self._result.pages
                    if any(
                        detection.entity_id == entity_id
                        for detection in page.detections
                    )
                ),
                None,
            )
            detection = (
                next(
                    (
                        item
                        for item in page.detections
                        if item.entity_id == entity_id
                    ),
                    None,
                )
                if page is not None
                else None
            )
            if page is not None:
                page_index = self.page_combo.findData(page.page_number)
                if page_index >= 0 and page_index != self.page_combo.currentIndex():
                    self.page_combo.setCurrentIndex(page_index)
            if detection is not None and detection.parent_id:
                entity_id = detection.parent_id
        item = self._item_for_entity(entity_id)
        if item is not None:
            self.structure_tree.setCurrentItem(item)
            self.canvas.select_entity(original_entity_id)

    def _show_issue_context_menu(self, position) -> None:
        item = self.issues.itemAt(position)
        if item is None:
            return
        key = item.data(_ISSUE_KEY_ROLE)
        muted = key in self._muted_issue_keys
        menu = QMenu(self.issues)
        action = menu.addAction(
            tr(
                "ai_statistics.element_recognition.issue_restore",
                "Restore This Issue",
            )
            if muted
            else tr(
                "ai_statistics.element_recognition.issue_ignore",
                "Ignore This Issue",
            )
        )
        chosen = _exec_context_menu(
            menu,
            self.issues.viewport().mapToGlobal(position),
        )
        if chosen is action:
            self._set_issue_muted(key, muted=not muted)

    def _set_issue_muted(
        self,
        key: tuple[object, ...],
        *,
        muted: bool,
    ) -> None:
        if muted:
            self._muted_issue_keys.add(key)
        else:
            self._muted_issue_keys.discard(key)
        self._populate_issues()

    def _select_issue(self, item) -> None:
        entity_id = item.data(_ISSUE_ENTITY_ROLE)
        page_number = item.data(_ISSUE_PAGE_ROLE)
        if page_number is not None:
            page_index = self.page_combo.findData(page_number)
            if page_index >= 0:
                self.page_combo.setCurrentIndex(page_index)
        if entity_id:
            self._select_entity_in_tree(str(entity_id))
            self.canvas.select_entity(str(entity_id))
        # Tree/page synchronization may rebuild the issue list. Restore input
        # focus to the dialog so the first click remains visibly selected and
        # keyboard navigation continues here.
        self.issues.setFocus(Qt.FocusReason.OtherFocusReason)

    def retranslate(self) -> None:
        self.fit_button.setText(
            tr("ai_statistics.element_recognition.fit", "Fit")
        )
        self.settings_button.setText(
            tr("ai_statistics.element_recognition.settings", "Settings")
        )
        self.table_button.setText(
            tr(
                "ai_statistics.element_recognition.table_review",
                "Node / Strings",
            )
        )
        self.table_review_title.setText(
            tr(
                "ai_statistics.element_recognition.table_review_title",
                "Extracted Node and string counts",
            )
        )
        self.table_review_table.setHorizontalHeaderLabels(
            (
                tr("ai_statistics.element_recognition.table_node", "Node"),
                tr(
                    "ai_statistics.element_recognition.table_strings",
                    "Strings",
                ),
            )
        )
        self.table_add_button.setText(
            tr("ai_statistics.element_recognition.table_add", "Add")
        )
        self.table_delete_button.setText(
            tr("ai_statistics.element_recognition.table_delete", "Delete")
        )
        self.table_recalculate_button.setText(
            tr(
                "ai_statistics.element_recognition.table_recalculate",
                "Apply and recalculate",
            )
        )
        self.debug_export_button.setText(
            tr(
                "ai_statistics.element_recognition.export_debug_json",
                "Export Debug JSON",
            )
        )
        self.settings_dialog.setWindowTitle(
            tr(
                "ai_statistics.element_recognition.settings_title",
                "Agent3 Recognition Settings",
            )
        )
        self.issues_dialog.setWindowTitle(
            tr("ai_statistics.element_recognition.issues_title", "Recognition Issues")
        )
        self.issue_search.setPlaceholderText(
            tr(
                "ai_statistics.element_recognition.issue_search",
                "Search issues…",
            )
        )
        self.issue_search_button.setText(
            tr(
                "ai_statistics.element_recognition.issue_search_button",
                "Search",
            )
        )
        self.structure_tree.setHeaderLabel(
            tr("count_tab.tree_header", "Project Structure")
        )
        self.structure_tree.setToolTip(
            tr(
                "ai_statistics.element_recognition.tree_hint",
                "Right-click a structure item to add, rename, move, or delete it.",
            )
        )
        self.property_empty.setText(
            tr(
                "ai_statistics.element_recognition.select_structure_hint",
                "Select an INV, Node, or Row to edit its properties.",
            )
        )
        self.project_orientation_label.setText(
            tr(
                "ai_statistics.element_recognition.project_orientation",
                "Project orientation",
            )
        )
        self.project_snap_label.setText(
            tr(
                "ai_statistics.element_recognition.offset_step",
                "Offset step (strings)",
            )
        )
        self.project_apply.setText(
            tr(
                "ai_statistics.element_recognition.apply_project_defaults",
                "Apply project defaults",
            )
        )
        self.mapping_table.setHorizontalHeaderLabels(
            (
                "INV",
                tr(
                    "ai_statistics.element_recognition.orientation",
                    "Orientation",
                ),
                tr(
                    "ai_statistics.element_recognition.offset_step_short",
                    "Offset step",
                ),
            )
        )
        self.property_labels["name"].setText(
            tr("ai_statistics.element_recognition.name", "Name")
        )
        self.property_labels["orientation"].setText(
            tr(
                "ai_statistics.element_recognition.panel_orientation",
                "Panel orientation",
            )
        )
        self.property_labels["snap"].setText(
            tr(
                "ai_statistics.element_recognition.offset_step",
                "Offset step (strings)",
            )
        )
        self.property_labels["strings"].setText(
            tr(
                "ai_statistics.element_recognition.node_string_count",
                "Node string count",
            )
        )
        self.property_labels["trackers"].setText(
            tr(
                "ai_statistics.element_recognition.tracker_counts",
                "Tracker counts",
            )
        )
        self.property_labels["box"].setText(
            tr("ai_statistics.element_recognition.box_position", "Box position")
        )
        self.tracker_counts.setPlaceholderText(
            tr(
                "ai_statistics.element_recognition.tracker_counts_hint",
                "code=count, code=count",
            )
        )
        self.recalculate_button.setText(
            tr(
                "ai_statistics.element_recognition.apply_recalculate",
                "Apply settings and recalculate",
            )
        )
        self.racking_table.setHorizontalHeaderLabels(
            (
                tr(
                    "ai_statistics.element_recognition.racking_entry",
                    "Racking Entry",
                ),
                tr("ai_statistics.element_recognition.offset", "Offset"),
            )
        )
        self._populate_orientation_combo(self.project_orientation)
        self._populate_orientation_combo(self.orientation_combo)
        for row in range(self.mapping_table.rowCount()):
            combo = self.mapping_table.cellWidget(row, 1)
            if isinstance(combo, QComboBox):
                self._populate_orientation_combo(combo)
        self._refresh_page_options()
        self._populate_issues()
        if self.structure_tree.currentItem() is not None:
            self._show_properties(self.structure_tree.currentItem())
        self._update_issue_button_text()
        self.retranslate_confirm()

    def _update_issue_button_text(self) -> None:
        self.issues_button.setText(
            tr(
                "ai_statistics.element_recognition.issues_count",
                "Issues ({count})",
            ).format(count=self._issue_count)
        )


class QTreeWidgetItemIteratorCompat:
    """Small recursive iterator avoiding binding-specific iterator operators."""

    def __init__(self, tree: QTreeWidget) -> None:
        self._items: list[QTreeWidgetItem] = []
        for index in range(tree.topLevelItemCount()):
            self._collect(tree.topLevelItem(index))

    def _collect(self, item: QTreeWidgetItem) -> None:
        self._items.append(item)
        for index in range(item.childCount()):
            self._collect(item.child(index))

    def __iter__(self):
        return iter(self._items)
