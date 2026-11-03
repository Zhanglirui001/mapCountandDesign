"""Paths and process arguments shared by the desktop and MCP entry points."""

import os
from pathlib import Path
import sys


def project_root() -> Path:
    default = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return Path(os.environ.get("COUNT_DESIGN_PROJECT_ROOT", default)).resolve()


def bridge_file() -> Path:
    configured = os.environ.get("COUNT_DESIGN_BRIDGE_FILE")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
        return base / "CountAndDesign" / "runtime" / "bridge.json"
    return project_root() / ".codex-runtime" / "bridge.json"


def gui_command(discovery_file: Path) -> list[str]:
    prefix = [sys.executable]
    if not getattr(sys, "frozen", False):
        prefix += ["-m", "count_and_design"]
    return prefix + ["--codex-bridge", str(discovery_file)]


def prepare_stdio() -> None:
    """Recover inherited pipes in a Windows PyInstaller windowed executable."""
    for name, handle_id, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        if getattr(sys, name) is not None:
            continue
        stream = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            get_handle = ctypes.windll.kernel32.GetStdHandle
            get_handle.argtypes = [wintypes.DWORD]
            get_handle.restype = wintypes.HANDLE
            handle = get_handle(handle_id & 0xFFFFFFFF)
            if handle and handle != ctypes.c_void_p(-1).value:
                flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
                fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
                stream = os.fdopen(fd, mode, encoding="utf-8", buffering=1)
        if stream is None:
            if name != "stderr":
                raise RuntimeError("--mcp requires inherited stdin/stdout pipes from an MCP client")
            stream = open(os.devnull, mode, encoding="utf-8")
        setattr(sys, name, stream)
