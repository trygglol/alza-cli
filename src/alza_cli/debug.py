"""Self-diagnostic capture for the agent.

Every meaningful browser step writes a screenshot + page metadata to
``~/.alza-cli/debug/<run_id>/``. The CLI returns the debug dir path in
error messages and (when ``--debug`` is set) in successful JSON output,
so the agent can `cat`, `ls`, or read the screenshot path without asking
the user.

Set ``ALZA_CLI_DEBUG=1`` to enable for every run.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import ensure_home


_RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
_STEP_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def env_debug() -> bool:
    return bool(os.environ.get("ALZA_CLI_DEBUG"))


def run_id() -> str:
    return _RUN_ID


def debug_dir() -> Path:
    p = ensure_home().home / "debug" / _RUN_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Trace:
    """A list of captured steps for a single CLI invocation."""

    steps: list[dict] = field(default_factory=list)
    enabled: bool = False

    def add(self, step: dict) -> None:
        self.steps.append(step)
        # Append to last-run.log immediately so an outside reader can tail it.
        try:
            log = debug_dir() / "last-run.log"
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(step, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def summary(self) -> dict:
        return {
            "run_id": _RUN_ID,
            "dir": str(debug_dir()),
            "step_count": len(self.steps),
            "steps": self.steps,
        }


_current: Optional[Trace] = None


def start(force: bool = False) -> Trace:
    global _current
    enabled = force or env_debug()
    _current = Trace(enabled=enabled)
    if enabled:
        # Touch the dir so it exists even if no steps run.
        debug_dir()
    return _current


def current() -> Optional[Trace]:
    return _current


async def capture(page, step: str, *, force: bool = False) -> dict:
    """Snapshot the page: screenshot + url + title + console errors so far."""

    trace = _current
    enabled = force or (trace and trace.enabled)
    safe = _STEP_RE.sub("-", step).strip("-") or "step"
    ts = datetime.now().strftime("%H%M%S")
    record: dict[str, Any] = {
        "ts": ts,
        "step": step,
    }
    try:
        record["url"] = page.url
    except Exception as e:
        record["url_error"] = str(e)
    try:
        record["title"] = await asyncio.wait_for(page.title(), timeout=3)
    except Exception as e:
        record["title_error"] = str(e)
    if enabled:
        path = debug_dir() / f"{ts}-{safe}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
            record["screenshot"] = str(path)
        except Exception as e:
            record["screenshot_error"] = str(e)
    if trace:
        trace.add(record)
    return record


def attach_page_logging(page) -> None:
    """Attach console listener that writes into last-run.log.

    NOTE: We deliberately do NOT attach ``pageerror`` — Camoufox's Firefox
    driver crashes when pageError.location is undefined, which kills the
    browser process. Console messages are enough signal for debugging.
    """

    def _emit(kind: str, payload: dict) -> None:
        if _current is None:
            return
        _current.add({"ts": datetime.now().strftime("%H%M%S"), "kind": kind, **payload})

    def on_console(msg) -> None:
        try:
            if msg.type in ("error", "warning"):
                _emit("console", {"level": msg.type, "text": msg.text[:500]})
        except Exception:
            pass

    page.on("console", on_console)


def storage_state_snapshot() -> dict:
    """Read storage state and return safe summary (cookie names + flags only,
    no values). Used by `alza diagnose`."""

    paths = ensure_home()
    if not paths.storage_state.exists():
        return {"exists": False}
    try:
        data = json.loads(paths.storage_state.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "parse_error": str(e)}
    cookies = data.get("cookies", [])
    names = sorted({c.get("name", "") for c in cookies if c.get("name")})
    auth_markers = [n for n in names if any(m in n for m in (".ASPXAUTH", "ASPXFORMSAUTH", "AlzaAuth", "AlzaUser", "Alza_Nick", "AlzaCustomer", "AlzaCommerce", "ALZA_SECURITY"))]
    cf_markers = [n for n in names if n.startswith("cf_") or n.startswith("__cf") or n in ("cf_clearance",)]
    return {
        "exists": True,
        "cookie_count": len(cookies),
        "cookie_names": names,
        "auth_markers": auth_markers,
        "cf_markers": cf_markers,
        "appears_logged_in": bool(auth_markers),
        "appears_cf_cleared": bool(cf_markers),
    }
