"""Inject build-time configuration into updater.py.

Called by the CI workflow (.gitlab-ci.yml) at build time.
Reads the updater source, replaces placeholders with real values,
and writes the result back.

Usage:
    python inject_updater.py <updater_file> <project_path> <api_base> <token> [verify_ssl]

    verify_ssl: "true" (default) or "false" — intranet with self-signed
                certificates should use "false".
"""

import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 5 or len(sys.argv) > 6:
        print("Usage: python inject_updater.py <updater_file> <project_path> <api_base> <token> [verify_ssl]")
        print()
        print("This script is designed to be called from CI (.gitlab-ci.yml).")
        print("Make sure the following CI variables are defined (in YAML 'variables:' or Settings → CI/CD → Variables):")
        print("  GITLAB_API_TOKEN   — Project Access Token with api scope")
        print("  UPDATER_VERIFY_SSL — 'true' or 'false' (default: true)")
        sys.exit(1)

    updater_path = Path(sys.argv[1])
    project_path = sys.argv[2].strip()
    api_base = sys.argv[3].strip()
    token = sys.argv[4].strip()
    verify_ssl = True
    if len(sys.argv) >= 6 and sys.argv[5].strip():
        verify_ssl = sys.argv[5].strip().lower() == "true"

    if not updater_path.exists():
        print(f"ERROR: {updater_path} not found")
        sys.exit(1)

    # Validate required arguments are not empty
    missing = []
    if not project_path:
        missing.append("project_path (CI_PROJECT_PATH)")
    if not api_base:
        missing.append("api_base (CI_API_V4_URL)")
    if missing:
        print("ERROR: The following required arguments are empty:")
        for m in missing:
            print(f"  - {m}")
        print()
        print("These are GitLab CI predefined variables and should always be available.")
        print("If this persists, check your GitLab runner version.")
        sys.exit(1)

    content = updater_path.read_text(encoding="utf-8")

    # Idempotency guard — if the sentinel placeholder in the variable
    # assignment is already gone, this file was already injected
    # (e.g. CI retry on a dirty workspace).  We use a regex rather than
    # a plain substring search because the placeholder also appears in
    # comments, which are never touched by injection.
    if not re.search(
        r'\b_BUILTIN_GITLAB_TOKEN:\s*str\s*=\s*"\{\{UPDATER_GITLAB_TOKEN\}\}"',
        content,
    ):
        print("INFO: updater.py already injected — skipping.")
        sys.exit(0)

    # Replace project path (regex to tolerate whitespace / formatting drift)
    new_content, count = re.subn(
        r'\b_GITLAB_PROJECT_PATH:\s*str\s*=\s*"[^"]*"',
        f'_GITLAB_PROJECT_PATH: str = "{project_path}"',
        content,
    )
    if count != 1:
        print(f"ERROR: Expected 1 occurrence of _GITLAB_PROJECT_PATH, found {count}")
        sys.exit(1)
    content = new_content

    # Replace API base URL
    new_content, count = re.subn(
        r'\b_GITLAB_API_BASE:\s*str\s*=\s*"[^"]*"',
        f'_GITLAB_API_BASE: str = "{api_base}"',
        content,
    )
    if count != 1:
        print(f"ERROR: Expected 1 occurrence of _GITLAB_API_BASE, found {count}")
        sys.exit(1)
    content = new_content

    # Replace SSL verify flag
    new_content, count = re.subn(
        r'\b_GITLAB_VERIFY_SSL:\s*bool\s*=\s*(?:True|False)',
        f'_GITLAB_VERIFY_SSL: bool = {verify_ssl}',
        content,
    )
    if count != 1:
        print(f"ERROR: Expected 1 occurrence of _GITLAB_VERIFY_SSL, found {count}")
        sys.exit(1)
    content = new_content
    print(f"SSL verify: {verify_ssl}")

    # Inject token or clear placeholder
    if token:
        new_content, count = re.subn(r'\{\{UPDATER_GITLAB_TOKEN\}\}', token, content)
        if count == 0:
            print("ERROR: Could not find {{UPDATER_GITLAB_TOKEN}} placeholder in updater.py")
            sys.exit(1)
        print(f"Token injected ({count} occurrence(s))")
        content = new_content
    else:
        new_content, count = re.subn(
            r'\b_BUILTIN_GITLAB_TOKEN:\s*str\s*=\s*"[^"]*"',
            '_BUILTIN_GITLAB_TOKEN: str = ""',
            content,
        )
        if count != 1:
            print(f"ERROR: Expected 1 occurrence of _BUILTIN_GITLAB_TOKEN, found {count}")
            sys.exit(1)
        print("WARNING: No token provided — updater will be disabled")
        content = new_content

    updater_path.write_text(content, encoding="utf-8")
    print("Updater configured.")


if __name__ == "__main__":
    main()
