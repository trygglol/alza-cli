"""High-level actions used by the CLI commands.

All HTTP/JS goes through Camoufox (Firefox antidetect). Single browser,
single fingerprint, single profile — no curl/HTTP fallback.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from . import browser, debug as dbg, parsers
from .cache import (
    _TTL_PRODUCT,
    _TTL_SEARCH,
    get as cache_get,
    product_key,
    search_key,
    set_ as cache_set,
)
from .config import BASE_URL, ensure_home
from .errors import NeedsLogin, NeedsWarm, NotFound, ParseError
from .models import Cart, CartItem, Order, Product, ProductDetail, SearchResult, WhoAmI

_PRODUCT_URL_RE = re.compile(r"^https?://(?:www\.)?alza\.cz/.+-d(\d+)\.htm", re.IGNORECASE)


def _normalize_product_arg(arg: str) -> tuple[str, str]:
    m = _PRODUCT_URL_RE.match(arg.strip())
    if m:
        return m.group(1), arg.strip().split("?", 1)[0]
    if arg.isdigit():
        return arg, parsers.product_url_from_id(arg)
    raise ValueError(f"Cannot parse product ID from: {arg}")


_CF_MARKERS = (
    "<title>Okamžik…</title>",
    "Prosím, potvrďte, že jste z masa a kostí",
    "cf_chl_opt",
    "cf-challenge",
    "Just a moment...",
)


def _is_cf_challenge(html: str) -> bool:
    sniff = html[:4096]
    return any(m in sniff for m in _CF_MARKERS)


def _fetch(url: str, wait_for: Optional[str] = None) -> str:
    html = asyncio.run(browser.fetch_html(url, wait_for=wait_for))
    if _is_cf_challenge(html):
        raise NeedsWarm("Cloudflare challenge returned from Camoufox session.")
    return html


# ----- Search / product / compare -----


def search(query: str, limit: int = 20, use_cache: bool = True) -> SearchResult:
    key = search_key(query, limit)
    if use_cache:
        cached = cache_get(key)
        if cached:
            return SearchResult.model_validate(cached)

    url = parsers.search_url(query)
    html = _fetch(url, wait_for=".browsingitem")
    products = parsers.parse_search(html, query)[:limit]
    result = SearchResult(query=query, total=len(products), products=products)
    cache_set(key, result.model_dump(), ttl=_TTL_SEARCH)
    return result


def product(arg: str, use_cache: bool = True) -> ProductDetail:
    pid, url = _normalize_product_arg(arg)
    if use_cache:
        cached = cache_get(product_key(pid))
        if cached:
            return ProductDetail.model_validate(cached)

    html = _fetch(url, wait_for="#detailName, h1")
    detail = parsers.parse_product_detail(html, url)
    if not detail.name:
        raise ParseError(f"Could not parse product at {url}")
    cache_set(product_key(pid), detail.model_dump(), ttl=_TTL_PRODUCT)
    return detail


def compare(ids_or_urls: list[str]) -> list[ProductDetail]:
    return [product(arg) for arg in ids_or_urls]


# ----- Login / auth -----


def login(email: Optional[str] = None) -> bool:
    return asyncio.run(browser.login_interactive(email=email))


def whoami() -> WhoAmI:
    try:
        html = _fetch(f"{BASE_URL}/my-account/")
    except NeedsWarm:
        return WhoAmI(logged_in=False)
    return _parse_whoami(html)


def _parse_whoami(html: str) -> WhoAmI:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    if tree.css_first("form#loginForm") or tree.css_first("input[name='password']"):
        return WhoAmI(logged_in=False)
    name_node = (
        tree.css_first(".user-name")
        or tree.css_first(".header-user__name")
        or tree.css_first("[data-user-name]")
        or tree.css_first("h1")
    )
    email_node = tree.css_first(".user-email") or tree.css_first("[data-user-email]")
    name = name_node.text(strip=True) if name_node else None
    email = email_node.text(strip=True) if email_node else None
    if name or email:
        return WhoAmI(logged_in=True, name=name, email=email)
    return WhoAmI(logged_in=False)


# ----- Cart -----


def cart_add(product_id: str, qty: int = 1) -> Cart:
    return asyncio.run(_cart_add_async(product_id, qty))


def cart_remove(product_id: str) -> Cart:
    return asyncio.run(_cart_remove_async(product_id))


def cart_show() -> Cart:
    return asyncio.run(_cart_show_async())


async def _cart_add_async(product_id: str, qty: int) -> Cart:
    """Add a product to cart.

    Uses Alza's stable add-by-code URL: navigating to ``/Order1.htm?addCode=<code>``
    adds the item to the cart server-side. The code is the product's
    ``data-code`` attribute, which we fetch from the product page first.
    """

    _, product_url = _normalize_product_arg(product_id)
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        # Visit product page first to discover its data-code AND to look human.
        await page.goto(product_url, wait_until="domcontentloaded")
        await dbg.capture(page, "cart-add:product-loaded")
        code = await page.evaluate(
            """() => {
              const el = document.querySelector('[data-code]') || document.body;
              return el.getAttribute('data-code');
            }"""
        )
        if not code:
            # Fall back: try clicking the "Do košíku" CTA.
            for sel in ("a.or-btn[href*='addCode=']", "a.btnk1", "button.btnk1", "#btnBuy"):
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    if href and "addCode=" in href:
                        # Use the encoded URL directly.
                        target = href if href.startswith("http") else f"{BASE_URL}{href}"
                        await page.goto(target, wait_until="domcontentloaded")
                        code = "via-cta-link"
                        break
                    await el.click()
                    code = "via-button"
                    break
        if code and code not in ("via-cta-link", "via-button"):
            await page.goto(
                f"{BASE_URL}/Order1.htm?addCode={code}{('&qty=' + str(qty)) if qty > 1 else ''}",
                wait_until="domcontentloaded",
            )
        await dbg.capture(page, f"cart-add:after-add (code={code})")
        if not code:
            raise ParseError(
                f"Could not find product data-code or add-to-cart button at {product_url}. "
                f"Debug snapshot in {dbg.debug_dir()}"
            )
        # Parse cart from current page (Order1.htm IS the cart page).
        html = await page.content()
        await page.close()
    return _parse_cart(html)


async def _cart_remove_async(product_id: str) -> Cart:
    """Remove a product from the cart.

    Alza's cart row has no direct remove link. Each row carries an
    "options" dropdown trigger (``.item-options__trigger``); clicking it
    reveals an "Odstranit" button (``.item-options__option--del``). We
    locate the row by its ``[data-commodityid]`` value, open that row's
    menu, and click the delete button.
    """

    pid, _ = _normalize_product_arg(product_id)
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(f"{BASE_URL}/Order1.htm", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        # Dismiss the basket summary popover that can intercept clicks.
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
        except Exception:
            pass
        await dbg.capture(page, "cart-remove:loaded")

        removed = await page.evaluate(
            """(pid) => {
              const rows = Array.from(document.querySelectorAll('.product.tbody'));
              const row = rows.find(r => {
                const c = r.querySelector('[data-commodityid]');
                return c && c.getAttribute('data-commodityid') === String(pid);
              });
              if (!row) return { ok: false, reason: 'row-not-found' };
              const trigger = row.querySelector('.item-options__trigger, .js-item-options-trigger');
              if (!trigger) return { ok: false, reason: 'trigger-not-found' };
              trigger.click();
              return { ok: true, reason: 'trigger-clicked' };
            }""",
            pid,
        )
        await dbg.capture(page, f"cart-remove:trigger ({removed})")
        if not removed.get("ok"):
            await page.close()
            raise ParseError(
                f"Could not open options menu for product {pid}: {removed.get('reason')}. "
                f"Debug snapshot in {dbg.debug_dir()}"
            )
        await page.wait_for_timeout(1500)
        # Click the revealed "Odstranit" button. There is one delete button
        # per cart row (all but the open row's are hidden), so we must pick
        # the VISIBLE one, not just the first match.
        clicked = await page.evaluate(
            """() => {
              const dels = Array.from(document.querySelectorAll('.item-options__option--del'));
              const visible = dels.find(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              });
              if (!visible) return false;
              visible.click();
              return true;
            }"""
        )
        await page.wait_for_timeout(2500)
        await dbg.capture(page, f"cart-remove:after-delete (clicked={clicked})")
        if not clicked:
            await page.close()
            raise ParseError(
                f"Found options menu but no 'Odstranit' button for product {pid}. "
                f"Debug snapshot in {dbg.debug_dir()}"
            )
        html = await page.content()
        await page.close()
    return _parse_cart(html)


async def _cart_show_async() -> Cart:
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(f"{BASE_URL}/Order1.htm", wait_until="domcontentloaded")
        await dbg.capture(page, "cart-show:loaded")
        html = await page.content()
        await page.close()
    return _parse_cart(html)


def _parse_cart(html: str) -> Cart:
    """Parse alza.cz/Order1.htm cart page.

    Real selectors (probed live, May 2026):
    - Row container: ``.product.tbody``
    - Product ID: ``[data-commodityid]`` inside row, or extracted from
      ``a[href*='-d<id>.htm']``
    - Quantity input: ``input[data-code]`` with ``value`` attribute
    - Unit price: ``.c4`` (e.g. "109 Kč / ks")
    - Line total: ``.c5`` (e.g. "218 Kč")
    - Cart total: link/section labelled "Celkem"
    """

    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    items: list[CartItem] = []
    for row in tree.css(".product.tbody, .productContainer"):
        # Skip nested duplicates: only outermost product row.
        if row.css_first(".product.tbody") and row.attributes.get("class") == "productContainer":
            continue
        # The first a[href*='-d'] is the product image (no text). The name
        # lives in a.mainItem (or h3 a). Prefer the link that has text.
        name_link = row.css_first("a.mainItem") or row.css_first("h3 a")
        link = name_link or row.css_first("a[href*='-d']")
        url = link.attributes.get("href") if link else None
        pid_node = row.css_first("[data-commodityid]")
        pid = (pid_node.attributes.get("data-commodityid") if pid_node else "") or (
            parsers._id_from_url(url or "") or ""
        )
        name_node = name_link or row.css_first("h3") or link
        qty_input = row.css_first("input[data-code]") or row.css_first("input[type='number']")
        qty = 1
        if qty_input:
            try:
                qty = int(qty_input.attributes.get("value") or qty_input.attributes.get("data-value") or 1)
            except ValueError:
                qty = 1
        unit_price_node = row.css_first(".c4")
        line_total_node = row.css_first(".c5")
        unit_price = parsers._parse_price(unit_price_node.text(strip=True) if unit_price_node else None)
        line_total = parsers._parse_price(line_total_node.text(strip=True) if line_total_node else None)
        items.append(
            CartItem(
                id=str(pid or ""),
                name=name_node.text(strip=True) if name_node else "",
                qty=qty,
                unit_price_czk=unit_price,
                total_czk=line_total,
            )
        )

    # Find "Celkem k úhradě" / "Celkem" — broader regex through visible text.
    total: Optional[float] = None
    for node in tree.css("td, span, div, strong"):
        txt = node.text(strip=True)
        if not txt or len(txt) > 50:
            continue
        if "Celkem k úhradě" in txt or txt.strip().lower() == "celkem":
            # Read sibling / parent's price.
            parent = node.parent
            if parent:
                price = parsers._parse_price(parent.text(strip=True))
                if price:
                    total = price
                    break
    if total is None and items:
        total = sum((it.total_czk or 0) for it in items) or None
    return Cart(items=items, total_czk=total)


# ----- Orders -----


def orders(limit: int = 10) -> list[Order]:
    try:
        html = _fetch(f"{BASE_URL}/my-account/orders.htm")
    except NeedsWarm as e:
        raise NeedsLogin("Orders page requires a logged-in session.") from e
    return _parse_orders(html)[:limit]


def _parse_orders(html: str) -> list[Order]:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    if tree.css_first("input[name='password']") or tree.css_first("form#loginForm"):
        raise NeedsLogin("Orders page requires login.")
    out: list[Order] = []
    for row in tree.css(".order-row, [data-order-id], .orders-list .order"):
        oid = row.attributes.get("data-order-id") or ""
        if not oid:
            num = row.css_first(".order-number, .order-id")
            oid = num.text(strip=True) if num else ""
        date = row.css_first(".date, .order-date")
        status = row.css_first(".status, .order-status")
        total = row.css_first(".total, .order-total")
        out.append(
            Order(
                id=str(oid or "").strip() or "?",
                date=date.text(strip=True) if date else None,
                status=status.text(strip=True) if status else None,
                total_czk=parsers._parse_price(total.text(strip=True) if total else None),
            )
        )
    return out


# ----- Diagnose / warm -----


def diagnose() -> dict:
    p = ensure_home()
    storage_summary = dbg.storage_state_snapshot()
    snapshot_info: dict
    try:
        snapshot_info = asyncio.run(_diagnose_probe())
    except Exception as e:
        snapshot_info = {"probe_error": str(e)}
    return {
        "home": str(p.home),
        "debug_dir": str(dbg.debug_dir()),
        "storage_state": storage_summary,
        "probe": snapshot_info,
    }


async def _diagnose_probe() -> dict:
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(BASE_URL, wait_until="domcontentloaded")
        home_rec = await dbg.capture(page, "diagnose:home", force=True)
        # Check login state by visiting /my-account/.
        await page.goto(f"{BASE_URL}/my-account/", wait_until="domcontentloaded")
        my_account_rec = await dbg.capture(page, "diagnose:my-account", force=True)
        # If we got redirected to identity.alza.cz, that's the login flow → not logged in.
        is_identity = "identity.alza.cz" in page.url
        # Cart probe (Order1.htm always loads, shows current cart).
        await page.goto(f"{BASE_URL}/Order1.htm", wait_until="domcontentloaded")
        cart_rec = await dbg.capture(page, "diagnose:cart", force=True)
        await page.close()
    return {
        "home": home_rec,
        "my_account": my_account_rec,
        "cart": cart_rec,
        "redirected_to_identity": is_identity,
        "appears_logged_in": not is_identity and "Chyba" not in (my_account_rec.get("title") or ""),
    }


def warm() -> bool:
    ensure_home()
    return asyncio.run(browser.warm_interactive())
