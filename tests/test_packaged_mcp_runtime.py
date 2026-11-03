"""Deployment paths must not depend on an installed source tree or cwd."""

import sys
from pathlib import Path

from count_and_design.integration import runtime
from count_and_design.integration.mcp_server import BridgeClient


def test_frozen_runtime_uses_user_data_and_same_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    executable = tmp_path / "Program Files" / "Count and Design.exe"
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-data"))
    monkeypatch.delenv("COUNT_DESIGN_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("COUNT_DESIGN_BRIDGE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    client = BridgeClient()
    assert client.root == executable.parent
    assert client.discovery_file == tmp_path / "user-data" / "CountAndDesign" / "runtime" / "bridge.json"
    assert runtime.gui_command(client.discovery_file) == [
        str(executable), "--codex-bridge", str(client.discovery_file),
    ]


def test_explicit_discovery_is_shared_between_gui_and_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    configured = tmp_path / "custom" / "bridge.json"
    monkeypatch.setenv("COUNT_DESIGN_BRIDGE_FILE", str(configured))
    assert runtime.bridge_file() == BridgeClient().discovery_file == configured


def test_source_launch_preserves_python_module_entry(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    discovery = tmp_path / "bridge.json"
    assert runtime.gui_command(discovery) == [
        sys.executable, "-m", "count_and_design", "--codex-bridge", str(discovery),
    ]
