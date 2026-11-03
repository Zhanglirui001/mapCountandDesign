"""Tests for source and frozen executable environment-file discovery."""

from pathlib import Path
import os
import sys

from count_and_design.application.env import load_env_file, resolve_env_path


def test_resolve_env_path_finds_pyinstaller_bundled_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exe_dir = tmp_path / "exe"
    bundle_dir = tmp_path / "bundle"
    working_dir = tmp_path / "cwd"
    exe_dir.mkdir()
    bundle_dir.mkdir()
    working_dir.mkdir()
    bundled_env = bundle_dir / ".env"
    bundled_env.write_text("TEST_VALUE=embedded\n", encoding="utf-8")

    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "app.exe"))

    assert resolve_env_path(None) == bundled_env


def test_external_env_beside_exe_overrides_bundled_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exe_dir = tmp_path / "exe"
    bundle_dir = tmp_path / "bundle"
    exe_dir.mkdir()
    bundle_dir.mkdir()
    external_env = exe_dir / ".env"
    external_env.write_text("TEST_VALUE=external\n", encoding="utf-8")
    (bundle_dir / ".env").write_text("TEST_VALUE=embedded\n", encoding="utf-8")

    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "app.exe"))

    assert resolve_env_path(None) == external_env


def test_process_environment_overrides_all_discovered_env_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exe_dir = tmp_path / "exe"
    bundle_dir = tmp_path / "bundle"
    working_dir = tmp_path / "cwd"
    exe_dir.mkdir()
    bundle_dir.mkdir()
    working_dir.mkdir()
    (bundle_dir / ".env").write_text("TEST_VALUE=embedded\n", encoding="utf-8")
    (exe_dir / ".env").write_text("TEST_VALUE=external\n", encoding="utf-8")
    (working_dir / ".env").write_text("TEST_VALUE=working\n", encoding="utf-8")

    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setenv("TEST_VALUE", "process")
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "app.exe"))

    load_env_file()

    assert os.environ["TEST_VALUE"] == "process"
