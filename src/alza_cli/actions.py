"""High-level actions used by the CLI commands.

All HTTP/JS goes through Camoufox (Firefox antidetect). Single browser,
single fingerprint, single profile — no curl/HTTP fallback.
"""

from __future__ import annotations

import asyncio
import os
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
    # Authoritative signal: real auth cookies in the persisted session. DOM
    # scraping (below) only enriches the name and can veto on a login form.
    cookie_logged_in = browser.storage_has_auth()
    try:
        html = _fetch(f"{BASE_URL}/my-account/")
    except NeedsWarm:
        return WhoAmI(logged_in=cookie_logged_in)
    return _parse_whoami(html, cookie_logged_in=cookie_logged_in)


def _parse_whoami(html: str, cookie_logged_in: bool = False) -> WhoAmI:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    # A real login form means the session is dead server-side, even if stale
    # auth cookies linger on disk.
    if tree.css_first("form#loginForm") or tree.css_first("input[name='password']"):
        return WhoAmI(logged_in=False)
    name_node = (
        tree.css_first(".user-name")
        or tree.css_first(".header-user__name")
        or tree.css_first("[data-user-name]")
        or tree.css_first("[data-testid='headerContextMenuToggleAccount'] .name")
    )
    email_node = tree.css_first(".user-email") or tree.css_first("[data-user-email]")
    name = name_node.text(strip=True) if name_node else None
    email = email_node.text(strip=True) if email_node else None
    logged_in = cookie_logged_in or bool(name or email)
    return WhoAmI(logged_in=logged_in, name=name, email=email)


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
        # Wait until the item shows up in the cart so we never parse a snapshot
        # captured before the server-rendered row lands.
        pid, _ = _normalize_product_arg(product_id)
        try:
            await page.wait_for_function(
                """(pid) => {
                  const rows = Array.from(document.querySelectorAll('.product.tbody'));
                  return rows.some(r => {
                    const c = r.querySelector('[data-commodityid]');
                    return c && c.getAttribute('data-commodityid') === String(pid);
                  });
                }""",
                arg=pid,
                timeout=8000,
            )
        except Exception:
            await page.wait_for_timeout(2000)
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
        if not clicked:
            await page.close()
            raise ParseError(
                f"Found options menu but no 'Odstranit' button for product {pid}. "
                f"Debug snapshot in {dbg.debug_dir()}"
            )
        # Removal is an AJAX re-render. Wait until the row is actually gone
        # instead of a fixed sleep, so we never return a stale snapshot that
        # still lists the just-removed item.
        try:
            await page.wait_for_function(
                """(pid) => {
                  const rows = Array.from(document.querySelectorAll('.product.tbody'));
                  return !rows.some(r => {
                    const c = r.querySelector('[data-commodityid]');
                    return c && c.getAttribute('data-commodityid') === String(pid);
                  });
                }""",
                arg=pid,
                timeout=8000,
            )
        except Exception:
            # Fall back to a settle delay if the re-render signal never fires.
            await page.wait_for_timeout(2500)
        await dbg.capture(page, f"cart-remove:after-delete (clicked={clicked})")
        html = await page.content()
        await page.close()
    return _parse_cart(html)


async def _cart_show_async() -> Cart:
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(f"{BASE_URL}/Order1.htm", wait_until="domcontentloaded")
        # Let the cart finish hydrating before snapshotting.
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
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


# The orders page is a client-rendered React app with utility-class markup and
# no per-row semantic classes. The one stable anchor per order is the
# ``order-details-<id>.htm`` link; price/date are pulled from leaf elements
# inside each order's row (reading the row's full innerText concatenates the
# year + order number + price with identical whitespace, so it can't be split).
_ORDERS_EXTRACT_JS = r"""
() => {
  const priceRe=/^\d[\d\s ]*\sKč$/;
  const dateRe=/^\d{1,2}\.\s*\d{1,2}\.\s*\d{4}$/;
  const statRe=/(Uzav\w+|Vyřízen\w+|Storno\w*|Zrušen\w+|Odeslán\w*|Připravuje\w*|připravujeme\w*|Zpracov\w+|Doručen\w*|Aktivní|Ček\w+)/i;
  const seen=new Set(), out=[];
  for (const a of document.querySelectorAll('a[href*="order-details-"]')) {
    const m=(a.getAttribute('href')||'').match(/order-details-(\d+)/); if(!m) continue;
    const id=m[1]; if(seen.has(id)) continue; seen.add(id);
    let row=a;
    for(let i=0;i<7;i++){ if(!row.parentElement) break; row=row.parentElement;
      const t=row.innerText||''; if(/Kč/.test(t)&&/\d{4}/.test(t)) break; }
    let price=null,date=null;
    for(const el of row.querySelectorAll('*')){
      if(el.children.length>1) continue;
      const t=(el.innerText||'').trim();
      if(!price && priceRe.test(t)) price=t.replace(/\s+/g,' ');
      if(!date && dateRe.test(t)) date=t.replace(/\s+/g,' ');
    }
    const sm=(row.innerText||'').match(statRe);
    const num=((a.innerText||'').replace(/\s+/g,'').match(/\d{6,}/)||[])[0]||id;
    out.push({id, number:num, date, price, status: sm?sm[1]:null});
  }
  return out;
}
"""


