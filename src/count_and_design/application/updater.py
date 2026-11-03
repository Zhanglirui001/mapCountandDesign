"""Automatic update checker and installer.

Fetches the latest release from a GitLab instance via the GitLab REST API,
compares the remote version against the running version, and offers to download
and install the update.

Token
-----
The GitLab Personal Access Token or Project Access Token is loaded from the
``UPDATER_GITLAB_TOKEN`` environment setting.

**How to create the token:**

1. Go to your GitLab instance → Settings → Access Tokens
   (or Project Settings → Access Tokens for a Project Access Token)
2. Enter a name (e.g. "Updater")
3. Select the ``api`` scope (covers Releases and Package Registry access)
4. Click "Create project access token" and copy the value

This ``api`` scope covers all the API endpoints we need:

   - ``GET /api/v4/projects/{id}/releases``  → read releases
   - ``GET /api/v4/projects/{id}/packages/...`` → download assets

5. Add the token as a CI/CD variable named ``UPDATER_GITLAB_TOKEN``
   (Settings → CI/CD → Variables → Add variable)

6. The CI workflow (.gitlab-ci.yml) will inject it during the build.

Single-threaded streaming download
----------------------------------
Downloads use ``requests`` in a single thread with streaming I/O.  This
avoids issues with CDN redirects that can break parallel Range requests.
A ``threading.Event`` allows the caller to cancel mid-download.

Architecture for EXE self-replacement on Windows
------------------------------------------------
1. Download the new EXE to the same folder as the running EXE
   (so the user can always find it).
2. When the user accepts the update, launch the new EXE detached
   and immediately quit the running process.
3. The old EXE is left in place — the user can delete it manually.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, NamedTuple

import requests
from PySide6.QtCore import QSettings

from .env import env_bool, env_str, load_env_file

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# In-memory log capture — exposed for the UI log viewer
# ═══════════════════════════════════════════════════════════════════════════════

_updater_logs: list[str] = []


class _LogCaptureHandler(logging.Handler):
    """Handler that appends formatted log records to ``_updater_logs``."""

    def emit(self, record: logging.LogRecord) -> None:
        _updater_logs.append(self.format(record))


_capture_handler = _LogCaptureHandler()
_capture_handler.setLevel(logging.DEBUG)
_capture_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
)
logger.addHandler(_capture_handler)


def get_captured_logs() -> list[str]:
    """Return a copy of all captured updater log lines."""
    return list(_updater_logs)


def clear_captured_logs() -> None:
    """Clear the in-memory log buffer."""
    _updater_logs.clear()

# ═══════════════════════════════════════════════════════════════════════════════
# Build-time configuration — injected by CI
# ═══════════════════════════════════════════════════════════════════════════════

load_env_file()
_GITLAB_PROJECT_PATH: str = env_str("UPDATER_GITLAB_PROJECT_PATH", "")
_GITLAB_API_BASE: str = env_str("UPDATER_GITLAB_API_BASE", "")
_GITLAB_VERIFY_SSL: bool = env_bool("UPDATER_GITLAB_VERIFY_SSL", True)

# ═══════════════════════════════════════════════════════════════════════════════

_USER_AGENT = "CountAndDesign-Updater/1.0"

_SETTINGS_GROUP = "updater"
_SETTINGS_AUTO_CHECK = "auto_check_enabled"
_SETTINGS_SKIPPED_VERSION = "skipped_version"
_SETTINGS_CHANNEL = "update_channel"

_CHANNEL_STABLE = "stable"
_CHANNEL_PREVIEW = "preview"
_DEFAULT_CHANNEL = _CHANNEL_STABLE

# GitLab has no draft/prerelease flag, so release types are identified by an
# explicit allow-list. Branch-specific and SIT tags must not leak into either
# user-facing update channel.
_STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_DEV_PREVIEW_TAG_RE = re.compile(r"^v\d{8}\.\d{6}$")


def _release_channel(tag_name: str) -> str | None:
    """Return the user update channel for a release tag, if any."""
    if _STABLE_TAG_RE.fullmatch(tag_name):
        return _CHANNEL_STABLE
    if _DEV_PREVIEW_TAG_RE.fullmatch(tag_name):
        return _CHANNEL_PREVIEW
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


class ReleaseInfo(NamedTuple):
    tag_name: str
    version: str
    name: str
    body: str
    asset_name: str
    download_url: str
    asset_size: int


class UpdateCheckResult(NamedTuple):
    update_available: bool
    current_version: str
    latest_release: ReleaseInfo | None


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _normalize_version(v: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple of ints.

    Handles PEP 440 local versions (``+...``), pre-release segments
    (``-...``), and dev-release segments (``.dev...``).

    Examples
    --------
    >>> _normalize_version('1.3.2')
    (1, 3, 2)
    >>> _normalize_version('v2.0.0')
    (2, 0, 0)
    >>> _normalize_version('1.3.4.dev20260623+abc1234')
    (1, 3, 4, 20260623)
    """
    import re

    # Strip leading 'v', local version (+...), and pre-release (-...)
    v = v.lstrip("v").split("+")[0].split("-")[0]
    parts: list[int] = []
    for p in v.split("."):
        m = re.search(r'\d+', p)
        if m:
            parts.append(int(m.group(0)))
        else:
            break
    return tuple(parts) if parts else (0,)


