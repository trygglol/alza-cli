"""HTML parsers for alza.cz pages.

Selectors are defensive: each field tries multiple candidates and JSON-LD
microdata as a fallback. Alza's markup changes; if a field stops working,
add a new candidate selector or extend the JSON-LD path.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from .config import BASE_URL
from .models import Product, ProductDetail, Review

_PRICE_RE = re.compile(r"([\d\s.,]+)\s*Kč", re.IGNORECASE)
_INT_RE = re.compile(r"\d+")
_PRODUCT_ID_FROM_URL = re.compile(r"-d(\d+)\.htm")


def _text(node: Optional[Node]) -> Optional[str]:
    if node is None:
        return None
    txt = node.text(strip=True)
    return txt or None


def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        m = re.search(r"[\d\s.,]+", text)
        if not m:
            return None
    raw = m.group(1) if m.lastindex else m.group(0)
    raw = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL, href)


def _id_from_url(url: str) -> Optional[str]:
    m = _PRODUCT_ID_FROM_URL.search(url)
    return m.group(1) if m else None


def _first_attr(node: Node, *names: str) -> Optional[str]:
    for n in names:
        v = node.attributes.get(n)
        if v:
            return v
    return None


def _select_first(root: HTMLParser | Node, selectors: Iterable[str]) -> Optional[Node]:
    for sel in selectors:
        node = root.css_first(sel)
        if node is not None:
            return node
    return None


def parse_search(html: str, query: str) -> list[Product]:
    """Parse alza.cz/search.htm?exps=<query> result page into Product list."""

    tree = HTMLParser(html)
    products: list[Product] = []
    cards = tree.css(".browsingitem")
    if not cards:
        # JSON-LD ItemList fallback
        for ld in _iter_jsonld(tree):
            if ld.get("@type") in ("ItemList", "SearchResultsPage"):
                for item in ld.get("itemListElement", []):
                    p = _product_from_jsonld_item(item)
                    if p:
                        products.append(p)
        return products

    for card in cards:
        product_id = card.attributes.get("data-id") or ""
        if not product_id:
            continue
        name_node = card.css_first(".name") or card.css_first("a.name")
        link_node = card.css_first("a.name") or card.css_first("a[href*='-d']")
        if not link_node:
            continue
        href = link_node.attributes.get("href") or ""
        url = _abs_url(href.split("?", 1)[0])  # strip ?o=N ordering query
        if not url:
            continue
        name = _text(name_node) or _text(link_node) or ""
        # Price text often has trailing noise ("Do košíku", "Novinka", "Od X měsíčně").
        # Take the first "<digits[ \xa0,]>+,-" token.
        price_node = card.css_first(".price")
        price_raw = price_node.text(separator=" ", strip=True) if price_node else None
        price_text, price_czk = _extract_first_price(price_raw)

        # Availability from card class flags.
        cls = card.attributes.get("class") or ""
        available: Optional[bool]
        availability_text: Optional[str]
        if "cannotBuy" in cls or "isSoldOut" in cls or "isOutOfStock" in cls:
            available = False
            availability_text = "není skladem"
        elif "canBuy" in cls:
            available = True
            availability_text = "skladem"
        else:
            available = None
            availability_text = None

        img = card.css_first("img")
        image_url = None
        if img:
            image_url = img.attributes.get("data-src") or img.attributes.get("src")
            if image_url and not image_url.startswith("http"):
                image_url = _abs_url(image_url)

        short_desc_node = card.css_first(".sc") or card.css_first(".typeinformation")
        products.append(
            Product(
                id=str(product_id),
                name=name,
                url=url,
                price_czk=price_czk,
                price_text=price_text,
                availability_text=availability_text,
                available=available,
                image_url=image_url,
                short_description=_text(short_desc_node),
            )
        )
    return products


# Alza price format: "15 990,-" / "299,-" / "od 1 071,-" / "1 234,50 Kč"
_FIRST_PRICE_RE = re.compile(r"(\d[\d\xa0\s]*(?:,\d+)?)\s*(?:,-|Kč|kč|CZK)")


def _extract_first_price(text: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    if not text:
        return None, None
    m = _FIRST_PRICE_RE.search(text)
    if not m:
        return None, None
    display = m.group(0).strip()
    raw = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return display, float(raw)
    except ValueError:
        return display, None


def parse_product_detail(html: str, url: str) -> ProductDetail:
    tree = HTMLParser(html)
    ld = _first_product_jsonld(tree)

    name = None
    description = None
    price_czk: Optional[float] = None
    price_text: Optional[str] = None
    available: Optional[bool] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    image_url: Optional[str] = None
    if ld:
        name = ld.get("name")
        description = ld.get("description")
        offers = ld.get("offers") or {}
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            try:
                price_czk = float(offers.get("price")) if offers.get("price") else None
            except (TypeError, ValueError):
                price_czk = None
            availability_text = offers.get("availability")
            if isinstance(availability_text, str):
                available = "InStock" in availability_text
        agg = ld.get("aggregateRating") or {}
        if isinstance(agg, dict):
            try:
                rating = float(agg.get("ratingValue")) if agg.get("ratingValue") else None
                review_count = int(agg.get("reviewCount") or agg.get("ratingCount") or 0) or None
            except (TypeError, ValueError):
                pass
        image = ld.get("image")
        if isinstance(image, list) and image:
            image_url = image[0]
        elif isinstance(image, str):
            image_url = image

    # HTML fallbacks
    if not name:
        name = _text(_select_first(tree, ["h1#detailName", "h1.name", "h1"]))
    if price_czk is None:
        price_node = _select_first(tree, ["#prices .price-box__price", ".price-box__price", ".bigPrice", ".bigPrice .price"])
        price_text = _text(price_node)
        price_czk = _parse_price(price_text)
    if not image_url:
        img = tree.css_first("#imageContainer img") or tree.css_first("img.main-product-image") or tree.css_first("meta[property='og:image']")
        if img:
            image_url = img.attributes.get("content") or img.attributes.get("src")
    if not description:
        description = _text(_select_first(tree, ["#detailText", "#popis", ".product-description", ".detailText"]))

    specs = _parse_specs(tree)
    reviews = _parse_reviews(tree)
    product_id = _id_from_url(url) or _first_attr(tree.body or tree.root, "data-product-id", "data-id") or ""

    return ProductDetail(
        id=str(product_id),
        name=name or "",
        url=url,
        price_czk=price_czk,
        price_text=price_text,
        available=available,
        availability_text=None,
        rating=rating,
        review_count=review_count,
        image_url=image_url,
        description=description,
        specs=specs,
        top_reviews=reviews,
    )


def _parse_specs(tree: HTMLParser) -> dict[str, str]:
    specs: dict[str, str] = {}
    # Common spec table containers.
    for table_sel in ("table.params-table", ".params-table", "#parametricky table", ".parameters table"):
        table = tree.css_first(table_sel)
        if not table:
            continue
        for row in table.css("tr"):
            cells = row.css("th, td")
            if len(cells) >= 2:
                k = _text(cells[0]) or ""
                v = _text(cells[1]) or ""
                if k and v and k not in specs:
                    specs[k] = v
        if specs:
            return specs
    # Definition list fallback.
    for dl in tree.css("dl"):
        keys = dl.css("dt")
        values = dl.css("dd")
        for k, v in zip(keys, values):
            kt = _text(k) or ""
            vt = _text(v) or ""
            if kt and vt and kt not in specs:
                specs[kt] = vt
        if specs:
            return specs
    return specs


def _parse_reviews(tree: HTMLParser) -> list[Review]:
    reviews: list[Review] = []
    # Embedded JSON-LD reviews.
    for ld in _iter_jsonld(tree):
        for r in _collect_reviews_from_jsonld(ld):
            reviews.append(r)
            if len(reviews) >= 10:
                return reviews
    if reviews:
        return reviews
    # HTML fallback
    for node in tree.css(".comment, .review, [data-comment-id]")[:10]:
        author = _text(_select_first(node, [".author", ".user", ".name"]))
        body = _text(_select_first(node, [".text", ".body", ".comment-text"]))
        title = _text(_select_first(node, [".title", "h3", "strong"]))
        rating_node = _select_first(node, ["[data-rating]", ".star-rating"])
        rating_attr = _first_attr(rating_node, "data-rating") if rating_node else None
        rating = float(rating_attr) if rating_attr and _is_number(rating_attr) else None
        date = _text(_select_first(node, [".date", "time"]))
        reviews.append(Review(author=author, rating=rating, title=title, body=body, date=date))
    return reviews


def _iter_jsonld(tree: HTMLParser):
    for s in tree.css("script[type='application/ld+json']"):
        raw = s.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            yield from data
        else:
            yield data


def _first_product_jsonld(tree: HTMLParser) -> Optional[dict]:
    for ld in _iter_jsonld(tree):
        t = ld.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return ld
    return None


def _product_from_jsonld_item(item: dict) -> Optional[Product]:
    inner = item.get("item") if isinstance(item, dict) else None
    if not isinstance(inner, dict):
        return None
    url = inner.get("url") or ""
    pid = _id_from_url(url) or str(inner.get("sku") or inner.get("productID") or "")
    if not pid:
        return None
    name = inner.get("name") or ""
    offers = inner.get("offers") or {}
    price = None
    if isinstance(offers, dict):
        try:
            price = float(offers.get("price")) if offers.get("price") else None
        except (TypeError, ValueError):
            price = None
    return Product(id=str(pid), name=name, url=url, price_czk=price)


def _collect_reviews_from_jsonld(ld: dict):
    if not isinstance(ld, dict):
        return
    raw_reviews = ld.get("review") or []
    if isinstance(raw_reviews, dict):
        raw_reviews = [raw_reviews]
    for r in raw_reviews[:10]:
        if not isinstance(r, dict):
            continue
        author = r.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        rating = None
        rr = r.get("reviewRating") or {}
        if isinstance(rr, dict) and rr.get("ratingValue"):
            try:
                rating = float(rr.get("ratingValue"))
            except (TypeError, ValueError):
                rating = None
        yield Review(
            author=author if isinstance(author, str) else None,
            rating=rating,
            title=r.get("headline"),
            body=r.get("reviewBody"),
            date=r.get("datePublished"),
        )


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _avail_to_bool(text: Optional[str]) -> Optional[bool]:
    if not text:
        return None
    t = text.lower()
    if "skladem" in t and "není" not in t:
        return True
    if "není skladem" in t or "vyprodáno" in t or "nedostupné" in t:
        return False
    if "objednání" in t or "dodání" in t:
        return None
    return None


def search_url(query: str) -> str:
    from urllib.parse import urlencode

    return f"{BASE_URL}/search.htm?{urlencode({'exps': query})}"


def product_url_from_id(product_id: str) -> str:
    return f"{BASE_URL}/-d{product_id}.htm"
