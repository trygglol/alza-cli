"""JSON vs rich rendering. JSON is the default; --pretty switches to rich."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from .models import Cart, Order, Product, ProductDetail, SearchResult, WhoAmI

_console = Console()


def emit_json(payload: BaseModel | list[BaseModel] | dict | list) -> None:
    if isinstance(payload, BaseModel):
        data: Any = payload.model_dump(exclude_none=True)
    elif isinstance(payload, list) and payload and isinstance(payload[0], BaseModel):
        data = [item.model_dump(exclude_none=True) for item in payload]
    else:
        data = payload
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def pretty_search(result: SearchResult) -> None:
    table = Table(title=f"Hledání: {result.query}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("ID", style="cyan")
    table.add_column("Název")
    table.add_column("Cena", justify="right", style="green")
    table.add_column("Hodnocení", justify="right")
    table.add_column("Dostupnost")
    for idx, p in enumerate(result.products, start=1):
        table.add_row(
            str(idx),
            p.id,
            (p.name or "")[:60],
            p.price_text or (f"{p.price_czk:.0f} Kč" if p.price_czk else "-"),
            f"{p.rating:.1f} ({p.review_count})" if p.rating else "-",
            p.availability_text or ("✓" if p.available else ("✗" if p.available is False else "-")),
        )
    _console.print(table)


def pretty_product(detail: ProductDetail) -> None:
    _console.rule(f"[bold]{detail.name}[/bold]")
    _console.print(f"[cyan]ID:[/cyan] {detail.id}")
    _console.print(f"[cyan]URL:[/cyan] {detail.url}")
    if detail.price_text:
        _console.print(f"[green]Cena:[/green] {detail.price_text}")
    elif detail.price_czk:
        _console.print(f"[green]Cena:[/green] {detail.price_czk:.0f} Kč")
    if detail.availability_text:
        _console.print(f"Dostupnost: {detail.availability_text}")
    if detail.rating:
        _console.print(f"Hodnocení: {detail.rating:.1f} ({detail.review_count})")
    if detail.description:
        _console.print("\n[bold]Popis[/bold]")
        _console.print(detail.description)
    if detail.specs:
        _console.print("\n[bold]Parametry[/bold]")
        spec_table = Table(show_header=False)
        spec_table.add_column("Klíč", style="cyan")
        spec_table.add_column("Hodnota")
        for k, v in list(detail.specs.items())[:30]:
            spec_table.add_row(k, v)
        _console.print(spec_table)
    if detail.top_reviews:
        _console.print("\n[bold]Top recenze[/bold]")
        for r in detail.top_reviews[:5]:
            head = []
            if r.author:
                head.append(r.author)
            if r.rating:
                head.append(f"★ {r.rating:.0f}")
            if r.date:
                head.append(r.date)
            _console.print(f"[dim]{' · '.join(head)}[/dim]")
            if r.title:
                _console.print(f"[bold]{r.title}[/bold]")
            if r.body:
                _console.print(r.body)
            _console.print()


def pretty_compare(products: list[ProductDetail]) -> None:
    if not products:
        _console.print("[yellow]Žádné produkty k porovnání.[/yellow]")
        return
    table = Table(title="Porovnání produktů")
    table.add_column("Atribut", style="cyan")
    for p in products:
        table.add_column(p.name[:30], overflow="fold")
    rows = [
        ("ID", [p.id for p in products]),
        ("Cena", [p.price_text or (f"{p.price_czk:.0f} Kč" if p.price_czk else "-") for p in products]),
        ("Dostupnost", [p.availability_text or "-" for p in products]),
        ("Hodnocení", [f"{p.rating:.1f}" if p.rating else "-" for p in products]),
        ("Recenze", [str(p.review_count) if p.review_count else "-" for p in products]),
        ("URL", [p.url for p in products]),
    ]
    for label, values in rows:
        table.add_row(label, *values)

    # Spec union
    all_keys = sorted({k for p in products for k in p.specs.keys()})
    for k in all_keys:
        table.add_row(k, *[p.specs.get(k, "-") for p in products])
    _console.print(table)


def pretty_cart(cart: Cart) -> None:
    table = Table(title="Košík")
    table.add_column("ID", style="cyan")
    table.add_column("Název")
    table.add_column("Ks", justify="right")
    table.add_column("Cena/ks", justify="right")
    table.add_column("Celkem", justify="right", style="green")
    for it in cart.items:
        table.add_row(
            it.id,
            (it.name or "")[:60],
            str(it.qty),
            f"{it.unit_price_czk:.0f} Kč" if it.unit_price_czk else "-",
            f"{it.total_czk:.0f} Kč" if it.total_czk else "-",
        )
    _console.print(table)
    if cart.total_czk:
        _console.print(f"\n[bold green]Celkem: {cart.total_czk:.0f} Kč[/bold green]")


def pretty_orders(orders: list[Order]) -> None:
    table = Table(title="Objednávky")
    table.add_column("ID", style="cyan")
    table.add_column("Datum")
    table.add_column("Status")
    table.add_column("Celkem", justify="right", style="green")
    for o in orders:
        table.add_row(
            o.id,
            o.date or "-",
            o.status or "-",
            f"{o.total_czk:.0f} Kč" if o.total_czk else "-",
        )
    _console.print(table)


def pretty_whoami(w: WhoAmI) -> None:
    if not w.logged_in:
        _console.print("[yellow]Nepřihlášený.[/yellow] Spusť `alza login`.")
        return
    parts = [w.name or "?", f"<{w.email}>" if w.email else ""]
    _console.print(f"[green]Přihlášen:[/green] {' '.join(p for p in parts if p)}")


def emit_error(message: str, hint: str = "") -> None:
    _console.print(f"[red]Chyba:[/red] {message}", file=sys.stderr) if False else None
    print(f"Chyba: {message}", file=sys.stderr)
    if hint:
        print(f"Tip: {hint}", file=sys.stderr)