def _running_version() -> str:
    from .. import __version__
    return __version__


def _exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.executable)


def _get_token() -> str | None:
    return env_str("UPDATER_GITLAB_TOKEN", "") or None


def _get_project_path() -> str:
    path = _GITLAB_PROJECT_PATH
    if not path:
        raise RuntimeError("Updater repository not configured.")
    return path


def _repo_owner_name() -> tuple[str, str]:
    """Parse ``_GITLAB_PROJECT_PATH`` into (owner, repo) tuple.

    Raises ``RuntimeError`` when the project path is not configured.
    """
    path = _GITLAB_PROJECT_PATH
    if not path:
        raise RuntimeError("Updater repository not configured.")
    parts = path.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    raise RuntimeError(f"Invalid project path: {path}")


def _get_api_base() -> str:
    return _GITLAB_API_BASE


def _ssl_verify() -> bool:
    """Return whether to verify SSL certificates.

    When disabled (intranet self-signed certs), also suppresses
    ``InsecureRequestWarning`` so the log is not spammed.
    """
    if not _GITLAB_VERIFY_SSL:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return _GITLAB_VERIFY_SSL


def _project_id_from_path(project_path: str) -> str:
    """Return the URL-encoded project path for use in GitLab API URLs."""
    return project_path.replace("/", "%2F")


def _auth_headers(token: str | None) -> dict[str, str]:
    """Return base HTTP headers for GitLab API / asset requests."""
    headers = {
        "User-Agent": _USER_AGENT,
    }
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


# ═══════════════════════════════════════════════════════════════════════════════
# Minimum version filter (internal — never shown in the UI)
# ═══════════════════════════════════════════════════════════════════════════════

_MIN_DOWNLOADABLE_VERSION = (1, 3, 2)


def _is_version_ge(version_str: str, min_version: tuple[int, ...]) -> bool:
    """Return True if *version_str* >= *min_version*."""
    return _normalize_version(version_str) >= min_version


# ═══════════════════════════════════════════════════════════════════════════════
# GitLab Releases API  (uses ``requests``)
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_release(payload: dict) -> ReleaseInfo | None:
    """Parse a single GitLab API release object into a :class:`ReleaseInfo`.

    Returns ``None`` when the release has no usable asset.
    """
    tag = payload.get("tag_name", "")
    version_str = tag.lstrip("v")

    # GitLab releases have assets.links (not assets like GitHub)
    assets_data = payload.get("assets", {}) or {}
    links = assets_data.get("links", [])
    link = None
    for a in links:
        if a.get("name", "").endswith(".exe"):
            link = a
            break
    if link is None and links:
        link = links[0]

    if link is None:
        return None

    return ReleaseInfo(
        tag_name=tag,
        version=version_str,
        name=payload.get("name", tag),
        body=payload.get("description", ""),
        asset_name=link.get("name", ""),
        download_url=link.get("url", ""),
        asset_size=0,  # filled later by _resolve_sizes()
    )


