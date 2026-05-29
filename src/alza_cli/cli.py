"""Typer CLI app — main entry point.

Run via ``alza <command>`` after ``uv tool install .``.
"""

from __future__ import annotations

import sys
from typing import Optional

import os

import typer

from . import __version__, actions, debug as dbg, output
from .errors import AlzaCliError


app = typer.Typer(
    name="alza",
    help="Personal CLI for alza.cz — research, compare, login, cart.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"alza-cli {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Enable diagnostic screenshots and verbose tracing into ~/.alza-cli/debug/<run-id>/.",
    ),
) -> None:
    """alza-cli root."""

    if debug:
        os.environ["ALZA_CLI_DEBUG"] = "1"
    dbg.start(force=debug)


def _handle(action_name: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AlzaCliError as e:
        output.emit_error(str(e), hint=e.hint)
        raise typer.Exit(2)
    except Exception as e:  # noqa: BLE001
        output.emit_error(f"{action_name} failed: {e}")
        raise typer.Exit(1)


@app.command()
def warm() -> None:
    """Open a headed browser so you can pass the Cloudflare challenge.

    The browser uses a persistent profile, so cookies + clearance carry over
    to subsequent commands. Close the browser window when alza.cz is loaded
    normally.
    """

    ok = _handle("warm", actions.warm)
    if ok:
        typer.echo("OK. Storage state uložen v ~/.alza-cli/storage_state.json")
    else:
        typer.echo("Storage state se neuložil. Zkus to znovu.", err=True)
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query, e.g. 'iphone 16'"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results."),
    pretty: bool = typer.Option(False, "--pretty", help="Render a rich table instead of JSON."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass disk cache."),
) -> None:
    """Search alza.cz."""

    result = _handle("search", actions.search, query, limit=limit, use_cache=not no_cache)
    if pretty:
        output.pretty_search(result)
    else:
        output.emit_json(result)


@app.command()
def product(
    arg: str = typer.Argument(..., help="Product ID (e.g. 18852809) or full URL."),
    pretty: bool = typer.Option(False, "--pretty", help="Render rich layout."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass disk cache."),
) -> None:
    """Fetch product detail: price, description, specs, reviews."""

    detail = _handle("product", actions.product, arg, use_cache=not no_cache)
    if pretty:
        output.pretty_product(detail)
    else:
        output.emit_json(detail)


@app.command()
def compare(
    ids: list[str] = typer.Argument(..., help="Product IDs or URLs (2+)."),
    pretty: bool = typer.Option(True, "--pretty/--json", help="Default to rich table; --json for JSON."),
) -> None:
    """Compare 2+ products side by side."""

    details = _handle("compare", actions.compare, ids)
    if pretty:
        output.pretty_compare(details)
    else:
        output.emit_json(details)


@app.command()
def login(
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Pre-fill e-mail."),
) -> None:
    """Open browser to alza.cz login. Sign in manually; session is saved."""

    ok = _handle("login", actions.login, email=email)
    if ok:
        typer.echo("Přihlášení uloženo.")
    else:
        typer.echo("Nepovedlo se ověřit přihlášení. Zkus znovu.", err=True)
        raise typer.Exit(1)


@app.command()
def whoami(
    pretty: bool = typer.Option(False, "--pretty", help="Human-readable line."),
) -> None:
    """Show whether you are currently logged in."""

    info = _handle("whoami", actions.whoami)
    if pretty:
        output.pretty_whoami(info)
    else:
        output.emit_json(info)


cart_app = typer.Typer(name="cart", help="Cart operations (requires login).", no_args_is_help=True)
app.add_typer(cart_app, name="cart")


@cart_app.command("add")
def cart_add_cmd(
    product_id: str = typer.Argument(..., help="Product ID or URL."),
    qty: int = typer.Option(1, "--qty", "-q", help="Quantity."),
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """Add a product to the cart."""

    cart = _handle("cart add", actions.cart_add, product_id, qty=qty)
    if pretty:
        output.pretty_cart(cart)
    else:
        output.emit_json(cart)


@cart_app.command("remove")
def cart_remove_cmd(
    product_id: str = typer.Argument(..., help="Product ID."),
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """Remove a product from the cart."""

    cart = _handle("cart remove", actions.cart_remove, product_id)
    if pretty:
        output.pretty_cart(cart)
    else:
        output.emit_json(cart)


@cart_app.command("show")
def cart_show_cmd(
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """Show current cart contents."""

    cart = _handle("cart show", actions.cart_show)
    if pretty:
        output.pretty_cart(cart)
    else:
        output.emit_json(cart)


@app.command()
def orders(
    limit: int = typer.Option(10, "--limit", "-n"),
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """List recent orders."""

    items = _handle("orders", actions.orders, limit=limit)
    if pretty:
        output.pretty_orders(items)
    else:
        output.emit_json(items)


@app.command()
def diagnose() -> None:
    """Self-diagnostic: probe alza.cz + cart, return state snapshot.

    Forces ``--debug`` to capture screenshots. Useful when commands fail
    and you want to see what the headless browser actually sees.
    """

    os.environ["ALZA_CLI_DEBUG"] = "1"
    dbg.start(force=True)
    report = _handle("diagnose", actions.diagnose)
    output.emit_json(report)


def run() -> None:
    """Console-script entry."""

    app()


if __name__ == "__main__":
    run()
