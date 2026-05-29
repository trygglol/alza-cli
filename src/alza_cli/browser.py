"""Camoufox (Firefox antidetect) browser session management.

All operations — warm-up, login, search, product, cart — go through
Camoufox. Firefox with browserforge fingerprints + geoip + macOS profile,
which Alza's bot detection has not been flagged by in Vali's existing tests.

``warm()`` opens a headed browser so the user can pass any initial Cloudflare
challenge. ``login_interactive()`` does the same for sign-in and auto-detects
auth cookies. ``fetch_html()`` runs headless against the persistent profile.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Sequence

from . import debug as dbg, patch_playwright as _patch
from .config import BASE_URL, ensure_home

# Apply Playwright driver patch at first browser import so every entry path
# (CLI, library use, tests) benefits without an explicit setup step.
_patch.ensure_patched()

from camoufox.async_api import AsyncCamoufox  # noqa: E402  (after patch)


_DEFAULT_WINDOW = (1280, 900)


@asynccontextmanager
async def open_context(headless: bool = True) -> AsyncIterator:
    """Open a Camoufox persistent context.

    Yields a Playwright BrowserContext bound to the on-disk profile under
    ``~/.alza-cli/browser-profile/`` so cookies and clearance persist.
    """

    paths = ensure_home()
    async with AsyncCamoufox(
        headless=headless,
        humanize=True,
        geoip=True,
        locale=["cs-CZ", "en-US"],
        os=["macos"],
        window=_DEFAULT_WINDOW,
        persistent_context=True,
        user_data_dir=str(paths.browser_profile),
    ) as context:
        try:
            yield context
        finally:
            try:
                await context.storage_state(path=str(paths.storage_state))
            except Exception:
                pass


_AUTH_COOKIE_HINTS = (
    ".ASPXAUTH",
    "ASPXFORMSAUTH",
    "AlzaAuth",
    "AlzaUser",
    "Alza_Nick",
    "AlzaCustomer",
    "AlzaCommerce",
    "ALZA_SECURITY",
    "ALZALOGGED",
    "ALZACOMPANY",
    "AlzaCustomerToken",
    "AspNetCore.Identity.Application",
    ".AspNetCore.Cookies",
)


async def warm_interactive() -> bool:
    """Open headed browser on alza.cz home. Wait until user closes window.

    Camoufox's anti-detect handles most Cloudflare challenges automatically;
    the human-in-the-loop is just a safety net.
    """

    paths = ensure_home()
    async with open_context(headless=False) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(BASE_URL, wait_until="domcontentloaded")
        await dbg.capture(page, "warm:home-loaded")
        print(
            "Otevřel jsem alza.cz v Camoufoxu. Pokud se objeví Cloudflare\n"
            "challenge, klikni na checkbox. Pak okno zavři (cmd+Q nebo X).",
            flush=True,
        )
        await _wait_until_user_closes(context)
    return paths.storage_state.exists()


async def login_interactive(email: Optional[str] = None) -> bool:
    """Open headed login via Alza OIDC identity flow.

    Navigates to alza.cz, clicks the header "Přihlásit se" button, and lets
    the user finish on identity.alza.cz. Auto-closes when an auth cookie
    appears.
    """

    paths = ensure_home()
    async with open_context(headless=False) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(BASE_URL, wait_until="domcontentloaded")
        await dbg.capture(page, "login:home")
        try:
            await page.click('[data-testid="headerContextMenuToggleLogin"]', timeout=8000)
        except Exception as e:
            print(f"Nepovedlo se kliknout na 'Přihlásit se' tlačítko: {e}", flush=True)
            await dbg.capture(page, "login:button-not-found")
        # Wait for navigation to identity domain.
        try:
            await page.wait_for_url("**/identity.alza.cz/**", timeout=10000)
        except Exception:
            pass
        await dbg.capture(page, "login:identity-opened")
        if email:
            for sel in (
                "input[type='email']",
                "input[name='Email']",
                "input[name='email']",
                "#Email",
                "#email",
                "input[autocomplete='username']",
            ):
                try:
                    await page.fill(sel, email, timeout=2500)
                    break
                except Exception:
                    continue
        print(
            "Otevřel jsem Alza login (identity.alza.cz) v Camoufoxu.\n"
            "Vyplň heslo a přihlas se. Browser se sám zavře, jakmile\n"
            "detekuju auth cookie. Pokud Alza čeká na potvrzení e-mailu nebo\n"
            "2FA, dokonči to taky.",
            flush=True,
        )
        ok = await _wait_for_login(context, timeout=600)
        if ok:
            await dbg.capture(page, "login:detected")
            await context.storage_state(path=str(paths.storage_state))
        else:
            await _wait_until_user_closes(context)
    if not paths.storage_state.exists():
        return False
    text = paths.storage_state.read_text(encoding="utf-8")
    return any(h in text for h in _AUTH_COOKIE_HINTS)


async def _wait_for_login(context, timeout: int = 600) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            cookies = await context.cookies()
        except Exception:
            return False
        names = {c.get("name", "") for c in cookies}
        if any(any(h in n for h in _AUTH_COOKIE_HINTS) for n in names):
            return True
        try:
            if not context.pages:
                return False
        except Exception:
            return False
        await asyncio.sleep(1.5)
    return False


async def _wait_until_user_closes(context) -> None:
    closed = asyncio.Event()

    def on_close(_):
        closed.set()

    try:
        context.on("close", on_close)
    except Exception:
        pass
    try:
        for page in context.pages:
            page.on("close", on_close)

        def on_new_page(page):
            page.on("close", on_close)

        context.on("page", on_new_page)
    except Exception:
        pass

    try:
        while not closed.is_set():
            try:
                await asyncio.wait_for(closed.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
            try:
                pages = context.pages
            except Exception:
                break
            if not pages:
                break
    except KeyboardInterrupt:
        pass


async def fetch_html(url: str, wait_for: Optional[str] = None) -> str:
    """Headless fetch reusing the persistent Camoufox profile."""

    async with open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(url, wait_until="domcontentloaded")
        await dbg.capture(page, f"fetch:{url}")
        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=8000)
            except Exception:
                await dbg.capture(page, f"wait-for-failed:{wait_for}")
        html = await page.content()
        await page.close()
        return html


async def fetch_many_html(urls: Sequence[str], wait_for: Optional[str] = None) -> list[str]:
    out: list[str] = []
    async with open_context(headless=True) as context:
        for url in urls:
            page = await context.new_page()
            dbg.attach_page_logging(page)
            try:
                await page.goto(url, wait_until="domcontentloaded")
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=8000)
                    except Exception:
                        pass
                out.append(await page.content())
            finally:
                await page.close()
            await asyncio.sleep(2.5)
    return out
