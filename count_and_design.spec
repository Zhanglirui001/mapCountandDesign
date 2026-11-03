# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Count and Design."""

import re
import sys
from pathlib import Path
from PyInstaller.building.datastruct import TOC
from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata

# 从 __init__.py 中读取版本号
_init_path = Path("src/count_and_design/__init__.py")
_version_match = re.search(
    r'__version__\s*=\s*["\']([^"\']+)["\']',
    _init_path.read_text(encoding="utf-8"),
)
_version = _version_match.group(1) if _version_match else "0.0.0"

# 翻译文件
_i18n_src = Path("src/count_and_design/i18n/translations")
_i18n_dest = "count_and_design/i18n/translations"
_i18n_files = [
    (str(f), _i18n_dest)
    for f in _i18n_src.glob("*.json")
]

# Changelog files (placed beside the exe so _find_project_root() can find them)
_changelog_files = [
    ("CHANGELOG.md", "."),
    ("RELEASE_NOTES.md", "."),
]

# Temporary test-build workaround: bundle the local, git-ignored .env inside
# the one-file executable. PyInstaller extracts it to sys._MEIPASS at runtime.
# Do not treat this as secure storage; packaged values can still be recovered.
_env_path = Path(".env")
_embedded_env = Path("embedded.env")
if _env_path.is_file():
    # Use a non-dot filename so one-file Windows builds reliably extract it.
    _embedded_env.write_text(_env_path.read_text(encoding="utf-8"), encoding="utf-8")
    _env_files = [(str(_embedded_env), ".")]
    print("[INFO] Bundling local .env as embedded.env into the executable.")
else:
    _env_files = []
    print("[WARN] Local .env not found; building without embedded environment configuration.")

_voltage_drop_data = [
    (
        "src/count_and_design/application/voltage_drop/data/resistance.json",
        "count_and_design/application/voltage_drop/data",
    ),
]

# Agent1 drawing category vectors.
_category_vector_data = [
    (
        "src/count_and_design/resources/embeddings/category_index.sqlite",
        "count_and_design/resources/embeddings",
    ),
]

# Agent2/Agent3 local detection models. Keep the resource path aligned with
# bundled_yolo_model_dir() for both one-file and source executions.
_local_yolo_model_data = [
    (
        "src/count_and_design/resources/models/node_tracker_yolov8n.onnx",
        "count_and_design/resources/models",
    ),
    (
        "src/count_and_design/resources/models/box_yolov8n.onnx",
        "count_and_design/resources/models",
    ),
    (
        "src/count_and_design/resources/models/block_schedule_yolov26s.onnx",
        "count_and_design/resources/models",
    ),
    (
        "src/count_and_design/resources/models/overall_schedule_yolov26s.onnx",
        "count_and_design/resources/models",
    ),
]

# PySide6 需要的隐藏导入
_pyside_hidden = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
]

# Packaged Agent2/Agent3 inference uses direct ONNX Runtime sessions. Ultralytics and
# its PyTorch dependency chain are development-only fallbacks. OpenCV is still
# required when AGENT3_LINE_ENHANCE is enabled.
_onnx_binaries = collect_dynamic_libs("onnxruntime")
_opencv_binaries = collect_dynamic_libs("cv2")
_yolo_hidden = ["onnxruntime", "cv2"]

# Data fill only needs CP-SAT from OR-Tools. Avoid collect_all(), which pulls
# every solver backend and duplicates native libraries in the archive.
_ortools_binaries = collect_dynamic_libs("ortools")
_ortools_hidden = [
    "ortools",
    "ortools.sat",
    "ortools.sat.python",
    "ortools.sat.python.cp_model",
    "ortools.sat.python.cp_model_helper",
]

a = Analysis(
    ["src/count_and_design/__main__.py"],
    pathex=["src"],
    binaries=(
        _ortools_binaries
        + _onnx_binaries
        + _opencv_binaries
    ),
    datas=(
        _i18n_files
        + _changelog_files
        + _env_files
        + _voltage_drop_data
        + _category_vector_data
        + _local_yolo_model_data
        + copy_metadata("mcp")
    ),
    hiddenimports=(
        _pyside_hidden
        + _ortools_hidden
        + _yolo_hidden
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "IPython",
        "jupyter",
        # Agent3 packaged builds route ONNX models through onnxruntime only.
        "ultralytics",
        "torch",
        "torchvision",
        "polars",
        "pandas",
        "matplotlib",
        "sympy",
        "onnx",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# Qt 6 on Windows uses the system ICU compatibility DLL. Some developer
# environments put an unrelated Poppler ICU build on PATH; PyInstaller can
# mistakenly collect that unversioned DLL and it then shadows System32's copy,
# causing ``ImportError: DLL load failed while importing QtCore`` at startup.
_incompatible_icu_names = {"icuuc.dll", "icudt78.dll"}
a.binaries = TOC(
    entry
    for entry in a.binaries
    if Path(entry[0]).name.lower() not in _incompatible_icu_names
)

# PATH may contain Conda OpenSSL DLLs with the same names but older exports.
# _ssl.pyd must use the libraries shipped with this build's Python runtime.
_python_dll_dir = Path(sys.base_prefix) / "DLLs"
_python_ssl = {
    path.name.lower(): path
    for pattern in ("libssl*.dll", "libcrypto*.dll")
    for path in _python_dll_dir.glob(pattern)
}
a.binaries = TOC(
    (name, str(_python_ssl.get(Path(name).name.lower(), source)), kind)
    for name, source, kind in a.binaries
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

_is_darwin = sys.platform == "darwin"
_app_name = f"Count and Design v{_version}"
_exe_name = "CountAndDesign" if _is_darwin else _app_name

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=_exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not _is_darwin,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if _is_darwin:
    app = BUNDLE(
        exe,
        name=f"{_app_name}.app",
        icon=None,
        bundle_identifier="com.countanddesign.app",
        info_plist={
            "CFBundleDisplayName": _app_name,
            "CFBundleName": _app_name,
            "CFBundleShortVersionString": _version,
            "CFBundleVersion": _version,
            "NSHighResolutionCapable": True,
        },
    )
