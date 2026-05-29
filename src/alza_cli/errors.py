"""Typed errors used by the CLI to give actionable messages."""


class AlzaCliError(Exception):
    """Base error. Carries a hint that the CLI prints to the user."""

    hint: str = ""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        if hint:
            self.hint = hint


class NeedsWarm(AlzaCliError):
    hint = "Run `alza warm` first to pass the Cloudflare challenge."


class NeedsLogin(AlzaCliError):
    hint = "Run `alza login` to sign in."


class RateLimited(AlzaCliError):
    hint = "Alza returned 429/503. Wait a few minutes before retrying."


class ParseError(AlzaCliError):
    hint = "The HTML structure changed. Open an issue with the URL."


class NotFound(AlzaCliError):
    hint = "Product or page not found."