def orders(limit: int = 10) -> list[Order]:
    rows = asyncio.run(_orders_async())
    out = [
        Order(
            id=str(r.get("number") or r.get("id") or "?"),
            date=r.get("date"),
            status=r.get("status"),
            total_czk=parsers._parse_price(r.get("price")),
        )
        for r in rows
    ]
    return out[:limit]


async def _orders_async() -> list[dict]:
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)
        await page.goto(f"{BASE_URL}/my-account/orders.htm", wait_until="domcontentloaded")
        if "identity.alza.cz" in page.url:
            await page.close()
            raise NeedsLogin("Orders page requires login.")
        if _is_cf_challenge(await page.content()):
            await page.close()
            raise NeedsWarm("Cloudflare challenge on orders page.")
        # Wait for actual order rows, not just the container — React hydrates
        # the list after the container mounts, so the container alone is not
        # enough (it yields an empty scrape).
        try:
            await page.wait_for_selector('a[href*="order-details-"]', timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        await dbg.capture(page, "orders:list")
        rows = await page.evaluate(_ORDERS_EXTRACT_JS)
        if not rows:
            # One retry in case the list is still hydrating.
            await page.wait_for_timeout(3000)
            rows = await page.evaluate(_ORDERS_EXTRACT_JS)
        await page.close()
    return rows or []


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


def checkout() -> bool:
    """Open the cart in a headed, logged-in browser for manual completion.

    The CLI deliberately stops here: it never clicks the final "Objednat"
    button. The human picks delivery + payment and confirms the order.
    """

    ensure_home()
    message = (
        "Otevřel jsem KOŠÍK v přihlášeném prohlížeči.\n"
        "Dokonči objednávku ručně:\n"
        "  1) zkontroluj položky,\n"
        "  2) vlož slevový kód (pokud máš),\n"
        "  3) zvol dopravu a platbu,\n"
        "  4) potvrď objednávku.\n"
        "Až budeš hotový, zavři okno (cmd+Q nebo X)."
    )
    return asyncio.run(browser.open_headed_at(f"{BASE_URL}/Order1.htm", message))


# ----- Automated order placement -----


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


def _dbg() -> str:
    return f"Debug snapshot in {dbg.debug_dir()}"


async def _apply_coupon(page, code: str) -> bool:
    """Best-effort: expand the discount block, type the code, submit. Whether
    it actually lowers the price is reflected in the scraped total, not here."""

    try:
        # Expand the collapsed discount block, then fill the real input.
        title = await page.query_selector("#discountContainerDiscountCode .insertItemTitle")
        if title:
            await title.click()
            await page.wait_for_timeout(700)
        inp = await page.query_selector("#txtDiscountCode")
        if not inp:
            return False
        await inp.fill(code)
        btn = await page.query_selector("#discountContainerDiscountCode .insertItemBtn")
        if btn:
            await btn.click()
        else:
            await inp.press("Enter")
        await page.wait_for_timeout(4000)
        return True
    except Exception:
        return False


async def _scrape_order3(page) -> dict:
    """Pull the human-readable order summary from the Order3 page so the caller
    can show exactly what will be (or was) submitted."""

    js = """() => {
      const pick = (sels) => {
        for (const s of sels) {
          const el = document.querySelector(s);
          if (el) { const t=(el.innerText||'').trim().replace(/\\s+/g,' '); if(t) return t; }
        }
        return null;
      };
      const byText = (needle) => {
        const els=[...document.querySelectorAll('span,div,strong,td,li,p')];
        for (const el of els){ const t=(el.innerText||'').trim(); if(t && t.includes(needle) && t.length<80) return t; }
        return null;
      };
      return {
        total: byText('K úhradě s DPH') || byText('Celkem'),
        delivery: byText('AlzaBox') || byText('Doprava'),
        payment: byText('vyzvednutí') || byText('Kartou'),
      };
    }"""
    try:
        return await page.evaluate(js)
    except Exception:
        return {}


async def _scrape_order_number(page) -> Optional[str]:
    js = """() => {
      const m = (document.body.innerText||'').match(/[čc]íslo (?:objednávky|obj\\.?)[^0-9]*([0-9]{6,})/i);
      if (m) return m[1];
      const m2 = (document.body.innerText||'').match(/objednávk[ay][^0-9]*([0-9]{8,})/i);
      return m2 ? m2[1] : null;
    }"""
    try:
        return await page.evaluate(js)
    except Exception:
        return None


def order(
    confirm: bool = False,
    coupon: Optional[str] = None,
    box: Optional[str] = None,
) -> dict:
    """Drive the full Alza checkout: AlzaBox delivery + pay-on-pickup.

    The AlzaBox is matched by ``box`` (a name substring) or, if not given, by
    the ``ALZA_CLI_BOX`` env var; with neither set the order relies on whatever
    delivery Alza has pre-selected.

    SAFETY: with ``confirm=False`` this is a DRY-RUN — it walks to the Order3
    summary and stops WITHOUT clicking "Potvrdit nákup". Only ``confirm=True``
    places the real, paid, non-refundable order.
    """

    box = box or os.environ.get("ALZA_CLI_BOX") or ""
    return asyncio.run(_order_async(confirm=confirm, coupon=coupon, box=box))


async def _order_async(confirm: bool, coupon: Optional[str], box: str) -> dict:
    ensure_home()
    result: dict = {"placed": False, "steps": []}
    async with browser.open_context(headless=True) as context:
        page = await context.new_page()
        dbg.attach_page_logging(page)

        # Step 1 — cart.
        await page.goto(f"{BASE_URL}/Order1.htm", wait_until="domcontentloaded")
        await _settle(page)
        await dbg.capture(page, "order:01-cart")
        if not await page.query_selector("[data-commodityid], .product.tbody"):
            raise ParseError("Cart appears empty — nothing to order. " + _dbg())
        if coupon:
            applied = await _apply_coupon(page, coupon)
            result["steps"].append(f"coupon {coupon}: {'submitted' if applied else 'field-not-found'}")
        cont = await page.query_selector("a.js-button-order-continue")
        if not cont:
            raise ParseError("Cart 'Pokračovat' button not found. " + _dbg())
        await cont.click()
        await page.wait_for_url("**/Order2.htm", timeout=15000)
        await _settle(page)
        result["steps"].append("reached Order2 (Doprava a platba)")

        # Step 2 — delivery + payment.
        await dbg.capture(page, "order:02-order2-loaded")
        pay = page.get_by_text("Kartou při vyzvednutí", exact=False)
        # Payment options only render once a delivery is chosen. Select the
        # AlzaBox ONLY if payment isn't already showing — Alza remembers the
        # last delivery and pre-selects it, and these rows behave like
        # checkboxes, so re-clicking a selected one would toggle it OFF.
        if await pay.count() == 0:
            if not box:
                raise ParseError(
                    "No delivery is pre-selected and no box given. Pass --box "
                    "<AlzaBox name> or set ALZA_CLI_BOX. " + _dbg()
                )
            try:
                await page.get_by_text(box, exact=False).first.click(timeout=8000)
            except Exception:
                raise ParseError(f"Delivery option matching '{box}' not found. " + _dbg())
            await page.wait_for_timeout(3500)
        try:
            await pay.first.wait_for(state="visible", timeout=8000)
        except Exception:
            raise ParseError("Payment 'Kartou při vyzvednutí' not available after selecting delivery. " + _dbg())
        await pay.first.click(timeout=8000)
        await page.wait_for_timeout(2500)
        await dbg.capture(page, "order:02-delivery-payment")
        btn2 = await page.query_selector("#confirmOrder2Button")
        cls2 = (await btn2.get_attribute("class")) if btn2 else None
        if not btn2 or "disabled" in (cls2 or ""):
            raise ParseError("Order2 'Pokračovat' still disabled — delivery/payment not accepted. " + _dbg())
        await btn2.click()
        # Tolerant transition: the Order2→Order3 nav can take a while.
        try:
            await page.wait_for_url("**/Order3.htm", timeout=20000)
        except Exception:
            await page.wait_for_timeout(4000)
        await _settle(page)
        if "Order3" not in page.url and not await page.query_selector("a.js-order3-continue"):
            raise ParseError("Did not reach Order3 (Dodací údaje) after 'Pokračovat'. " + _dbg())
        await dbg.capture(page, "order:03-summary")
        result["steps"].append("reached Order3 (Dodací údaje)")
        result["summary"] = await _scrape_order3(page)

        # Final gate.
        if not confirm:
            result["note"] = "DRY-RUN: order NOT placed. Re-run with confirm=True to place."
            await page.close()
            return result

        final = await page.query_selector("a.js-order3-continue")
        if not final:
            raise ParseError("Final 'Potvrdit nákup' button not found. " + _dbg())
        await final.click()
        try:
            await page.wait_for_url("**/Order4**", timeout=20000)
        except Exception:
            await page.wait_for_timeout(8000)
        await _settle(page)
        await dbg.capture(page, "order:04-confirmation")
        result["placed"] = True
        result["confirmation_url"] = page.url
        order_no = await _scrape_order_number(page)
        if not order_no:
            # Fall back to the order id embedded in the confirmation URL,
            # e.g. .../order-details-<id>.htm
            m = re.search(r"order-details-(\d+)", page.url)
            if m:
                order_no = m.group(1)
        result["order_number"] = order_no
        await page.close()
    return result
