"""Main application window."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...i18n import LANGUAGE_NAMES, tr, TranslationManager
from ..controllers import (
    ProductExportController,
    ProjectFileController,
    ScreenshotOcrController,
    UpdateCheckController,
    VoltageNavigationController,
)
from ..tooltips import TooltipManager
from ..viewmodels import AppViewModel
from . import (
    AIStatisticsTab,
    ChangelogDialog,
    CountTab,
    HistoryTab,
    LengthIdentifiersTab,
    ProductPresetsTab,
    PVPanelsTab,
    RackingsTab,
)


class MainWindow(QMainWindow):
    """Main application window with tabs."""

    def __init__(self) -> None:
        super().__init__()
        self.resize(1200, 800)
        self._view_model = AppViewModel()
        self._i18n = TranslationManager.instance()
        self._tooltip_manager = TooltipManager.instance()
        self._file_controller = ProjectFileController(self, self._view_model)
        self._update_controller = UpdateCheckController(
            self,
            self._file_controller.save_before_update,
        )
        self._tabs: QTabWidget | None = None
        self._ai_statistics_tab: AIStatisticsTab | None = None
        self._count_tab: CountTab | None = None
        self._length_identifiers_tab: LengthIdentifiersTab | None = None
        self._rackings_tab: RackingsTab | None = None
        self._product_presets_tab: ProductPresetsTab | None = None
        self._pv_panels_tab: PVPanelsTab | None = None
        self._history_tab: HistoryTab | None = None
        self._file_menu: QWidget | None = None
        self._lang_menu: QWidget | None = None
        self._help_menu: QWidget | None = None
        self._version_label: QLabel | None = None
        self._ocr_action: QAction | None = None
        self._setup_ui()
        self._setup_menu()
        self._setup_status_bar()
        self._product_export_controller = ProductExportController(
            self,
            self.statusBar(),
            self._view_model,
        )
        self._ocr_controller = ScreenshotOcrController(
            self,
            self.statusBar(),
            self._ocr_action,
            self._view_model.ocr_service,
        )
        self._voltage_navigation = VoltageNavigationController(
            self._tabs,
            self.statusBar(),
            {
                "project": (1, self._view_model.count_tab, self._count_tab),
                "inv": (1, self._view_model.count_tab, self._count_tab),
                "node": (1, self._view_model.count_tab, self._count_tab),
                "row": (1, self._view_model.count_tab, self._count_tab),
                "rackings": (3, self._view_model.rackings_tab, self._rackings_tab),
                "product_preset": (
                    4,
                    self._view_model.product_presets_tab,
                    self._product_presets_tab,
                ),
                "pv_panel": (
                    5,
                    self._view_model.pv_panels_tab,
                    self._pv_panels_tab,
                ),
                "length_identifier": (
                    2,
                    self._view_model.length_identifiers_tab,
                    self._length_identifiers_tab,
                ),
            },
        )
        self._count_tab.voltage_issue_activated.connect(
            self._voltage_navigation.navigate
        )
        self._ai_statistics_tab.import_to_count_requested.connect(
            self._import_ai_statistics_to_count
        )
        self._view_model.count_tab.voltage_issues_changed.connect(
            self._voltage_navigation.on_issues_changed
        )
        self._view_model.state_changed.connect(self._render_state)
        self._i18n.language_changed.connect(self._on_language_changed)
        self._render_state(self._view_model.state)

    @property
    def _active_voltage_fingerprint(self) -> tuple[object, ...] | None:
        """Compatibility hook retained for existing integration tests."""
        return self._voltage_navigation.active_fingerprint

    # ── UI setup ─────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()

        self._ai_statistics_tab = AIStatisticsTab()
        self._tabs.addTab(
            self._ai_statistics_tab,
            tr("tab.ai_statistics", "AI Statistics"),
        )

        self._count_tab = CountTab(self._view_model.count_tab)
        self._tabs.addTab(self._count_tab, tr("tab.count", "Count"))

        self._length_identifiers_tab = LengthIdentifiersTab(self._view_model.length_identifiers_tab)
        self._tabs.addTab(self._length_identifiers_tab, tr("tab.length_identifiers", "Length Identifiers"))

        self._rackings_tab = RackingsTab(self._view_model.rackings_tab)
        self._tabs.addTab(self._rackings_tab, tr("tab.rackings", "Rackings"))

        self._product_presets_tab = ProductPresetsTab(self._view_model.product_presets_tab)
        self._tabs.addTab(self._product_presets_tab, tr("tab.product_presets", "Product Presets"))

        self._pv_panels_tab = PVPanelsTab(self._view_model.pv_panels_tab)
        self._tabs.addTab(self._pv_panels_tab, tr("tab.pv_panels", "PV Panels"))

        self._history_tab = HistoryTab(self._view_model.history_tab)
        self._history_tab.node_selected.connect(self._view_model.checkout_node)
        self._tabs.addTab(self._history_tab, tr("tab.history", "History"))

        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()
        menubar.clear()

        # File
        self._file_menu = menubar.addMenu(tr("menu.file", "File"))

        new_action = QAction(tr("menu.file.new", "New Project"), self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._new_project)
        self._file_menu.addAction(new_action)

        open_action = QAction(tr("menu.file.open", "Open..."), self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open_project)
        self._file_menu.addAction(open_action)

        save_action = QAction(tr("menu.file.save", "Save"), self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._save_project)
        self._file_menu.addAction(save_action)

        save_as_action = QAction(tr("menu.file.save_as", "Save As..."), self)
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.triggered.connect(self._save_project_as)
        self._file_menu.addAction(save_as_action)

        self._file_menu.addSeparator()

        merge_action = QAction(tr("menu.file.import_merge", "Import & Merge..."), self)
        merge_action.setShortcut("Ctrl+M")
        merge_action.triggered.connect(self._import_and_merge)
        self._file_menu.addAction(merge_action)

        fill_action = QAction(tr("menu.file.fill_data", "Fill Project Data..."), self)
        fill_action.triggered.connect(self._open_data_fill)
        self._file_menu.addAction(fill_action)

        self._file_menu.addSeparator()

        generate_action = QAction(tr("menu.file.generate_products", "Generate Products"), self)
        generate_action.setShortcut("Ctrl+G")
        generate_action.triggered.connect(self._generate_products)
        self._file_menu.addAction(generate_action)

        self._file_menu.addSeparator()

        exit_action = QAction(tr("menu.file.exit", "Exit"), self)
        exit_action.setShortcut("Alt+F4")
        exit_action.triggered.connect(self.close)
        self._file_menu.addAction(exit_action)

        # Language
        self._lang_menu = menubar.addMenu(tr("menu.language", "Language"))
        self._rebuild_language_menu()

        # OCR
        self._ocr_action = QAction(tr("menu.hints.screenshot_ocr", "OCR"), self)
        self._ocr_action.triggered.connect(self._start_screenshot_ocr)
        menubar.addAction(self._ocr_action)

        # Help
        self._help_menu = menubar.addMenu(tr("menu.help", "Help"))
        self._rebuild_help_menu()

    def _setup_status_bar(self) -> None:
        """Create status bar with a clickable version label."""
        status = QStatusBar()
        self.setStatusBar(status)

        self._version_label = QLabel()
        self._version_label.setStyleSheet(
            "QLabel { color: #888; padding: 0 8px; }"
            "QLabel:hover { color: #4a90d9; text-decoration: underline; }"
        )
        self._version_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._version_label.setToolTip(tr("updater.status_tip", "Click to check for updates"))
        self._version_label.mousePressEvent = lambda _e: self.check_for_updates_manual()
        status.addPermanentWidget(self._version_label)
        self._update_version_label()

    def _update_version_label(self) -> None:
        """Refresh the version label in the status bar."""
        if self._version_label is None:
            return
        self._version_label.setText(f"v{self._update_controller.running_version}")

    # ── Menu rebuilders ──────────────────────────────────────────────────

    def _rebuild_language_menu(self) -> None:
        if self._lang_menu is None:
            return
        self._lang_menu.clear()
        current_locale = self._i18n.locale
        for code in self._i18n.available_locales():
            label = LANGUAGE_NAMES.get(code, code)
            if code == current_locale:
                label = f"✓ {label}"
            action = QAction(label, self)
            action.setData(code)
            action.triggered.connect(lambda checked, c=code: self._switch_language(c))
            self._lang_menu.addAction(action)

    def _rebuild_help_menu(self) -> None:
        if self._help_menu is None:
            return
        self._help_menu.clear()

        update_action = QAction(tr("menu.help.update", "Update"), self)
        update_action.triggered.connect(self._show_version_manager)
        self._help_menu.addAction(update_action)

        changelog_action = QAction(tr("menu.help.changelog", "Changelog"), self)
        changelog_action.triggered.connect(self._show_changelog)
        self._help_menu.addAction(changelog_action)

        self._help_menu.addSeparator()
        toggle_action = QAction(tr("menu.hints.toggle", "Enable Hints"), self)
        toggle_action.setCheckable(True)
        toggle_action.setChecked(self._tooltip_manager.enabled)
        toggle_action.toggled.connect(self._on_toggle_hints)
        self._help_menu.addAction(toggle_action)


    # ── Update checking ──────────────────────────────────────────────────

    def check_for_updates_silent(self) -> None:
        self._update_controller.check_silent()

    def check_for_updates_manual(self) -> None:
        self._update_controller.check_manual()

    # ── Other dialogs ───────────────────────────────────────────────────

    def _show_changelog(self) -> None:
        ChangelogDialog(self).exec()

    def _show_version_manager(self) -> None:
        self._update_controller.show_version_manager()

    # ── Slots ─────────────────────────────────────────────────────────────

    def _on_toggle_hints(self, checked: bool) -> None:
        self._tooltip_manager.enabled = checked

    def _switch_language(self, locale: str) -> None:
        self._i18n.locale = locale
        self._setup_menu()
        self._ocr_controller.set_action(self._ocr_action)
        self._update_tab_labels()

    def _on_language_changed(self, _locale: str) -> None:
        self._view_model.refresh_window_state()
        self._update_tab_labels()

    def _update_tab_labels(self) -> None:
        if self._tabs is None:
            return
        self._tabs.setTabText(0, tr("tab.ai_statistics", "AI Statistics"))
        self._tabs.setTabText(1, tr("tab.count", "Count"))
        self._tabs.setTabText(2, tr("tab.length_identifiers", "Length Identifiers"))
        self._tabs.setTabText(3, tr("tab.rackings", "Rackings"))
        self._tabs.setTabText(4, tr("tab.product_presets", "Product Presets"))
        self._tabs.setTabText(5, tr("tab.pv_panels", "PV Panels"))
        self._tabs.setTabText(6, tr("tab.history", "History"))

    def _on_tab_changed(self, index: int) -> None:
        # AI Statistics owns no project ViewModel. Translate the visible
        # index back to the coordinator's six project-tab indexes.
        if index > 0:
            self._view_model.on_tab_changed(index - 1)

    def _start_screenshot_ocr(self) -> None:
        self._ocr_controller.start()

    # ── File operations ──────────────────────────────────────────────────

    def _new_project(self) -> None:
        self._file_controller.new_project()

    def _open_project(self) -> None:
        self._file_controller.open_project()

    def _save_project(self) -> None:
        self._file_controller.save_project()

    def _save_project_as(self) -> None:
        self._file_controller.save_project_as()

    def _save_before_update(self) -> None:
        self._file_controller.save_before_update()

    def _add_inv(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            tr("dialog.add_inv_title", "Add INV"),
            tr("dialog.add_inv_prompt", "Enter INV name:"),
            text=f"INV{len(self._view_model.count_tab.state.tree[0].children) + 1}",
        )
        if ok and name:
            self._view_model.add_inv(name)

    def _render_state(self, state) -> None:
        window_id = getattr(self, "_mcp_window_id", None)
        self.setWindowTitle(f"{state.title} [{window_id}]" if window_id else state.title)

    def _import_and_merge(self) -> None:
        self._file_controller.import_and_merge()

    def _open_data_fill(self) -> None:
        self._file_controller.open_data_fill()

    def _import_ai_statistics_to_count(self, payload: dict) -> None:
        try:
            self._view_model.import_ai_statistics_payload(payload)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                tr("ai_statistics.validation.import_failed", "Import Failed"),
                str(exc),
            )
            return
        if self._tabs is not None:
            self._tabs.setCurrentIndex(1)
        self.statusBar().showMessage(
            tr(
                "ai_statistics.validation.imported_to_count",
                "Imported validation JSON to Count.",
            ),
            5000,
        )

    def _generate_products(self) -> None:
        self._product_export_controller.run()

    def closeEvent(self, event) -> None:
        if not self._ai_statistics_tab._view_model.shutdown():
            event.ignore()
            return
        if (
            self._count_tab is not None
            and not self._count_tab.shutdown_voltage_calculation()
        ):
            event.ignore()
            return
        super().closeEvent(event)
