"""Changelog dialog – merges RELEASE_NOTES.md and CHANGELOG.md."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from ...i18n import tr


def _find_project_root() -> Path:
    """Return the project root directory, works both in dev and frozen (PyInstaller)."""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile extracts datas (CHANGELOG.md, RELEASE_NOTES.md)
        # into sys._MEIPASS, not beside the exe.
        return Path(sys._MEIPASS)
    # Dev: this file is in src/count_and_design/gui/views/ — go up 5 levels to project root
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _read_markdown(path: Path) -> str | None:
    """Return file contents as a string, or None if the file doesn't exist."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return None


class ChangelogDialog(QDialog):
    """A read-only dialog that shows RELEASE_NOTES.md and CHANGELOG.md."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("dialog.changelog_title", "Changelog"))
        self.resize(720, 600)
        self.setMinimumSize(480, 360)
        self._setup_ui()
        self._load_content()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)
        self._browser.setReadOnly(True)
        layout.addWidget(self._browser)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _load_content(self) -> None:
        root = _find_project_root()
        parts: list[str] = []

        # 1) Manual release notes (optional)
        rn = _read_markdown(root / "RELEASE_NOTES.md")
        if rn:
            parts.append(rn)

        # 2) Auto-generated changelog
        cl = _read_markdown(root / "CHANGELOG.md")
        if cl:
            # Separate with a horizontal rule if we already have release notes
            if parts:
                parts.append("\n\n---\n\n")
            parts.append(cl)

        if not parts:
            self._browser.setPlainText(
                tr("dialog.changelog_empty", "No changelog found.")
            )
            return

        full = "".join(parts)
        self._browser.setMarkdown(full)
        # Scroll back to top after rendering
        self._browser.moveCursor(QTextCursor.Start)
