"""Merge wizard window – step-by-step project merge."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...application.project.merge import (
    MergeConflict,
    MergeService,
    MergeStep,
    STEP_COUNT,
    STEP_INVS,
    STEP_LENGTH_IDENTIFIERS,
    STEP_PRODUCT_PRESETS,
    STEP_RACKINGS_LIST,
)
from ...i18n import t


class MergeWindow(QDialog):
    """Step-by-step merge wizard.

    After construction call :meth:`exec_merge` to start the guided merge.
    """

    merge_finished = Signal()  # emitted when the user completes or cancels

    def __init__(
        self,
        merge_service: MergeService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._merge = merge_service
        self._current_step: MergeStep = STEP_LENGTH_IDENTIFIERS
        self._conflicts: list[MergeConflict] = []
        self._non_conflict_items: list = []  # imported items without name conflict
        self._selected_index: int = 0
        self._setup_ui()
        self._load_step(self._current_step)

    # -- UI setup ---------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle(t("merge_window.title"))
        self.resize(950, 650)
        self.setMinimumSize(750, 480)

        root = QVBoxLayout(self)

        # -- Step indicator --
        self._step_label = QLabel()
        self._step_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding: 4px 0;"
        )
        root.addWidget(self._step_label)

        # -- Info line --
        self._info_label = QLabel()
        self._info_label.setWordWrap(True)
        root.addWidget(self._info_label)

        # -- Splitter (left panel | resolution panel) --
        splitter = QSplitter(Qt.Horizontal)

        # Left: conflict list + non-conflict list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Conflict list
        left_layout.addWidget(QLabel(t("merge_window.conflict_list_label")))
        self._conflict_list = QListWidget()
        self._conflict_list.currentRowChanged.connect(self._on_conflict_selected)
        left_layout.addWidget(self._conflict_list, 2)

        # Non-conflict list
        left_layout.addWidget(QLabel(t("merge_window.non_conflict_list_label")))
        self._non_conflict_list = QListWidget()
        self._non_conflict_list.setToolTip(t("merge_window.non_conflict_list_tooltip"))
        left_layout.addWidget(self._non_conflict_list, 1)

        # Select all / deselect all row
        sel_row = QHBoxLayout()
        self._select_all_btn = QPushButton(t("merge_window.select_all"))
        self._select_all_btn.clicked.connect(self._select_all_non_conflict)
        sel_row.addWidget(self._select_all_btn)
        self._deselect_all_btn = QPushButton(t("merge_window.deselect_all"))
        self._deselect_all_btn.clicked.connect(self._deselect_all_non_conflict)
        sel_row.addWidget(self._deselect_all_btn)
        left_layout.addLayout(sel_row)

        # Auto-merge button
        self._auto_merge_btn = QPushButton(t("merge_window.auto_merge_selected"))
        self._auto_merge_btn.clicked.connect(self._apply_auto_merge_selected)
        left_layout.addWidget(self._auto_merge_btn)

        splitter.addWidget(left_widget)

        # Right: resolution panel
        right_widget = QWidget()
        self._right_layout = QVBoxLayout(right_widget)
        self._right_layout.setContentsMargins(8, 0, 0, 0)

        # -- Current item info --
        self._current_item_group = QGroupBox(t("merge_window.current_item_group"))
        self._current_item_label = QLabel()
        self._current_item_label.setWordWrap(True)
        self._current_item_group.setLayout(QVBoxLayout())
        self._current_item_group.layout().addWidget(self._current_item_label)
        self._right_layout.addWidget(self._current_item_group)

        self._imported_item_group = QGroupBox(t("merge_window.imported_item_group"))
        self._imported_item_label = QLabel()
        self._imported_item_label.setWordWrap(True)
        self._imported_item_group.setLayout(QVBoxLayout())
        self._imported_item_group.layout().addWidget(self._imported_item_label)
        self._right_layout.addWidget(self._imported_item_group)

        # -- Resolution options --
        self._resolution_group = QGroupBox(t("merge_window.resolution_group"))
        res_layout = QVBoxLayout()

        self._radio_keep_current = QRadioButton(t("merge_window.keep_current"))
        self._radio_keep_imported = QRadioButton(t("merge_window.keep_imported"))
        self._radio_rename_current = QRadioButton(t("merge_window.rename_current"))
        self._radio_rename_imported = QRadioButton(t("merge_window.rename_imported"))
        self._radio_rename_both = QRadioButton(t("merge_window.rename_both"))

        res_layout.addWidget(self._radio_keep_current)
        res_layout.addWidget(self._radio_keep_imported)
        res_layout.addWidget(self._radio_rename_current)
        res_layout.addWidget(self._radio_rename_imported)
        res_layout.addWidget(self._radio_rename_both)

        # Rename fields
        rename_grid = QHBoxLayout()
        rename_grid.addWidget(QLabel(t("merge_window.rename_current_to")))
        self._rename_current_edit = QLineEdit()
        self._rename_current_edit.setPlaceholderText(
            t("merge_window.rename_current_placeholder")
        )
        rename_grid.addWidget(self._rename_current_edit)
        res_layout.addLayout(rename_grid)

        rename_imp_grid = QHBoxLayout()
        rename_imp_grid.addWidget(QLabel(t("merge_window.rename_imported_to")))
        self._rename_imported_edit = QLineEdit()
        self._rename_imported_edit.setPlaceholderText(
            t("merge_window.rename_imported_placeholder")
        )
        rename_imp_grid.addWidget(self._rename_imported_edit)
        res_layout.addLayout(rename_imp_grid)

        self._apply_resolution_btn = QPushButton(t("merge_window.apply_resolution"))
        self._apply_resolution_btn.clicked.connect(self._apply_current_resolution)
        res_layout.addWidget(self._apply_resolution_btn)

        self._resolution_group.setLayout(res_layout)
        self._right_layout.addWidget(self._resolution_group)

        self._right_layout.addStretch()
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        # -- Bottom buttons --
        btn_layout = QHBoxLayout()

        self._stop_btn = QPushButton(t("merge_window.stop_discard"))
        self._stop_btn.clicked.connect(self._stop_and_discard)
        self._stop_btn.setStyleSheet("color: #c0392b;")
        btn_layout.addWidget(self._stop_btn)

        btn_layout.addStretch()

        self._skip_btn = QPushButton(t("merge_window.skip_and_finish"))
        self._skip_btn.clicked.connect(self._skip_and_finish)
        self._skip_btn.setVisible(False)
        self._skip_btn.setStyleSheet("color: #e67e22;")
        btn_layout.addWidget(self._skip_btn)

        self._next_btn = QPushButton(t("merge_window.next_step"))
        self._next_btn.clicked.connect(self._next_step)
        btn_layout.addWidget(self._next_btn)

        self._finish_btn = QPushButton(t("merge_window.finish_merge"))
        self._finish_btn.clicked.connect(self._finish_merge)
        self._finish_btn.setVisible(False)
        btn_layout.addWidget(self._finish_btn)

        root.addLayout(btn_layout)

        # -- Connect radio changes to auto-fill rename fields --
        self._radio_keep_current.toggled.connect(self._on_resolution_changed)
        self._radio_keep_imported.toggled.connect(self._on_resolution_changed)
        self._radio_rename_current.toggled.connect(self._on_resolution_changed)
        self._radio_rename_imported.toggled.connect(self._on_resolution_changed)
        self._radio_rename_both.toggled.connect(self._on_resolution_changed)

    # -- Step management --------------------------------------------------

    def _load_step(self, step: MergeStep) -> None:
        self._current_step = step
        self._conflicts = self._merge.build_conflicts(step)
        self._non_conflict_items = self._merge.get_non_conflict_items(step)
        non_conflict = len(self._non_conflict_items)

        self._step_label.setText(
            t("merge_window.step_indicator").format(
                step=step + 1,
                total=STEP_COUNT,
                label=MergeService.step_label(step),
            )
        )

        if self._conflicts:
            info = t("merge_window.conflicts_info").format(
                conflicts=len(self._conflicts),
                non_conflict=non_conflict,
            )
        else:
            info = t("merge_window.no_conflicts_info").format(
                non_conflict=non_conflict,
            )
        self._info_label.setText(info)

        # Populate conflict list
        self._conflict_list.clear()
        for c in self._conflicts:
            item = QListWidgetItem(c.name)
            item.setData(Qt.UserRole, c)
            self._conflict_list.addItem(item)

        # Populate non-conflict list with checkboxes
        self._non_conflict_list.clear()
        for nc_item in self._non_conflict_items:
            name = nc_item.name
            list_item = QListWidgetItem()
            list_item.setText(name)
            list_item.setData(Qt.UserRole, nc_item)
            list_item.setFlags(list_item.flags() | Qt.ItemIsUserCheckable)
            list_item.setCheckState(Qt.Checked)  # default: selected for import
            self._non_conflict_list.addItem(list_item)

        # Update auto-merge button text
        if non_conflict > 0:
            self._auto_merge_btn.setText(
                t("merge_window.auto_merge_selected_count").format(count=non_conflict)
            )
            self._auto_merge_btn.setVisible(True)
        else:
            self._auto_merge_btn.setVisible(False)

        # Select first conflict
        if self._conflicts:
            self._conflict_list.setCurrentRow(0)
            self._right_panel_enabled(True)
        else:
            self._right_panel_enabled(False)

        # Update buttons
        is_last = (step == STEP_INVS)
        show_skip = (step >= STEP_PRODUCT_PRESETS)  # steps 2, 3, 4
        self._skip_btn.setVisible(show_skip)
        self._next_btn.setVisible(not is_last)
        self._finish_btn.setVisible(is_last)

        self._selected_index = 0

    def _on_conflict_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._conflicts):
            self._right_panel_enabled(False)
            return
        self._selected_index = index
        self._show_conflict_detail(self._conflicts[index])

    def _show_conflict_detail(self, c: MergeConflict) -> None:
        """Populate the right panel for *c*."""
        self._current_item_label.setText(self._describe_item(c.current_item))
        self._imported_item_label.setText(self._describe_item(c.imported_item))

        # Reset radios
        self._radio_keep_current.setChecked(True)
        self._rename_current_edit.clear()
        self._rename_imported_edit.clear()

        # Pre-fill rename suggestions
        sugg_current = c.name + "_current"
        sugg_imported = c.name + "_imported"
        self._rename_current_edit.setPlaceholderText(sugg_current)
        self._rename_imported_edit.setPlaceholderText(sugg_imported)

        # If already resolved, restore state
        if c.resolution == "keep_current":
            self._radio_keep_current.setChecked(True)
        elif c.resolution == "keep_imported":
            self._radio_keep_imported.setChecked(True)
        elif c.resolution == "rename_current":
            self._radio_rename_current.setChecked(True)
            self._rename_current_edit.setText(c.rename_current_to)
        elif c.resolution == "rename_imported":
            self._radio_rename_imported.setChecked(True)
            self._rename_imported_edit.setText(c.rename_imported_to)
        elif c.resolution == "keep_both_renamed":
            self._radio_rename_both.setChecked(True)
            self._rename_current_edit.setText(c.rename_current_to)
            self._rename_imported_edit.setText(c.rename_imported_to)

        self._right_panel_enabled(True)

    def _right_panel_enabled(self, enabled: bool) -> None:
        self._current_item_group.setVisible(enabled)
        self._imported_item_group.setVisible(enabled)
        self._resolution_group.setVisible(enabled)

    def _on_resolution_changed(self) -> None:
        """Enable/disable rename fields based on radio selection."""
        keep_current = self._radio_keep_current.isChecked()
        keep_imported = self._radio_keep_imported.isChecked()
        rename_current = self._radio_rename_current.isChecked()
        rename_imported = self._radio_rename_imported.isChecked()
        rename_both = self._radio_rename_both.isChecked()

        self._rename_current_edit.setEnabled(rename_current or rename_both)
        self._rename_imported_edit.setEnabled(rename_imported or rename_both)

    # -- Actions ----------------------------------------------------------

    def _apply_current_resolution(self) -> None:
        if not self._conflicts or self._selected_index >= len(self._conflicts):
            return

        c = self._conflicts[self._selected_index]
        step = self._current_step

        # Determine resolution
        if self._radio_keep_current.isChecked():
            c.resolution = "keep_current"
        elif self._radio_keep_imported.isChecked():
            c.resolution = "keep_imported"
        elif self._radio_rename_current.isChecked():
            new_name = self._rename_current_edit.text().strip()
            if not new_name:
                QMessageBox.warning(
                    self,
                    t("merge_window.invalid_name_title"),
                    t("merge_window.invalid_name_current"),
                )
                return
            if not self._merge.is_name_available_for_step(step, new_name, c.name):
                QMessageBox.warning(
                    self,
                    t("merge_window.name_conflict_title"),
                    t("merge_window.name_conflict_in_use").format(name=new_name),
                )
                return
            c.resolution = "rename_current"
            c.rename_current_to = new_name
        elif self._radio_rename_imported.isChecked():
            new_name = self._rename_imported_edit.text().strip()
            if not new_name:
                QMessageBox.warning(
                    self,
                    t("merge_window.invalid_name_title"),
                    t("merge_window.invalid_name_imported"),
                )
                return
            if not self._merge.is_name_available_for_step(step, new_name):
                QMessageBox.warning(
                    self,
                    t("merge_window.name_conflict_title"),
                    t("merge_window.name_conflict_already_used").format(name=new_name),
                )
                return
            c.resolution = "rename_imported"
            c.rename_imported_to = new_name
        elif self._radio_rename_both.isChecked():
            new_cur = self._rename_current_edit.text().strip()
            new_imp = self._rename_imported_edit.text().strip()
            if not new_cur and not new_imp:
                QMessageBox.warning(
                    self,
                    t("merge_window.invalid_name_title"),
                    t("merge_window.invalid_name_both"),
                )
                return
            if new_cur and not self._merge.is_name_available_for_step(
                step, new_cur, c.name
            ):
                QMessageBox.warning(
                    self,
                    t("merge_window.name_conflict_title"),
                    t("merge_window.name_conflict_already_used").format(name=new_cur),
                )
                return
            if new_imp and not self._merge.is_name_available_for_step(step, new_imp):
                QMessageBox.warning(
                    self,
                    t("merge_window.name_conflict_title"),
                    t("merge_window.name_conflict_already_used").format(name=new_imp),
                )
                return
            c.resolution = "keep_both_renamed"
            c.rename_current_to = new_cur
            c.rename_imported_to = new_imp
        else:
            return

        # Apply the resolution
        try:
            self._merge.resolve_conflict(step, c)
        except Exception as e:
            QMessageBox.critical(
                self,
                t("merge_window.invalid_name_title"),
                t("merge_window.resolution_failed").format(error=e),
            )
            return

        # Calculate where the selection should land *before* modifying the
        # list widget.  takeItem() causes Qt to emit currentRowChanged with
        # an unreliable intermediate value; blocking signals while we remove
        # the item avoids corrupting self._selected_index.
        removed_index = self._selected_index
        remaining = len(self._conflicts) - 1

        del self._conflicts[removed_index]

        self._conflict_list.blockSignals(True)
        self._conflict_list.takeItem(removed_index)
        self._conflict_list.blockSignals(False)

        if self._conflicts:
            new_index = min(removed_index, remaining - 1)
            self._conflict_list.setCurrentRow(new_index)
        else:
            self._right_panel_enabled(False)
            self._info_label.setText(t("merge_window.all_resolved"))

    # -- Auto-merge (non-conflicting items, user-triggered) ----------------

    def _apply_auto_merge_selected(self) -> None:
        """Merge only the non-conflict items that are checked by the user."""
        selected_names: set[str] = set()
        unchecked_names: set[str] = set()
        for i in range(self._non_conflict_list.count()):
            list_item = self._non_conflict_list.item(i)
            if list_item is None:
                continue
            nc_item = list_item.data(Qt.UserRole)
            if nc_item is None:
                continue
            if list_item.checkState() == Qt.Checked:
                selected_names.add(nc_item.name)
            else:
                unchecked_names.add(nc_item.name)

        if not selected_names:
            QMessageBox.information(
                self,
                t("merge_window.auto_merge_title"),
                t("merge_window.auto_merge_none_selected"),
            )
            return

        self._merge.apply_auto_merge_items(self._current_step, selected_names)

        # Remove merged items from the non-conflict list
        for i in reversed(range(self._non_conflict_list.count())):
            list_item = self._non_conflict_list.item(i)
            if list_item is None:
                continue
            nc_item = list_item.data(Qt.UserRole)
            if nc_item is not None and nc_item.name in selected_names:
                self._non_conflict_list.takeItem(i)

        # Also remove from our internal list
        self._non_conflict_items = [
            nci for nci in self._non_conflict_items
            if nci.name not in selected_names
        ]

        # Update info label
        remaining_non_conflict = len(self._non_conflict_items)
        if self._conflicts:
            self._info_label.setText(
                t("merge_window.conflicts_info").format(
                    conflicts=len(self._conflicts),
                    non_conflict=remaining_non_conflict,
                )
            )
        else:
            self._info_label.setText(
                t("merge_window.no_conflicts_info").format(
                    non_conflict=remaining_non_conflict,
                )
            )

        # Update button
        if remaining_non_conflict > 0:
            self._auto_merge_btn.setText(
                t("merge_window.auto_merge_selected_count").format(
                    count=remaining_non_conflict
                )
            )
        else:
            self._auto_merge_btn.setVisible(False)

    def _get_checked_non_conflict_names(self) -> set[str]:
        """Return the set of names of non-conflict items the user chose to import."""
        selected: set[str] = set()
        for i in range(self._non_conflict_list.count()):
            list_item = self._non_conflict_list.item(i)
            if list_item is not None and list_item.checkState() == Qt.Checked:
                nc_item = list_item.data(Qt.UserRole)
                if nc_item is not None:
                    selected.add(nc_item.name)
        return selected

    # -- Select all / deselect all ----------------------------------------

    def _select_all_non_conflict(self) -> None:
        """Check all non-conflict items."""
        for i in range(self._non_conflict_list.count()):
            item = self._non_conflict_list.item(i)
            if item is not None:
                item.setCheckState(Qt.Checked)

    def _deselect_all_non_conflict(self) -> None:
        """Uncheck all non-conflict items."""
        for i in range(self._non_conflict_list.count()):
            item = self._non_conflict_list.item(i)
            if item is not None:
                item.setCheckState(Qt.Unchecked)

    # -- Navigation --------------------------------------------------------

    def _next_step(self) -> None:
        """Advance to the next merge step.

        - Unresolved conflicts are automatically kept as the current version.
        - Non-conflict items that were NOT selected by the user are discarded.
        - Non-conflict items that the user checked are NOT auto-merged here;
          the user must explicitly click the auto-merge button for those.
          If the user forgot, we ask whether to merge them now or discard.
        """
        # Resolve remaining conflicts by keeping current
        if self._conflicts:
            self._merge.resolve_remaining_conflicts_as_keep_current(
                self._current_step, self._conflicts
            )

        # Handle undecided non-conflict items
        checked = self._get_checked_non_conflict_names()
        unchecked_count = len(self._non_conflict_items) - len(checked)

        if checked:
            # User has checked items but not yet merged them
            reply = QMessageBox.question(
                self,
                t("merge_window.auto_merge_title"),
                t("merge_window.auto_merge_pending").format(
                    count=len(checked),
                    unchecked=unchecked_count,
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self._merge.apply_auto_merge_items(self._current_step, checked)
        # unchecked items are simply discarded (not merged)

        next_step = self._current_step + 1
        if next_step >= STEP_COUNT:
            self._finish_merge()
            return
        self._load_step(next_step)

    def _skip_and_finish(self) -> None:
        """Skip the current step and all remaining steps, then finish the merge.

        Changes from previous steps are retained. For the current step:
        - Already-resolved conflicts keep their resolution.
        - Unresolved conflicts are kept as the current version.
        - Non-conflict items that the user checked are auto-merged.
        For remaining steps, all non-conflict items are auto-merged.
        """
        step_label = MergeService.step_label(self._current_step)
        remaining = STEP_COUNT - self._current_step

        reply = QMessageBox.question(
            self,
            t("merge_window.skip_and_finish_title"),
            t("merge_window.skip_and_finish_msg").format(
                step=step_label,
                remaining=remaining,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.No:
            return

        # Resolve remaining conflicts in current step as keep_current
        if self._conflicts:
            self._merge.resolve_remaining_conflicts_as_keep_current(
                self._current_step, self._conflicts
            )

        # Auto-merge checked non-conflict items for the current step
        checked = self._get_checked_non_conflict_names()
        if checked:
            self._merge.apply_auto_merge_items(self._current_step, checked)

        # Auto-merge all non-conflict items for remaining steps
        for s in range(self._current_step + 1, STEP_COUNT):
            self._merge.apply_auto_merge(s)

        self._merge.finalise()
        self.merge_finished.emit()
        self.accept()

    def _finish_merge(self) -> None:
        """Complete the merge on the last step."""
        # Resolve remaining conflicts by keeping current
        if self._conflicts:
            self._merge.resolve_remaining_conflicts_as_keep_current(
                self._current_step, self._conflicts
            )

        # Handle undecided non-conflict items
        checked = self._get_checked_non_conflict_names()
        if checked:
            reply = QMessageBox.question(
                self,
                t("merge_window.auto_merge_title"),
                t("merge_window.auto_merge_pending_finish").format(
                    count=len(checked),
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self._merge.apply_auto_merge_items(self._current_step, checked)

        self._merge.finalise()
        self.merge_finished.emit()
        self.accept()

    def _stop_and_discard(self) -> None:
        reply = QMessageBox.question(
            self,
            t("merge_window.stop_title"),
            t("merge_window.stop_msg"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._merge.revert()
            self.merge_finished.emit()
            self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Intercept the window-close (X) button so it behaves like Stop."""
        self._stop_and_discard()
        # If the user declined, ignore the close event.
        if self.isVisible():
            event.ignore()
        else:
            event.accept()

    # -- Describe items for display ---------------------------------------

    @staticmethod
    def _describe_item(item: object) -> str:
        """Return a human-readable description of a model item."""
        if hasattr(item, "name") and hasattr(item, "length"):
            # LengthIdentifier
            li = item
            return (
                f"<b>{t('merge_window.item_name')}:</b> {li.name}<br>"
                f"<b>{t('merge_window.item_length')}:</b> {li.length.length_str}<br>"
                f"<b>{t('merge_window.item_label')}:</b> {li.label or t('merge_window.none')}"
            )
        if hasattr(item, "product_type"):
            # ProductPreset
            pp = item
            return (
                f"<b>{t('merge_window.item_name')}:</b> {pp.name}<br>"
                f"<b>{t('merge_window.item_type')}:</b> {pp.product_type}<br>"
                f"<b>{t('merge_window.item_remarks')}:</b> {pp.remarks or t('merge_window.none')}"
            )
        if hasattr(item, "product_presets") and hasattr(item, "gap_extenders"):
            # Rackings
            r = item
            return (
                f"<b>{t('merge_window.item_name')}:</b> {r.name}<br>"
                f"<b>{t('merge_window.item_product_presets')}:</b> {r.product_presets or t('merge_window.none')}<br>"
                f"<b>{t('merge_window.item_gap_extenders')}:</b> {len(r.gap_extenders)}{t('merge_window.item_suffix')}"
            )
        if hasattr(item, "nodes") and hasattr(item, "racking_pitch"):
            # Inv
            inv = item
            node_count = len(inv.nodes)
            return (
                f"<b>{t('merge_window.item_name')}:</b> {inv.name}<br>"
                f"<b>{t('merge_window.item_nodes')}:</b> {node_count}<br>"
                f"<b>{t('merge_window.item_racking_pitch')}:</b> {inv.racking_pitch.length_str if inv.racking_pitch else t('merge_window.none')}"
            )
        return f"<b>{item.__class__.__name__}:</b> {getattr(item, 'name', str(item))}"