def _resolve_sizes(
    releases: list[ReleaseInfo],
    token: str | None = None,
) -> list[ReleaseInfo]:
    """Fill in asset sizes for *releases* via HEAD requests.

    GitLab release asset links do not include file size, so we query
    ``Content-Length`` from each download URL.  Failures are logged but
    non-fatal — the size stays 0.
    """
    if not releases:
        return releases

    session = requests.Session()
    session.headers.update(_auth_headers(token))
    session.headers["Accept"] = "application/octet-stream"
    verify = _ssl_verify()

    for i, rel in enumerate(releases):
        if not rel.download_url or rel.asset_size > 0:
            continue
        try:
            resp = session.head(rel.download_url, timeout=10, verify=verify,
                                allow_redirects=True)
            if resp.status_code == 200:
                length = resp.headers.get("Content-Length")
                if length and int(length) > 0:
                    releases[i] = rel._replace(asset_size=int(length))
        except requests.RequestException:
            logger.debug("HEAD %s failed — size unknown", rel.download_url)

    session.close()
    return releases


def fetch_all_releases(
    project_path: str = "",
    token: str | None = None,
    channel: str = _CHANNEL_STABLE,
) -> list[ReleaseInfo]:
    """Fetch **all** GitLab Releases for the repository (paginated).

    Only releases whose version is >= the internal minimum
    (``_MIN_DOWNLOADABLE_VERSION``) are returned.  The minimum is **never**
    shown in the UI — it is purely a safety guard.

    Parameters
    ----------
    channel:
        ``"stable"`` (default) — only full releases.
        ``"preview"`` — only development preview releases.

    Returns an empty list when the API is unreachable or authentication fails.
    """
    if not project_path:
        project_path = _get_project_path()
    token = token or _get_token()
    api_base = _get_api_base()
    encoded_path = _project_id_from_path(project_path)

    all_releases: list[ReleaseInfo] = []
    page = 1
    per_page = 100

    while True:
        url = (
            f"{api_base}/projects/{encoded_path}/releases"
            f"?per_page={per_page}&page={page}"
        )
        try:
            resp = requests.get(url, headers=_auth_headers(token), timeout=30, verify=_ssl_verify())
        except requests.RequestException as exc:
            logger.warning("GitLab API unreachable (releases list): %s", exc)
            raise

        if resp.status_code in (401, 403):
            logger.warning("GitLab API auth failed — token may be invalid or expired.")
            break
        if resp.status_code != 200:
            logger.warning("GitLab API returned HTTP %s for releases list", resp.status_code)
            raise requests.RequestException(
                f"GitLab API returned HTTP {resp.status_code}"
            )

        try:
            page_data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from GitLab API (releases list)")
            raise

        if not page_data:
            break  # no more pages

        for item in page_data:
            release_channel = _release_channel(item.get("tag_name", ""))
            if release_channel != channel:
                continue
            version_str = item.get("tag_name", "").lstrip("v")
            # Internal guard — minimum version is never exposed in the UI
            if not _is_version_ge(version_str, _MIN_DOWNLOADABLE_VERSION):
                continue
            ri = _parse_release(item)
            if ri is not None:
                all_releases.append(ri)

        # If this page was smaller than per_page, we've reached the end
        if len(page_data) < per_page:
            break
        page += 1

    return _resolve_sizes(all_releases, token)


