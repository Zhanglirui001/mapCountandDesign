"""Lightweight version manager — replacement for release-please on GitLab.

Analyses conventional commits since the last release tag, computes the next
semantic version, updates version files, and creates a GitLab Release via API.

Usage (CI):
    python version_manager.py

Environment variables expected:
    GITLAB_API_TOKEN   — GitLab Project Access Token with ``api`` scope
    CI_PROJECT_ID      — set automatically by GitLab CI
    CI_API_V4_URL      — set automatically by GitLab CI
    CI_SERVER_URL      — set automatically by GitLab CI
    CI_COMMIT_BRANCH   — set automatically by GitLab CI
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

INIT_FILE = Path("src/count_and_design/__init__.py")
MANIFEST_FILE = Path(".release-please-manifest.json")
CHANGELOG_FILE = Path("CHANGELOG.md")
RELEASE_BRANCH = "master"

# Conventional commit patterns
FEAT_RE = re.compile(r'^\s*feat(\([^)]*\))?[!:]')
FIX_RE = re.compile(r'^\s*fix(\([^)]*\))?[!:]')
BREAKING_RE = re.compile(r'BREAKING[ -]CHANGE|!:')

# ── API helpers ──────────────────────────────────────────────────────────────


def _api_url(path: str) -> str:
    base = os.environ.get("CI_API_V4_URL", "")
    if not base:
        raise RuntimeError("CI_API_V4_URL not set")
    return f"{base}/{path}"


def _api_headers() -> dict[str, str]:
    token = os.environ.get("GITLAB_API_TOKEN", "")
    if not token:
        raise RuntimeError("GITLAB_API_TOKEN not set")
    return {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}

# ═══════════════════════════════════════════════════════════════════════════════
# Version helpers
# ═══════════════════════════════════════════════════════════════════════════════


class Version(NamedTuple):
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, bump_type: str) -> Version:
        if bump_type == "major":
            return Version(self.major + 1, 0, 0)
        if bump_type == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)


def _read_current_version() -> Version:
    text = INIT_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        raise RuntimeError(f"Cannot parse version from {INIT_FILE}")
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _write_version(version: Version) -> None:
    text = INIT_FILE.read_text(encoding="utf-8")
    new_text = re.sub(
        r'__version__\s*=\s*"\d+\.\d+\.\d+"',
        f'__version__ = "{version}"',
        text,
    )
    INIT_FILE.write_text(new_text, encoding="utf-8")
    print(f"Updated {INIT_FILE} -> {version}")

    # Also update manifest
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    manifest["."] = str(version)
    MANIFEST_FILE.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Updated {MANIFEST_FILE} -> {version}")

# ═══════════════════════════════════════════════════════════════════════════════
# Git helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _get_last_tag() -> str | None:
    """Return the most recent version tag, or None."""
    try:
        result = subprocess.run(
            ["git", "tag", "--sort=-v:refname"],
            capture_output=True, text=True, encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if re.match(r'^v?\d+\.\d+\.\d+$', line):
            return line
    return None


def _commits_since(tag: str | None) -> list[str]:
    """Return commit messages (subject + body) since *tag* (or all if tag is None).

    Commits are separated by null bytes so that multi-line bodies are preserved.
    """
    range_spec = f"{tag}..HEAD" if tag else "HEAD"
    try:
        result = subprocess.run(
            ["git", "log", range_spec, "--no-merges", "--pretty=format:%B%x00"],
            capture_output=True, text=True, encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [c for c in (c.strip() for c in result.stdout.split('\x00')) if c]


def _classify_commits(commits: list[str]) -> dict[str, int]:
    """Count commits by type.

    Returns ``{"major": n, "minor": n, "patch": n}``.
    """
    counts = {"major": 0, "minor": 0, "patch": 0}
    for msg in commits:
        # Check for breaking changes first — BREAKING_RE uses search()
        # so it finds BREAKING CHANGE footers anywhere in the body.
        if BREAKING_RE.search(msg):
            counts["major"] += 1
        elif FEAT_RE.match(msg):
            counts["minor"] += 1
        elif FIX_RE.match(msg):
            counts["patch"] += 1
        # Other types (chore, docs, etc.) are ignored for versioning
    return counts


def _determine_bump(counts: dict[str, int]) -> str | None:
    """Determine the highest-priority bump type, or None if no bump needed."""
    if counts["major"] > 0:
        return "major"
    if counts["minor"] > 0:
        return "minor"
    if counts["patch"] > 0:
        return "patch"
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# GitLab API
# ═══════════════════════════════════════════════════════════════════════════════


def _create_gitlab_release(tag: str, version: Version, changes: str) -> None:
    """Create a GitLab Release via the REST API."""
    import urllib.request

    # Prepend full RELEASE_NOTES.md content as user-facing release notes
    release_notes_path = Path("RELEASE_NOTES.md")
    if release_notes_path.exists():
        notes = release_notes_path.read_text(encoding="utf-8").strip()
        if notes:
            changes = notes + "\n\n---\n\n" + changes

    payload = json.dumps({
        "tag_name": tag,
        "name": f"Release {tag}",
        "description": changes,
        "ref": RELEASE_BRANCH,
    }).encode("utf-8")

    url = _api_url(f"projects/{os.environ['CI_PROJECT_ID']}/releases")
    req = urllib.request.Request(url, data=payload, headers=_api_headers(), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            resp_data = json.loads(resp.read())
            print(f"Release created: {resp_data.get('tag_name')} "
                  f"(id={resp_data.get('id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[ERROR] Create release failed: HTTP {e.code}")
        print(body)
        raise


def _get_changelog_section(version: Version, commits: list[str]) -> str:
    """Build a simple changelog section from commit messages."""
    lines = [f"## {version}\n"]
    feat_lines: list[str] = []
    fix_lines: list[str] = []
    other_lines: list[str] = []

    for msg in commits:
        subject = msg.split('\n')[0].strip()
        if BREAKING_RE.search(msg):
            feat_lines.append(f"- **BREAKING**: {subject}")
        elif FEAT_RE.match(subject):
            feat_lines.append(f"- {subject}")
        elif FIX_RE.match(subject):
            fix_lines.append(f"- {subject}")
        else:
            other_lines.append(f"- {subject}")

    if feat_lines:
        lines.append("### Features")
        lines.extend(feat_lines)
        lines.append("")
    if fix_lines:
        lines.append("### Fixes")
        lines.extend(fix_lines)
        lines.append("")
    if other_lines:
        lines.append("### Other")
        lines.extend(other_lines)
        lines.append("")

    return "\n".join(lines)


def _prepend_changelog(version: Version, section: str) -> None:
    """Prepend a new section to CHANGELOG.md."""
    if CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text(encoding="utf-8")
    else:
        existing = "# Changelog\n\n"

    # Insert after the header line
    first_newline = existing.find("\n")
    if first_newline == -1:
        header_end = len(existing)
        separator = "\n\n"
    else:
        header_end = first_newline + 1
        separator = "\n"
    new_content = existing[:header_end] + separator + section + existing[header_end:]
    CHANGELOG_FILE.write_text(new_content, encoding="utf-8")
    print(f"Updated {CHANGELOG_FILE}")

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    if branch != RELEASE_BRANCH:
        print(f"[SKIP] version_manager only runs on '{RELEASE_BRANCH}' branch "
              f"(current: '{branch}')")
        return 0

    current = _read_current_version()
    print(f"Current version: {current}")

    last_tag = _get_last_tag()
    print(f"Last tag: {last_tag or '(none)'}")

    commits = _commits_since(last_tag)
    print(f"Commits since last tag: {len(commits)}")

    if not commits:
        print("[SKIP] No new commits since last tag — nothing to release.")
        return 0

    counts = _classify_commits(commits)
    print(f"Commit analysis: major={counts['major']}, minor={counts['minor']}, "
          f"patch={counts['patch']}")

    bump_type = _determine_bump(counts)
    if bump_type is None:
        print("[SKIP] No feat/fix/breaking commits — nothing to release.")
        return 0

    new_version = current.bump(bump_type)
    tag = f"v{new_version}"
    print(f"Next version: {new_version}  (bump: {bump_type})  tag: {tag}")

    # Check if tag already exists
    try:
        subprocess.run(
            ["git", "rev-parse", tag],
            capture_output=True, check=True,
        )
        print(f"[SKIP] Tag {tag} already exists.")
        return 0
    except subprocess.CalledProcessError:
        pass

    # Build changelog section
    section = _get_changelog_section(new_version, commits)
    _prepend_changelog(new_version, section)

    # Write version files
    _write_version(new_version)

    # Commit version bump and tag
    subprocess.run(["git", "config", "user.email", "ci@gitlab"], check=True)
    subprocess.run(["git", "config", "user.name", "GitLab CI"], check=True)
    subprocess.run(
        ["git", "add", str(INIT_FILE), str(MANIFEST_FILE), str(CHANGELOG_FILE)],
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"chore: release {tag}"],
        check=True,
    )
    subprocess.run(["git", "tag", tag], check=True)

    # Build push URL from CI variables (works in any GitLab instance)
    server_url = os.environ.get("CI_SERVER_URL", "")
    if not server_url:
        raise RuntimeError("CI_SERVER_URL not set")
    # Strip protocol to insert credentials: http://host:port -> host:port
    host_port = server_url.split("://", 1)[1]
    token = os.environ.get("GITLAB_API_TOKEN", "")
    if not token:
        raise RuntimeError("GITLAB_API_TOKEN not set")
    project_path = os.environ.get("CI_PROJECT_PATH", "")
    if not project_path:
        raise RuntimeError("CI_PROJECT_PATH not set")
    proto = server_url.split("://")[0]
    push_url = f"{proto}://gitlab-ci-token:{token}@{host_port}/{project_path}.git"
    subprocess.run(
        [
            "git",
            "push",
            push_url,
            f"HEAD:refs/heads/{RELEASE_BRANCH}",
            "--tags",
        ],
        check=True,
    )
    print(f"Pushed tag {tag}")

    # Create GitLab Release
    _create_gitlab_release(tag, new_version, section)

    print(f"[OK] Release {tag} created successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
