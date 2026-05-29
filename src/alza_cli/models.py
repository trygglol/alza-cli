"""Typed result models. Pydantic so JSON output is round-trippable."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Product(BaseModel):
    """Product summary as it appears in search listings or detail pages."""

    id: str
    name: str
    url: str
    price_czk: Optional[float] = None
    price_text: Optional[str] = None
    available: Optional[bool] = None
    availability_text: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    image_url: Optional[str] = None
    short_description: Optional[str] = None


class SearchResult(BaseModel):
    query: str
    total: Optional[int] = None
    products: list[Product] = Field(default_factory=list)


class Review(BaseModel):
    author: Optional[str] = None
    rating: Optional[float] = None
    title: Optional[str] = None
    body: Optional[str] = None
    date: Optional[str] = None


class ProductDetail(Product):
    description: Optional[str] = None
    # Discount fields. All None when the product is sold at its plain price.
    # ``price_czk`` stays the headline/regular price; ``sale_price_czk`` is the
    # final price after the discount (e.g. a coupon applied in the cart).
    sale_price_czk: Optional[float] = None
    discount_pct: Optional[int] = None
    coupon_code: Optional[str] = None
    specs: dict[str, str] = Field(default_factory=dict)
    top_reviews: list[Review] = Field(default_factory=list)


class CartItem(BaseModel):
    id: str
    name: str
    qty: int
    unit_price_czk: Optional[float] = None
    total_czk: Optional[float] = None


class Cart(BaseModel):
    items: list[CartItem] = Field(default_factory=list)
    total_czk: Optional[float] = None


class Order(BaseModel):
    id: str
    date: Optional[str] = None
    status: Optional[str] = None
    total_czk: Optional[float] = None
    items: list[CartItem] = Field(default_factory=list)


class WhoAmI(BaseModel):
    logged_in: bool
    name: Optional[str] = None
    email: Optional[str] = None