def fetch_latest_release(project_path: str = "",
                         token: str | None = None,
                         channel: str = _CHANNEL_STABLE) -> ReleaseInfo | None:
    """Query the latest GitLab Release and return its info.

    Parameters
    ----------
    channel:
        ``"stable"`` (default) — return the latest full release.
        ``"preview"`` — return the latest pre-release.

    Returns ``None`` when the API is unreachable, authentication fails,
    or no suitable asset exists.
    """
    if not project_path:
        project_path = _get_project_path()
    token = token or _get_token()
    api_base = _get_api_base()
    encoded_path = _project_id_from_path(project_path)

    # GitLab has no /releases/latest endpoint — fetch releases sorted by
    # released_at DESC and pick the first matching one.
    url = f"{api_base}/projects/{encoded_path}/releases?per_page=20&page=1"
    try:
        resp = requests.get(url, headers=_auth_headers(token), timeout=30, verify=_ssl_verify())
    except requests.RequestException as exc:
        logger.warning("GitLab API unreachable: %s", exc)
        raise

    if resp.status_code in (401, 403):
        logger.warning("GitLab API auth failed — token may be invalid or expired.")
        return None
    if resp.status_code != 200:
        logger.warning("GitLab API returned HTTP %s", resp.status_code)
        raise requests.RequestException(
            f"GitLab API returned HTTP {resp.status_code}"
        )

    try:
        releases = resp.json()
    except ValueError:
        logger.warning("Invalid JSON from GitLab API")
        return None

    for item in releases:
        if _release_channel(item.get("tag_name", "")) != channel:
            continue
        ri = _parse_release(item)
        if ri is not None:
            return _resolve_sizes([ri], token)[0]

    if channel == _CHANNEL_PREVIEW:
        logger.info("No pre-release found")
    else:
        logger.warning("No downloadable asset in latest release")
    return None


def check_for_update(project_path: str = "",
                     token: str | None = None,
                     channel: str = _CHANNEL_STABLE) -> UpdateCheckResult:
    """Check whether a newer version is available on GitLab.

    Parameters
    ----------
    channel:
        ``"stable"`` (default) — check the latest full release.
        ``"preview"`` — check the latest pre-release.
    """
    if not project_path:
        project_path = _get_project_path()

    current = _running_version()
    token = token or _get_token()

    try:
        latest = fetch_latest_release(project_path, token=token, channel=channel)
    except requests.RequestException as exc:
        logger.warning("Update check failed: %s", exc)
        return UpdateCheckResult(False, current, None)
    if latest is None:
        return UpdateCheckResult(False, current, None)

    current_tuple = _normalize_version(current)
    latest_tuple = _normalize_version(latest.version)
    has_update = latest_tuple > current_tuple

    return UpdateCheckResult(has_update, current, latest)


# ═══════════════════════════════════════════════════════════════════════════════
# Single-threaded streaming download  (uses ``requests``)
# ═══════════════════════════════════════════════════════════════════════════════


def _download(
    url: str,
    dest: Path,
    total_size: int,
    token: str | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[bool, str]:
    """Download *url* to *dest* as a single streaming GET.

    *progress_callback* is called as ``callback(downloaded_bytes, total_size)``
    from the calling thread.

    If *cancel_event* is set, the download is aborted and the partial file
    is removed.

    Returns ``(True, "")`` on success, ``(False, error_message)`` on failure/cancel.
    """
    headers = _auth_headers(token)
    headers["Accept"] = "application/octet-stream"

    try:
        resp = requests.get(url, headers=headers, timeout=120, stream=True, verify=_ssl_verify())
        if resp.status_code not in (200, 302):
            logger.warning("Download HTTP %s", resp.status_code)
            return False, f"HTTP {resp.status_code}"

        # If the server doesn't report a Content-Length, use the asset size
        effective_total = total_size
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > 0:
            effective_total = int(content_length)

        with open(dest, "wb") as f:
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if cancel_event and cancel_event.is_set():
                    logger.info("Download cancelled")
                    f.close()
                    dest.unlink(missing_ok=True)
                    return False, "cancelled"
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, max(effective_total, downloaded))
        return True, ""
    except requests.RequestException as exc:
        logger.warning("Download failed: %s", exc)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False, str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Installation
# ═══════════════════════════════════════════════════════════════════════════════


