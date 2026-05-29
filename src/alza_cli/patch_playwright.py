"""Auto-patch Playwright's Node driver to survive Firefox pageError edge cases.

Playwright 1.55–1.60 crashes the Node driver process when Firefox emits a
``pageerror`` whose ``location`` field is undefined. The driver tries to
read ``pageError.location.url`` and throws, killing the whole browser
session.

This module finds ``coreBundle.js`` inside whichever Playwright is currently
imported, and applies a one-line replacement that defaults missing values to
``""`` / ``0`` instead of throwing. Idempotent — runs at most once per
install via a marker file.
"""

from __future__ import annotations

from pathlib import Path

_REPLACEMENTS = [
    (
        "url: pageError.location.url,",
        'url: (pageError.location && pageError.location.url) || "",',
    ),
    (
        "lineNumber: pageError.location.lineNumber,",
        "lineNumber: (pageError.location && pageError.location.lineNumber) || 0,",
    ),
    (
        "columnNumber: pageError.location.columnNumber,",
        "columnNumber: (pageError.location && pageError.location.columnNumber) || 0,",
    ),
    (
        "line: pageError.location.lineNumber,",
        "line: (pageError.location && pageError.location.lineNumber) || 0,",
    ),
    (
        "column: pageError.location.columnNumber,",
        "column: (pageError.location && pageError.location.columnNumber) || 0,",
    ),
    (
        "column: pageError.location.columnNumber",
        "column: (pageError.location && pageError.location.columnNumber) || 0",
    ),
]


def _core_bundle_path() -> Path | None:
    try:
        import playwright  # type: ignore
    except ImportError:
        return None
    pkg_root = Path(playwright.__file__).resolve().parent
    candidate = pkg_root / "driver" / "package" / "lib" / "coreBundle.js"
    return candidate if candidate.exists() else None


def ensure_patched() -> bool:
    """Apply patch if file still contains the unsafe pattern.

    Checks the file content directly — no marker file. Survives Playwright
    reinstalls because we always reapply when the raw pattern reappears.
    """

    core = _core_bundle_path()
    if not core:
        return False
    try:
        text = core.read_text(encoding="utf-8")
    except OSError:
        return False
    # If none of the unsafe patterns remain, already patched (or fixed upstream).
    needs_patch = any(old in text for old, _ in _REPLACEMENTS)
    if not needs_patch:
        return True
    for old, new in _REPLACEMENTS:
        if old in text:
            text = text.replace(old, new)
    try:
        core.write_text(text, encoding="utf-8")
        return True
    except (OSError, PermissionError):
        return False
