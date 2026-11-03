"""Page-classification stage view."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .....application.ai_statistics import (
    DrawingPageType,
    PageClassification,
    PageClassificationResult,
    render_pdf_page_with_thumbnail,
)
from .....i18n import tr
from ...widgets import disable_combo_scroll
from ..widgets import WorkflowStagePage, panel_header


_PAGE_TYPES = tuple(item.value for item in DrawingPageType)
_PAGE_TYPE_TRANSLATIONS = {
    DrawingPageType.ARRAY_LAYOUT.value: (
        "ai_statistics.page_type.array_layout",
        "Array Layout",
    ),
    DrawingPageType.OVERALL_LAYOUT.value: (
        "ai_statistics.page_type.overall_layout",
        "Overall Layout",
    ),
    DrawingPageType.OTHER.value: (
        "ai_statistics.page_type.other",
        "Other",
    ),
}


def _page_type_label(page_type: str) -> str:
    key, fallback = _PAGE_TYPE_TRANSLATIONS[page_type]
    return tr(key, fallback)


class ClickableThumbnailLabel(QLabel):
    """Thumbnail label that opens a larger page preview when clicked."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PagePreviewDialog(QDialog):
    """Maximized, scrollable PDF page preview with zoom controls."""

    def __init__(
        self,
        page_number: int,
        pixmap: QPixmap,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            tr("ai_statistics.page_preview", "Page {number} Preview").format(
                number=page_number
            )
        )
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1100, 760)
        self._source = pixmap
        self._scale = 1.0
        self._fit_to_window = True

        root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        self._zoom_out = QPushButton("−")
        self._zoom_in = QPushButton("+")
        self._fit = QPushButton(
            tr("ai_statistics.preview_fit", "Fit to Window")
        )
        self._actual = QPushButton(
            tr("ai_statistics.preview_actual", "Actual Size")
        )
        self._close = QPushButton(tr("common.close", "Close"))
        self._zoom_out.clicked.connect(lambda: self._set_scale(self._scale / 1.2))
        self._zoom_in.clicked.connect(lambda: self._set_scale(self._scale * 1.2))
        self._fit.clicked.connect(self._fit_image)
        self._actual.clicked.connect(lambda: self._set_scale(1.0))
        self._close.clicked.connect(self.accept)
        for button in (
            self._zoom_out,
            self._zoom_in,
            self._fit,
            self._actual,
            self._close,
        ):
            toolbar.addWidget(button)
        root.addLayout(toolbar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setStyleSheet("background: #20242a;")
        self._scroll.setWidget(self._image)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._scroll, 1)
        self._fit_image()

    @property
    def scale_factor(self) -> float:
        return self._scale

    def _set_scale(self, scale: float) -> None:
        self._fit_to_window = False
        self._scale = max(0.1, min(5.0, scale))
        size = self._source.size() * self._scale
        self._image.setPixmap(
            self._source.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._image.resize(self._image.pixmap().size())

    def _fit_image(self) -> None:
        self._fit_to_window = True
        viewport = self._scroll.viewport().size()
        if viewport.width() <= 1 or viewport.height() <= 1:
            viewport = self.size()
        scaled = self._source.scaled(
            max(1, viewport.width() - 24),
            max(1, viewport.height() - 24),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scale = scaled.width() / max(1, self._source.width())
        self._image.setPixmap(scaled)
        self._image.resize(scaled.size())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_to_window:
            self._fit_image()


class SourceCategoryLabel(QLabel):
    """Compact source name with the full category available on hover."""

    def __init__(self, category: str) -> None:
        super().__init__()
        self._category_name = category.split("+", 1)[-1]
        self.setToolTip(category)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet("font-size: 9px; color: #94a3b8;")
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(70)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(
            self._category_name, Qt.TextElideMode.ElideRight, max(0, self.width())
        ))


class PageClassificationCard(QFrame):
    """One rendered PDF page and its editable Agent1 category."""

    category_changed = Signal(int, str)
    preview_requested = Signal(int)

    def __init__(
        self,
        page: PageClassification,
        thumbnail: QPixmap,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.page_number = page.page_number
        self._confidence = page.source.confidence
        self._source_category = str(
            page.source.raw.get("matched_category") or page.purpose or ""
        )
        self.setObjectName("classificationCard")
        self.setFixedWidth(180)
        self.setStyleSheet(
            "QFrame#classificationCard {"
            " background: #ffffff; border: 1px solid #dfe4ea;"
            " border-radius: 4px; }"
            "QFrame#classificationCard:hover { border-color: #4676f2; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        preview = ClickableThumbnailLabel()
        preview.setObjectName("pageThumbnail")
        preview.setFixedSize(162, 112)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setCursor(Qt.CursorShape.PointingHandCursor)
        preview.setToolTip(
            tr("ai_statistics.open_page_preview", "Click to enlarge")
        )
        preview.clicked.connect(
            lambda: self.preview_requested.emit(self.page_number)
        )
        preview.setStyleSheet(
            "background: #f8fafc; border: 1px solid #e2e8f0;"
        )
        if thumbnail.isNull():
            preview.setText(tr("ai_statistics.thumbnail_failed", "Preview unavailable"))
        else:
            preview.setPixmap(
                thumbnail.scaled(
                    preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        root.addWidget(preview)

        self.page_label = QLabel()
        self.page_label.setStyleSheet("font-weight: 600; color: #243447;")
        root.addWidget(self.page_label)

        self.category_combo = disable_combo_scroll(QComboBox())
        for page_type in _PAGE_TYPES:
            self.category_combo.addItem(_page_type_label(page_type), page_type)
        self.category_combo.blockSignals(True)
        self.category_combo.setCurrentIndex(
            self.category_combo.findData(page.page_type)
        )
        self.category_combo.blockSignals(False)
        self.category_combo.currentIndexChanged.connect(
            lambda _index: self.category_changed.emit(
                self.page_number,
                str(self.category_combo.currentData()),
            )
        )
        root.addWidget(self.category_combo)

        self.meta = QLabel()
        self.meta.setObjectName("muted")
        self.meta.setStyleSheet("font-size: 10px; color: #94a3b8;")
        metadata_row = QHBoxLayout()
        metadata_row.setSpacing(6)
        metadata_row.addWidget(self.meta)
        self.source_category = SourceCategoryLabel(self._source_category)
        self.source_category.setObjectName("sourceCategory")
        metadata_row.addWidget(self.source_category, 1)
        root.addLayout(metadata_row)
        self.retranslate()

    def retranslate(self) -> None:
        self.page_label.setText(
            tr("ai_statistics.page_number", "Page {number}").format(
                number=self.page_number
            )
        )
        current_page_type = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        for index in range(self.category_combo.count()):
            page_type = str(self.category_combo.itemData(index))
            self.category_combo.setItemText(index, _page_type_label(page_type))
        self.category_combo.setCurrentIndex(
            self.category_combo.findData(current_page_type)
        )
        self.category_combo.blockSignals(False)
        confidence_text = (
            f"{self._confidence:.0%}" if self._confidence is not None else "—"
        )
        self.meta.setText(
            tr(
                "ai_statistics.classification_confidence",
                "Confidence: {confidence}",
            ).format(confidence=confidence_text)
        )


class ResponsiveCardGrid(QWidget):
    """Reflow cards to use all available horizontal space."""

    CARD_WIDTH = 180
    SPACING = 10

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(self.SPACING)
        self._layout.setVerticalSpacing(self.SPACING)
        self._layout.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._cards: list[PageClassificationCard] = []
        self._column_count = 1

    @property
    def column_count(self) -> int:
        return self._column_count

    def add_card(self, card: PageClassificationCard) -> None:
        self._cards.append(card)
        self._reflow()

    def _reflow(self) -> None:
        available = max(self.CARD_WIDTH, self.width())
        columns = max(
            1,
            (available + self.SPACING)
            // (self.CARD_WIDTH + self.SPACING),
        )
        if columns == self._column_count and self._layout.count():
            return
        self._column_count = columns
        while self._layout.count():
            self._layout.takeAt(0)
        for index, card in enumerate(self._cards):
            self._layout.addWidget(
                card,
                index // columns,
                index % columns,
            )
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()


class CategoryCountsPanel(QFrame):
    """Right-hand summary that follows the reviewed classifications."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setFixedWidth(220)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(9)

        self.title_label = QLabel()
        self.title_label.setObjectName("panelTitle")
        root.addWidget(self.title_label)

        self._value_labels: dict[str, QLabel] = {}
        self._name_labels: dict[str, QLabel] = {}
        for key in ("Total Pages", *_PAGE_TYPES):
            row = QFrame()
            row.setStyleSheet(
                "background: #f8fafc; border: 1px solid #edf1f5;"
                "border-radius: 4px;"
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            label = QLabel()
            label.setStyleSheet("color: #64748b; border: none;")
            value = QLabel("0")
            value.setStyleSheet(
                "font-weight: 700; color: #243447; border: none;"
            )
            row_layout.addWidget(label)
            row_layout.addStretch()
            row_layout.addWidget(value)
            root.addWidget(row)
            self._value_labels[key] = value
            self._name_labels[key] = label
        root.addStretch()
        self.retranslate()

    def set_counts(self, pages: tuple[PageClassification, ...]) -> None:
        counts = Counter(page.page_type for page in pages)
        self._value_labels["Total Pages"].setText(str(len(pages)))
        for page_type in _PAGE_TYPES:
            self._value_labels[page_type].setText(str(counts[page_type]))

    def retranslate(self) -> None:
        self.title_label.setText(
            tr("ai_statistics.category_counts", "Category Counts")
        )
        self._name_labels["Total Pages"].setText(
            tr("ai_statistics.total_pages", "Total Pages")
        )
        for page_type in _PAGE_TYPES:
            if page_type == DrawingPageType.OTHER.value:
                label = tr("ai_statistics.other_pages", "Other Pages")
            else:
                label = _page_type_label(page_type)
            self._name_labels[page_type].setText(label)


class PageClassificationPage(WorkflowStagePage):
    """Grouped Agent1 results with editable categories and PDF thumbnails."""

    CANVAS_EMPTY = 0
    CANVAS_PREVIEW = 1
    classification_changed = Signal(int, str)

    def __init__(
        self,
        pdf_document: QPdfDocument,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "PageClassificationPage",
            "ai_statistics.page_classification.confirm",
            "Confirm Classification",
            parent,
        )
        self._pdf_document = pdf_document
        self._pdf_name: str | None = None
        self._pdf_path = None
        self._result: PageClassificationResult | None = None
        self._preview_dialog: PagePreviewDialog | None = None
        self._group_layout = None
        self._setup_ui()
        self.retranslate()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        header, self.canvas_title, self.canvas_meta = panel_header()
        panel_layout.addWidget(header)

        self.canvas_stack = QStackedWidget()
        self.canvas_stack.addWidget(self._build_empty_state())
        self.canvas_stack.addWidget(self._build_result_view())
        panel_layout.addWidget(self.canvas_stack, 1)
        root.addWidget(panel, 1)
        self.append_confirm_footer(root)

    def _build_empty_state(self) -> QWidget:
        placeholder = QWidget()
        layout = QVBoxLayout(placeholder)
        self.canvas_empty = QLabel()
        self.canvas_empty.setObjectName("emptyHint")
        self.canvas_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.canvas_empty)
        return placeholder

    def _build_result_view(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._groups_widget = QWidget()
        self._group_layout = QVBoxLayout(self._groups_widget)
        self._group_layout.setContentsMargins(0, 0, 0, 0)
        self._group_layout.setSpacing(10)
        self._group_layout.addStretch()
        self.results_scroll.setWidget(self._groups_widget)
        layout.addWidget(self.results_scroll, 1)

        self.counts_panel = CategoryCountsPanel()
        layout.addWidget(self.counts_panel)
        return container

    def _clear_groups(self) -> None:
        while self._group_layout.count():
            item = self._group_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_thumbnail(self, page_number: int) -> QPixmap:
        """Load the thumbnail Agent1 already produced for this page.

        Rasterizing a drawing page costs over a second, so the review grid
        never renders: a missing thumbnail shows the placeholder instead.
        """

        if self._result is None:
            return QPixmap()
        data = self._result.thumbnails.get(page_number)
        if not data:
            return QPixmap()
        pixmap = QPixmap()
        pixmap.loadFromData(data, "PNG")
        return pixmap

    def show_result(
        self,
        result: PageClassificationResult,
        pdf_path=None,
    ) -> None:
        if pdf_path is not None:
            self._pdf_path = pdf_path
        if self._result is result:
            return
        self._result = result
        self._clear_groups()
        grouped = {
            page_type: tuple(
                page for page in result.pages if page.page_type == page_type
            )
            for page_type in _PAGE_TYPES
        }
        for page_type in _PAGE_TYPES:
            pages = grouped[page_type]
            if not pages:
                continue
            group = QFrame()
            group.setObjectName("classificationGroup")
            group.setStyleSheet(
                "QFrame#classificationGroup {"
                " background: #fbfcfe; border: 1px solid #e2e8f0;"
                " border-radius: 4px; }"
            )
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(10, 8, 10, 10)
            title_row = QHBoxLayout()
            title = QLabel(_page_type_label(page_type))
            title.setObjectName("classificationGroupTitle")
            title.setProperty("pageType", page_type)
            title.setStyleSheet("font-weight: 600; color: #334155;")
            count = QLabel(
                tr("ai_statistics.pages_count", "{count} pages").format(
                    count=len(pages)
                )
            )
            count.setObjectName("muted")
            count.setProperty("pageCount", len(pages))
            title_row.addWidget(title)
            title_row.addStretch()
            title_row.addWidget(count)
            group_layout.addLayout(title_row)

            cards = ResponsiveCardGrid()
            for page in pages:
                card = PageClassificationCard(
                    page,
                    self._render_thumbnail(page.page_number),
                )
                card.category_changed.connect(self.classification_changed)
                card.preview_requested.connect(self._open_page_preview)
                cards.add_card(card)
            group_layout.addWidget(cards)
            self._group_layout.addWidget(group)
        self._group_layout.addStretch()
        self.counts_panel.set_counts(result.pages)
        self.canvas_stack.setCurrentIndex(self.CANVAS_PREVIEW)

    def _open_page_preview(self, page_number: int) -> None:
        pixmap = QPixmap()
        if self._result is not None:
            cached = self._result.page_images.get(page_number)
            if cached:
                pixmap.loadFromData(cached, "PNG")
        if pixmap.isNull() and self._pdf_path is not None:
            try:
                full, _thumbnail = render_pdf_page_with_thumbnail(
                    self._pdf_path,
                    page_number - 1,
                )
                pixmap.loadFromData(full, "PNG")
            except Exception:
                pixmap = QPixmap()
        if pixmap.isNull():
            pixmap = self._render_thumbnail(page_number)
        if pixmap.isNull():
            return
        dialog = PagePreviewDialog(page_number, pixmap, self)
        self._preview_dialog = dialog
        dialog.setWindowState(
            dialog.windowState() | Qt.WindowState.WindowMaximized
        )
        dialog.exec()
        self._preview_dialog = None

    def show_empty(self) -> None:
        self._pdf_name = None
        self._pdf_path = None
        self._result = None
        self.canvas_meta.setText(tr("ai_statistics.no_pdf", "No PDF loaded"))
        self.canvas_stack.setCurrentIndex(self.CANVAS_EMPTY)
        self.counts_panel.set_counts(())

    def set_pdf_loaded(self, filename: str) -> None:
        self._pdf_name = filename
        self._result = None
        self.canvas_meta.setText(filename)
        self.canvas_stack.setCurrentIndex(self.CANVAS_EMPTY)

    def show_pdf(self, filename: str) -> None:
        self._pdf_name = filename
        self.canvas_meta.setText(filename)
        if self._result is not None:
            self.canvas_stack.setCurrentIndex(self.CANVAS_PREVIEW)

    def retranslate(self) -> None:
        self.canvas_title.setText(
            tr("ai_statistics.classification_preview", "Classification Preview")
        )
        self.canvas_empty.setText(
            tr("ai_statistics.empty_canvas", "Upload PDF to start")
        )
        if self._pdf_name is None:
            self.canvas_meta.setText(
                tr("ai_statistics.no_pdf", "No PDF loaded")
            )
        self.counts_panel.retranslate()
        for card in self.findChildren(PageClassificationCard):
            card.retranslate()
        for title in self.findChildren(QLabel, "classificationGroupTitle"):
            title.setText(_page_type_label(str(title.property("pageType"))))
        for count in self.findChildren(QLabel, "muted"):
            page_count = count.property("pageCount")
            if page_count is not None:
                count.setText(
                    tr("ai_statistics.pages_count", "{count} pages").format(
                        count=page_count
                    )
                )
        self.retranslate_confirm()