def install_update(
    release: ReleaseInfo,
    token: str | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Download the update next to the running EXE.

    Returns the path to the downloaded EXE on success, or ``None`` on failure.
    The caller is responsible for launching the new EXE and quitting.

    If *cancel_event* is set during the download, the download is aborted
    and ``None`` is returned.
    """
    token = token or _get_token()
    download_url = release.download_url
    if not download_url:
        logger.error("No download URL in release info")
        return None

    exe_path = _exe_path()
    app_dir = exe_path.parent

    # Download the new EXE right next to the running one so the user can
    # always find it.
    new_exe = app_dir / f"Count and Design v{release.version}.exe"

    # Guard: if for some reason the new name collides with the old name
    # (same version re-install), append a suffix to avoid self-conflict.
    if new_exe.resolve() == exe_path.resolve():
        new_exe = app_dir / f"Count and Design v{release.version}_new.exe"

    # Skip download if the file already exists with a reasonable size.
    if new_exe.exists() and new_exe.stat().st_size >= 1024:
        logger.info("Update already downloaded: %s", new_exe)
        return str(new_exe)

    logger.info("Downloading: %s (%d bytes)", download_url, release.asset_size)

    ok, err_msg = _download(
        download_url, new_exe, release.asset_size,
        token=token,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    if not ok:
        logger.error("Download failed or was cancelled: %s", err_msg)
        if new_exe.exists():
            new_exe.unlink(missing_ok=True)
        return None

    if new_exe.stat().st_size < 1024:
        logger.error("Downloaded file is suspiciously small")
        new_exe.unlink(missing_ok=True)
        return None

    logger.info("Update downloaded to: %s", new_exe)
    return str(new_exe)


def launch_new_version(new_exe_path: str) -> None:
    """Launch *new_exe_path* detached and return immediately.

    The caller should call this just before quitting so the new process
    takes over seamlessly.
    """
    subprocess.Popen(
        [new_exe_path],
        creationflags=0x00000008,  # DETACHED_PROCESS
        close_fds=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Settings (auto-check, skip-version, and update channel)
# ═══════════════════════════════════════════════════════════════════════════════


def detect_channel() -> str:
    """Detect the appropriate update channel from the running version.

    Returns ``"stable"`` for full releases (e.g. ``1.3.3``) and
    ``"preview"`` for pre-releases (e.g. ``1.3.4.dev20260623``).

    The heuristic mirrors :func:`_normalize_version`: a version whose
    normalised tuple has more than 3 components is considered a
    pre-release.  This covers PEP 440 ``.devN`` suffixes and similar
    patterns used by this project.
    """
    v = _running_version()
    if len(_normalize_version(v)) > 3:
        return _CHANNEL_PREVIEW
    return _CHANNEL_STABLE


def get_update_channel() -> str:
    """Return the current update channel: ``"stable"`` or ``"preview"``."""
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    val = settings.value(_SETTINGS_CHANNEL, _DEFAULT_CHANNEL)
    settings.endGroup()
    if isinstance(val, str) and val in (_CHANNEL_STABLE, _CHANNEL_PREVIEW):
        return val
    return _DEFAULT_CHANNEL


def set_update_channel(channel: str) -> None:
    """Set the update channel."""
    if channel not in (_CHANNEL_STABLE, _CHANNEL_PREVIEW):
        raise ValueError(f"Invalid channel: {channel}")
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    settings.setValue(_SETTINGS_CHANNEL, channel)
    settings.endGroup()


def is_auto_check_enabled() -> bool:
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    val = settings.value(_SETTINGS_AUTO_CHECK, True)
    settings.endGroup()
    if isinstance(val, str):
        val = val.lower() == "true"
    return bool(val)


def set_auto_check_enabled(enabled: bool) -> None:
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    settings.setValue(_SETTINGS_AUTO_CHECK, enabled)
    settings.endGroup()


def get_skipped_version() -> str:
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    v = settings.value(_SETTINGS_SKIPPED_VERSION, "")
    settings.endGroup()
    return v or ""


def set_skipped_version(version: str) -> None:
    settings = QSettings()
    settings.beginGroup(_SETTINGS_GROUP)
    settings.setValue(_SETTINGS_SKIPPED_VERSION, version)
    settings.endGroup()
