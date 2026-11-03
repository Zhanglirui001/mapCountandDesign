"""History tab – displays the operation tree with folding and navigation."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr
from ..viewmodels.history_tab import HistoryTabViewModel
from ..viewmodels.states import HistoryTreeNodeState, HistoryTabState


class HistoryTab(QWidget):
    """A tab that shows the operation history as a tree.

    - Linear chains of middle nodes are collapsed into fold rows.
    - The path from root to current node is highlighted with bold text.
    - Rich summaries (from diffs) are shown in the operation column.
    - Clicking any node navigates to that state (creating a fork on next commit).
    """

    node_selected = Signal(str)  # emits node_id

    def __init__(self, view_model: HistoryTabViewModel) -> None:
        super().__init__()
        self._view_model = view_model
        self._tree: QTreeWidget | None = None
        self._search_box: QLineEdit | None = None
        self._search_count_label: QLabel | None = None
        self._prev_btn: QPushButton | None = None
        self._next_btn: QPushButton | None = None
        self._current_path_set: set[str] = set()
        self._pending_search_target: str = ""
        self._setup_ui()
        self._view_model.state_changed.connect(self._render_state)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # -- search bar --------------------------------------------------
        search_layout = QHBoxLayout()
        search_layout.setSpacing(4)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(
            tr("history_tab.search.placeholder", "Search operations...")
        )
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search_text_changed)
        self._search_box.returnPressed.connect(self._on_search_next)
        search_layout.addWidget(self._search_box)

        self._search_count_label = QLabel()
        self._search_count_label.setVisible(False)
        search_layout.addWidget(self._search_count_label)

        self._prev_btn = QPushButton(
            tr("history_tab.search.prev", "\u25b2")
        )
        self._prev_btn.setToolTip(
            tr("history_tab.search.prev_tip", "Previous match")
        )
        self._prev_btn.setFixedWidth(28)
        self._prev_btn.clicked.connect(self._on_search_prev)
        self._prev_btn.setVisible(False)
        search_layout.addWidget(self._prev_btn)

        self._next_btn = QPushButton(
            tr("history_tab.search.next", "\u25bc")
        )
        self._next_btn.setToolTip(
            tr("history_tab.search.next_tip", "Next match")
        )
        self._next_btn.setFixedWidth(28)
        self._next_btn.clicked.connect(self._on_search_next)
        self._next_btn.setVisible(False)
        search_layout.addWidget(self._next_btn)

        layout.addLayout(search_layout)

        # -- tree --------------------------------------------------------
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            tr("history_tab.header.operation", "Operation"),
            tr("history_tab.header.time", "Time"),
        ])
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.setExpandsOnDoubleClick(False)
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemCollapsed.connect(self._on_item_collapsed)

        layout.addWidget(self._tree)

        # Keyboard shortcut: Ctrl+F focuses the search box
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search)
        # Escape clears search
        QShortcut(QKeySequence("Escape"), self, self._on_search_escape)

    # -- render -----------------------------------------------------------

    def _render_state(self, state: HistoryTabState) -> None:
        if self._tree is None:
            return
        self._current_path_set = set(state.path_to_current)
        self._pending_search_target = state.search_target_node_id
        self._tree.clear()
        for node in state.tree:
            item = self._build_item(node)
            self._tree.addTopLevelItem(item)
        # Expand every item that has children through a manual recursive
        # walk.  We deliberately avoid QTreeWidget.expandAll() because it
        # interacts poorly with DontShowIndicator and can silently skip
        # deeply nested children after a clear()+rebuild cycle.
        for i in range(self._tree.topLevelItemCount()):
            self._expand_all_items(self._tree.topLevelItem(i))
        # If there is a pending search target, locate and select it
        if self._pending_search_target:
            self._select_node_in_tree(self._pending_search_target)
        # Update the search count label in case results changed
        self._update_search_label()

    @staticmethod
    def _expand_all_items(item: QTreeWidgetItem) -> None:
        """Recursively expand *item* and all its descendants."""
        if item.childCount() > 0:
            item.setExpanded(True)
            for i in range(item.childCount()):
                HistoryTab._expand_all_items(item.child(i))

    def _build_item(self, node: HistoryTreeNodeState) -> QTreeWidgetItem:
        time_str = datetime.fromtimestamp(node.timestamp).strftime("%H:%M:%S")

        # Build label text
        if node.is_fold_row:
            label = f"📋 {node.description}"
            tooltip = ""
        else:
            # Build the display label: use translated description + summary
            desc = tr(node.description, node.description)
            if node.summary:
                label = f"{desc}: {node.summary}"
            else:
                label = desc
            if node.is_current:
                label = f"\u25b6 {label}"
            tooltip = node.summary if node.summary else desc

        item = QTreeWidgetItem([label, time_str])
        item.setData(0, Qt.ItemDataRole.UserRole, node.node_id)
        item.setToolTip(0, tooltip)

        if node.is_locked:
            item.setData(0, Qt.ItemDataRole.UserRole + 4, True)
            item.setToolTip(
                0,
                tr(
                    "history_tab.locked_tip",
                    "This history predates integrity verification and is read-only.",
                ),
            )
            item.setForeground(0, QColor(140, 140, 140))
            item.setForeground(1, QColor(140, 140, 140))

        # Fold row styling (style before adding children so child items
        # inherit the default policy).
        if node.is_fold_row:
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "fold_row")
            item.setData(0, Qt.ItemDataRole.UserRole + 2, node.fold_count)
            item.setData(0, Qt.ItemDataRole.UserRole + 3, node.fold_start_index)
            font = item.font(0)
            font.setItalic(True)
            item.setFont(0, font)
            item.setFont(1, font)
            item.setForeground(0, QColor(120, 120, 120))
            item.setForeground(1, QColor(120, 120, 120))

        # Current node styling
        if node.is_current:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            item.setFont(1, font)
            item.setBackground(0, QColor(200, 230, 255))
            item.setBackground(1, QColor(200, 230, 255))

        # Path-to-current highlighting (bold, but not blue)
        if not node.is_current and not node.is_fold_row and node.node_id in self._current_path_set:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            item.setFont(1, font)

        # Recursively build and attach children
        for child in node.children:
            child_item = self._build_item(child)
            item.addChild(child_item)

        # Expand fold rows after children have been added so that Qt
        # correctly calculates the child layout.
        if node.is_fold_row:
            item.setExpanded(True)

        return item

    # -- events -----------------------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        node_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not node_id:
            return
        if item.data(0, Qt.ItemDataRole.UserRole + 4):
            return

        # Check if it's a fold row
        is_fold = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if is_fold == "fold_row":
            self._show_fold_menu(item)
            return

        # Regular node: navigate (checkout will cause a refresh, which
        # automatically resets any expanded fold via set_version_manager)
        self.node_selected.emit(node_id)

    def _on_context_menu(self, pos) -> None:
        """Right-click context menu on the tree."""
        if self._tree is None:
            return
        item = self._tree.itemAt(pos)
        if item is None:
            return
        is_fold = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if is_fold == "fold_row":
            self._show_fold_menu(item)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        """When user manually collapses a tree node, collapse any open fold too."""
        is_fold = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if is_fold != "fold_row":
            self._view_model.collapse_expanded_fold()

    # -- search slots ----------------------------------------------------

    def _focus_search(self) -> None:
        """Focus the search box and select all text."""
        if self._search_box is not None:
            self._search_box.setFocus()
            self._search_box.selectAll()

    def _on_search_text_changed(self, text: str) -> None:
        """Search on every keystroke; jump to first result."""
        count = self._view_model.search(text)
        self._update_search_controls(count > 0)
        if count > 0:
            self._view_model.next_result()
        else:
            self._update_search_label()

    def _on_search_next(self) -> None:
        """Jump to the next search result."""
        if self._view_model.search_result_count == 0:
            return
        self._view_model.next_result()

    def _on_search_prev(self) -> None:
        """Jump to the previous search result."""
        if self._view_model.search_result_count == 0:
            return
        self._view_model.prev_result()

    def _on_search_escape(self) -> None:
        """Clear the search when Escape is pressed."""
        if self._search_box is not None:
            self._search_box.clear()

    def _update_search_controls(self, has_results: bool) -> None:
        """Show/hide search navigation controls based on whether there are results."""
        if self._prev_btn is not None:
            self._prev_btn.setVisible(has_results)
        if self._next_btn is not None:
            self._next_btn.setVisible(has_results)
        self._update_search_label()

    def _update_search_label(self) -> None:
        """Refresh the 'N/M' label and the search-box styling."""
        if self._search_count_label is None or self._search_box is None:
            return
        total = self._view_model.search_result_count
        current = self._view_model.search_result_index
        if total > 0:
            self._search_box.setStyleSheet("")
            self._search_count_label.setText(
                tr("history_tab.search.count", "{current}/{total}").format(
                    current=current, total=total
                )
            )
            self._search_count_label.setVisible(True)
        else:
            self._search_count_label.setVisible(False)
            # If there is text but no results, style the search box to indicate "no match"
            if self._search_box.text().strip():
                self._search_box.setStyleSheet(
                    "QLineEdit { background-color: #fff0f0; }"
                )
            else:
                self._search_box.setStyleSheet("")

    def _select_node_in_tree(self, node_id: str) -> None:
        """Find *node_id* in the tree, expand ancestors, select and scroll to it."""
        if self._tree is None:
            return
        item = self._find_item_by_node_id(node_id)
        if item is None:
            self._pending_search_target = ""
            return
        self._pending_search_target = ""
        # Expand all ancestors so the item is visible
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item, QTreeWidget.ScrollHint.PositionAtCenter)

    def _find_item_by_node_id(self, node_id: str) -> QTreeWidgetItem | None:
        """Walk the tree to find the item whose UserRole data matches *node_id*."""
        if self._tree is None:
            return None

        def _walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) == node_id:
                return item
            for i in range(item.childCount()):
                found = _walk(item.child(i))
                if found is not None:
                    return found
            return None

        for i in range(self._tree.topLevelItemCount()):
            found = _walk(self._tree.topLevelItem(i))
            if found is not None:
                return found
        return None

    # -- fold menu ---------------------------------------------------------

    def _show_fold_menu(self, item: QTreeWidgetItem) -> None:
        """Show the context menu for a fold row."""
        fold_node_id = item.data(0, Qt.ItemDataRole.UserRole)
        fold_count = item.data(0, Qt.ItemDataRole.UserRole + 2)
        fold_offset = item.data(0, Qt.ItemDataRole.UserRole + 3) or 0

        if not fold_node_id or not fold_count:
            return

        menu = QMenu(self)
        menu.addAction(
            tr("history_tab.fold.show_first", "Show first 10 (of {count})").format(count=fold_count),
            lambda *args: self._view_model.expand_fold_first(fold_node_id, fold_count, fold_offset),
        )
        menu.addAction(
            tr("history_tab.fold.show_last", "Show last 10 (of {count})").format(count=fold_count),
            lambda *args: self._view_model.expand_fold_last(fold_node_id, fold_count, fold_offset),
        )
        menu.addAction(tr("history_tab.fold.show_index", "Show i-th...")).triggered.connect(
            lambda *args: self._prompt_fold_index(fold_node_id, fold_count, fold_offset)
        )

        menu.exec(self._tree.viewport().mapToGlobal(
            self._tree.visualItemRect(item).bottomLeft()
        ))

    def _prompt_fold_index(self, fold_node_id: str, fold_count: int, fold_offset: int = 0) -> None:
        """Ask user for the starting index to expand."""
        i, ok = QInputDialog.getInt(
            self,
            tr("history_tab.fold.dialog_title", "Expand Folded Nodes"),
            tr("history_tab.fold.dialog_prompt", "Start from which index? (1-{count})").format(count=fold_count),
            value=1,
            minValue=1,
            maxValue=fold_count,
        )
        if ok:
            self._view_model.expand_fold_at(fold_node_id, i - 1, fold_count, fold_offset)
