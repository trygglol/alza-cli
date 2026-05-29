"""Runtime configuration: paths, throttle, user agent.

All personal data lives under ALZA_CLI_HOME (default ``~/.alza-cli``).
Nothing inside the repo. ``.env`` files are loaded from CWD if present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_THROTTLE_SECONDS = 2.5


def home() -> Path:
    raw = os.environ.get("ALZA_CLI_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".alza-cli"


@dataclass(frozen=True)
class Paths:
    home: Path
    storage_state: Path
    browser_profile: Path
    cache_db: Path
    config_toml: Path


def paths() -> Paths:
    h = home()
    return Paths(
        home=h,
        storage_state=h / "storage_state.json",
        browser_profile=h / "browser-profile",
        cache_db=h / "cache.db",
        config_toml=h / "config.toml",
    )


def ensure_home() -> Paths:
    p = paths()
    p.home.mkdir(parents=True, exist_ok=True)
    p.browser_profile.mkdir(parents=True, exist_ok=True)
    return p


def throttle_seconds() -> float:
    raw = os.environ.get("ALZA_CLI_THROTTLE")
    if not raw:
        return DEFAULT_THROTTLE_SECONDS
    try:
        return max(0.5, float(raw))
    except ValueError:
        return DEFAULT_THROTTLE_SECONDS


def user_agent() -> str:
    return os.environ.get("ALZA_CLI_USER_AGENT") or DEFAULT_USER_AGENT


BASE_URL = "https://www.alza.cz"
