# alza-cli

Personal CLI for [alza.cz](https://www.alza.cz/) — search, product detail,
compare, login, cart, orders. JSON-first output, optional rich tables.

> **Disclaimer.** Personal tool for individual research. Not affiliated with
> Alza. Respect alza.cz Terms of Service and use a reasonable request rate.
> Do **not** redistribute scraped product data.

## Features

- `alza warm` — interactive Cloudflare warm-up (persistent profile)
- `alza search <query>` — search results as JSON
- `alza product <id-or-url>` — full product detail (price, specs, reviews)
- `alza compare <id1> <id2> …` — side-by-side comparison
- `alza login` — interactive sign-in, session is saved locally
- `alza whoami` — check current auth state
- `alza cart add / remove / show` — cart operations
- `alza orders` — recent orders history

All commands print JSON to stdout by default. Add `--pretty` for a `rich`
table. Errors go to stderr with non-zero exit codes.

## Architecture

`alza-cli` uses a hybrid scraping stack to deal with Alza's Cloudflare
Turnstile gating:

- **[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)** —
  stealth Playwright fork. Used for warm-up, login, and JS-driven cart
  actions. A persistent browser profile keeps Cloudflare clearance alive.
- **[curl_cffi](https://github.com/lexiforest/curl_cffi)** — Chrome 124 TLS
  fingerprint impersonation. Used for fast read-only queries (search,
  product detail) after the browser has cleared the challenge.
- **selectolax** — fast HTML parser. JSON-LD microdata first, CSS selector
  fallback.
- **diskcache** — SQLite-backed cache (search 24h, product 1h).
- **typer + rich** — CLI and tabular output.

## Install

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/) (or pipx).

```bash
git clone https://github.com/trygglol/alza-cli.git
cd alza-cli
uv tool install .
patchright install chromium
alza --version
```

`uv tool install .` exposes the `alza` command globally (linked into
`~/.local/bin/`). `patchright install chromium` downloads the stealth-patched
Chromium build.

### One-time warm-up

Alza serves a Cloudflare Turnstile challenge on first visit. Run:

```bash
alza warm
```

A browser window opens at `alza.cz`. Click the Cloudflare checkbox if it
appears, wait for the page to load normally, then close the window. The
session is persisted to `~/.alza-cli/storage_state.json` (gitignored).

## Usage

```bash
# Search
alza search "iphone 16" --limit 10
alza search "iphone 16" --pretty

# Product detail
alza product 18852809
alza product https://www.alza.cz/iphone/18852809.htm --pretty

# Compare
alza compare 18852809 18852810 18852811

# Login (interactive)
alza login --email you@example.com
alza whoami --pretty

# Cart
alza cart add 18852809 --qty 1
alza cart show --pretty
alza cart remove 18852809

# Orders
alza orders --limit 5 --pretty

# Skip cache for a fresh fetch
alza product 18852809 --no-cache
```

### JSON output

All commands without `--pretty` print machine-readable JSON to stdout. Use
`jq` to slice it:

```bash
alza search "iphone 16" --limit 5 | jq '.products[] | {id, name, price_czk}'
```

## Configuration

`alza-cli` keeps all personal state under `~/.alza-cli/`:

| Path | What |
|---|---|
| `~/.alza-cli/storage_state.json` | Cookies, localStorage, Cloudflare clearance |
| `~/.alza-cli/browser-profile/` | Patchright persistent profile |
| `~/.alza-cli/cache.db` | diskcache SQLite |
| `~/.alza-cli/config.toml` | (optional) user config |

Environment variables:

| Var | Default | What |
|---|---|---|
| `ALZA_CLI_HOME` | `~/.alza-cli` | Override state directory |
| `ALZA_CLI_THROTTLE` | `2.5` | Seconds between HTTP requests |
| `ALZA_CLI_USER_AGENT` | Chrome 130 / macOS | UA override |

A `.env` file in the current directory is loaded automatically.

## Anti-bot etiquette

- Default throttle is **1 request / 2.5s** with ±500 ms jitter. Don't tune it
  below 1s — Alza's edge will rate-limit and trigger an hour-long IP cooldown.
- On `429 / 503`, the CLI raises `RateLimited`. Back off **at least 5 min**
  before retrying. There is no automatic retry loop on purpose.
- Run on your home connection. Datacenter proxies are flagged immediately.
- Don't build a sniper / availability bot on top of this. Alza actively
  detects burst patterns.

## Privacy & open-source split

| Public (in this repo) | Private (NEVER committed) |
|---|---|
| Python source code | `~/.alza-cli/storage_state.json` |
| `pyproject.toml`, dependencies | `~/.alza-cli/browser-profile/` |
| README, docs, examples | `~/.alza-cli/config.toml` |
| `.env.example` (empty) | `.env` (real values) |
| Tests against offline HTML fixtures | Cache databases |

`.gitignore` blocks the personal patterns inside the repo as a safety net.

## Development

```bash
git clone https://github.com/trygglol/alza-cli.git
cd alza-cli
uv sync
uv run alza --version
```

Reinstall the global binary after changes:

```bash
uv tool install . --reinstall
```

## Troubleshooting

- **"No storage state yet"** → run `alza warm`.
- **"Cloudflare challenge returned"** → run `alza warm` again; Cloudflare
  expired your clearance.
- **`alza cart` / `alza orders` returns login form** → run `alza login`.
- **HTML parser returns empty results** → Alza changed markup. Open an issue
  with the URL and a snippet; selectors live in `src/alza_cli/parsers.py`.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [Kaliiiiiiiiii-Vinyzu/patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
- [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi)
- [rushter/selectolax](https://github.com/rushter/selectolax)
