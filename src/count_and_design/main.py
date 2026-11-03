"""Application entry point."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication

from . import __version__
from .application.env import load_env_file
from .i18n import tr
from .gui import MainWindow

logger = logging.getLogger(__name__)


def main() -> int:
    """Run the application."""
    load_env_file()
    app = QApplication(sys.argv)
    app.setApplicationName(f"{tr('app.title', 'Count and Design')} v{__version__}")
    app.setOrganizationName("CountAndDesign")
    _configure_logging()

    # Install a global exception hook so that unhandled exceptions
    # are shown in the GUI rather than lost to the console.
    _install_excepthook()

    discovery = None
    if "--codex-bridge" in sys.argv:
        index = sys.argv.index("--codex-bridge")
        if index + 1 >= len(sys.argv):
            raise ValueError("--codex-bridge requires a discovery JSON path")
        discovery = Path(sys.argv[index + 1]).resolve()
    elif getattr(sys, "frozen", False):
        from .integration.runtime import bridge_file, new_bridge_file
        discovery = bridge_file() if os.environ.get("COUNT_DESIGN_BRIDGE_FILE") else new_bridge_file()

    # Only an explicitly shared bridge is single-instance; each ordinary launch
    # gets an independent bridge, project store and Qt process.
    instance_lock = None
    if discovery is not None:
        discovery.parent.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(discovery.with_suffix(".lock")))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(0):
            from .integration.mcp_server import BridgeClient
            try:
                BridgeClient(discovery).call("start_session")
            except Exception:
                logger.info("Another application instance is starting")
            return 0

    window = MainWindow()
    if discovery is not None:
        from .integration.runtime import window_id
        window._mcp_window_id = window_id(discovery)
        window.setWindowTitle(f"{window.windowTitle()} [{window_id(discovery)}]")
    window.show()
    if discovery is not None:
        from .integration.gui_bridge import GuiBridge
        window._codex_bridge = GuiBridge(window, discovery)
        app.aboutToQuit.connect(window._codex_bridge.close)

    # ── Automatic update check (runs after the event loop starts) ─────
    QTimer.singleShot(1500, window.check_for_updates_silent)

    try:
        return app.exec()
    finally:
        if instance_lock is not None:
            instance_lock.unlock()


def _configure_logging() -> None:
    """Persist operational and Agent1 performance logs for packaged builds."""

    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    log_dir = Path(location or Path.cwd()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / f"count-and-design-{os.getpid()}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(existing, RotatingFileHandler)
        for existing in root_logger.handlers
    ):
        root_logger.addHandler(handler)


def _install_excepthook() -> None:
    """Replace sys.excepthook with one that shows ErrorDialog.

    Unhandled exceptions will appear in a modal dialog with the
    full traceback visible and a "Copy" button.
    """
    original_hook = sys.excepthook

    def gui_excepthook(exc_type, exc_value, exc_tb):
        # Format the traceback
        detail = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)
        )
        # Avoid creating a dialog if there is no QApplication yet
        app = QApplication.instance()
        if app is None:
            original_hook(exc_type, exc_value, exc_tb)
            return
        # Import here to avoid circular imports at startup
        from .gui.views.error_dialog import ErrorDialog
        dlg = ErrorDialog(
            message=tr("error.unhandled", "An unexpected error occurred."),
            detail=detail,
        )
        dlg.exec()
        # Also call the original hook so errors still appear in console
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = gui_excepthook


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
