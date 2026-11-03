"""Internationalization (i18n) module.

Provides a singleton TranslationManager that loads JSON translation tables
and emits a signal when the language changes so that all views can refresh.

Usage:
    from count_and_design.i18n import tr, t

    # In widget constructors / render methods:
    label.setText(t("menu.file"))
    button.setText(t("rackings_tab.rename_all_btn"))

    # The translation tables live alongside this package:
    #   translations/en_US.json   (English – default / fallback)
    #   translations/zh_CN.json   (Chinese)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QObject, Signal


def _get_translations_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后，资源文件在 sys._MEIPASS 下
        return Path(sys._MEIPASS) / "count_and_design" / "i18n" / "translations"
    return Path(__file__).resolve().parent / "translations"


_TRANSLATIONS_DIR: Path = _get_translations_dir()

# Friendly names shown in the language-switch menu.
LANGUAGE_NAMES: dict[str, str] = {
    "en_US": "English",
    "zh_CN": "中文",
    "pl_PL": "Polski",
}


class TranslationManager(QObject):
    """Loads translation tables and notifies listeners on change."""

    language_changed = Signal(str)  # emits the new locale code

    _instance: ClassVar[TranslationManager | None] = None

    def __init__(self) -> None:
        if TranslationManager._instance is not None:
            raise RuntimeError("Use TranslationManager.instance()")
        super().__init__()
        self._locale: str = "en_US"
        self._table: dict[str, str] = {}
        self._load()

    @classmethod
    def instance(cls) -> TranslationManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- public API -------------------------------------------------

    @property
    def locale(self) -> str:
        return self._locale

    @locale.setter
    def locale(self, code: str) -> None:
        if code == self._locale:
            return
        self._locale = code
        self._load()
        self.language_changed.emit(code)

    def translate(self, key: str, default: str = "") -> str:
        """Return the translation for *key*, falling back to *default*."""
        return self._table.get(key, default)

    def available_locales(self) -> list[str]:
        """Sorted list of locale codes found in translations/."""
        locales: list[str] = []
        for p in sorted(_TRANSLATIONS_DIR.glob("*.json")):
            locales.append(p.stem)
        return locales

    # -- internals --------------------------------------------------

    def _load(self) -> None:
        path = _TRANSLATIONS_DIR / f"{self._locale}.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                self._table = json.load(fh)
        else:
            self._table = {}


# ---- module-level convenience -------------------------------------

def tr(key: str, default: str = "") -> str:
    """Shorthand for TranslationManager.instance().translate(key, default)."""
    return TranslationManager.instance().translate(key, default)


# Alias for readability – use whichever reads better in context.
t = tr
