# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pricing **sidecar** for [Homebox](https://homebox.software). A background
sweep finds Homebox items with no `purchasePrice`, searches the web (DuckDuckGo
via `ddgs`), extracts a price with a local Ollama text model, and queues
candidates for human review. Approving a candidate writes `purchasePrice` back
to Homebox.

Everything lives under `price-lookup/` (a FastAPI app). Homebox itself and
[Homebox Companion](https://github.com/Duelion/homebox-companion) (photo
capture) run **separately** and are not managed by this repo's compose file.

## Commands

All Python work happens inside `price-lookup/` with **`uv`**:

```bash
cd price-lookup
uv run --extra dev pytest -q                       # full test suite
uv run --extra dev pytest tests/test_pricing.py -q # one file
uv run --extra dev pytest -q -k coerce_confidence  # one test by name
uv run uvicorn app.main:app --reload --port 8091   # run the app locally
```

Stack-level (needs Docker + a real `.env`):

```bash
docker compose build price-lookup
docker compose up -d price-lookup        # also starts ollama + ollama-init
./scripts/verify-local.sh                # compose config + tests + build + health probe
docker compose logs price-lookup         # runtime logs (PUT failures log here at ERROR)
```

Tests mock all external I/O (httpx, DDGS, sleep) — no network, Homebox, or
Ollama required to run them.

## Architecture

Request/data flow, by module under `price-lookup/app/`:

- **`main.py`** — FastAPI app + all routes. A `lifespan` hook calls `init_db()`
  and spawns the `scheduler_loop()` asyncio task. Serves both a Jinja2 HTML
  review queue (`/`) and a JSON API (`/api/...`). Approve/reject/sweep
  endpoints **content-negotiate** via `_wants_html()`: browser form POSTs get a
  303 redirect with a flash message (PRG pattern); JSON clients get JSON.
- **`scheduler.py`** — `run_sweep()` is the core loop: `list_items()` →
  filter to items with no price and not already pending → build a search query
  from `manufacturer + name + modelNumber` → `lookup_price()` → `upsert_candidate()`.
  `scheduler_loop()` waits one `check_interval` before the first sweep (so
  `/health` passes early) then repeats. Distinguishes `HomeboxAuthError`
  (pause gracefully, service stays up) from `HomeboxError` (abort sweep).
- **`pricing.py`** — `lookup_price()` never raises; returns a null-price result
  on any failure. DDG search (`ddgs`) with bounded exponential-backoff retry →
  format snippets → Ollama `/api/generate` → `_parse_model_output()` tolerates
  code fences / stray text. `_coerce_confidence()` downgrades to `low` when the
  source shows no Australian signal (`.com.au`, `aud`, `a$`, `.au/`).
- **`homebox.py`** — Homebox API client. Supports static token **or**
  user/password auto-refresh. `_retry_request()` retries only transient
  failures (transport errors, 5xx); 4xx including the 401-refresh handshake is
  handled explicitly. `apply_price()` does read-modify-write via
  `_build_put_payload()` (see Homebox gotchas below). **See the dedicated
  section below before changing this file.**
- **`db.py`** — SQLite (WAL mode, `sqlite3.Row`, parameterized queries only).
  A **partial unique index** `ux_pending_item ON price_candidates(homebox_id)
  WHERE status='pending'` lets `upsert_candidate()` use `INSERT OR IGNORE` to
  avoid duplicate pending rows for the same item. Status lifecycle:
  `pending → applied | rejected`.
- **`config.py`** — pydantic-settings from env / `.env`. Only Homebox URL +
  auth are needed to boot; everything else defaults. `get_settings()` returns a
  fresh instance each call (intentional, for easy test override).
- **`logging_config.py`** — `LOG_FORMAT=plain|json` toggle.

Compose services: `price-lookup`, `ollama`, and `ollama-init` (one-shot that
pulls `PRICE_TEXT_MODEL` into the shared volume before price-lookup starts).

## Homebox API: references (consult before guessing)

Most bugs here have been Homebox payload/schema mismatches. Use these in order:

1. **Official API docs — https://homebox.software/en/api/** (source of truth for
   `ItemUpdate`, `ItemOut`, login, etc.). The site is Cloudflare-protected and
   **403s automated fetchers** (WebFetch/curl). To read the schema
   programmatically, pull it from the Homebox **source** instead: the swagger
   `swagger.json` under `backend/app/api/static/docs/`, or the raw Go structs in
   `internal/data/repo/repo_items.go` (`ItemUpdate` = PUT body, `ItemOut` = GET
   response — **not** the same shape). Use GitHub `search_code` or
   `raw.githubusercontent.com`.
2. **Homebox Companion — https://github.com/Duelion/homebox-companion** — a
   mature working client; treat as "what good looks like" and a fallback when
   docs are ambiguous. Key file: `src/homebox_companion/homebox/client.py`
   (login, get_item, update_item, token normalization). The GitHub MCP here is
   scoped to `rodsawhite/homebox-pricer`, so `get_file_contents` on Companion is
   denied — use **`search_code`** (works cross-repo) or raw URLs via WebFetch.

## Homebox integration gotchas (before editing `homebox.py`)

- **Login must send `stayLoggedIn: True`** — without it Homebox issues a
  short-lived session token that 401s on the very next call (login 200 →
  immediate 401).
- **Strip the `Bearer ` prefix from tokens** — Homebox v0.22.0+ returns login
  tokens with `Bearer ` already prepended; re-adding it yields
  `Authorization: Bearer Bearer <token>` → 401. Strip on both the login
  response and the static `HOMEBOX_TOKEN`.
- **PUT uses the `ItemUpdate` schema, not the GET `ItemOut` response.** Feeding
  the GET response back to PUT causes `500 {"error":"Unknown Error"}`.
  `_build_put_payload()` projects it: nested edges → flat IDs (`location`→
  `locationId`, `labels`→`labelIds`, `parent`→`parentId`), drops read-only
  fields (`attachments`, timestamps), and sends **`null` not `""`** for empty
  UUID/date fields (an empty string fails the parse → same generic 500).
  `parentId`/`purchasePrice`/`soldPrice` are `x-nullable`; `locationId` is required.
- **PUT not PATCH** — PATCH has silently dropped custom fields on some versions.
- A generic **`500 {"error":"Unknown Error"}`** is Homebox's *sanitized* client
  response; the real Go error is in the **Homebox container logs**
  (`docker logs <homebox>`). Validation errors come back as **422**, so a 500
  means a parse/DB error — usually a malformed field value.

### Debugging a PUT 500 (current open issue)

Approving a candidate still 500s. `put_item` logs the exact outgoing JSON body
and Homebox's response at ERROR level on any non-2xx. To diagnose:
1. Reproduce the approve, then find the `ERROR ... PUT ... failed: 500 ... —
   sent body: {...}` line in `docker compose logs price-lookup`.
2. Diff that `sent body` field-by-field against `ItemUpdate` in the swagger spec.
3. Cross-check the same operation in Companion's `update_item`.
4. Check the Homebox container logs for the underlying Go error.

Leading suspects: the `fields` (custom fields) array shape, or a date GET
returns as RFC3339 but PUT expects as `YYYY-MM-DD`.

## Conventions

- Web search uses the **`ddgs`** package — do **not** revert to
  `duckduckgo_search` (renamed; now returns empty results).
- Dev branch: `claude/hopeful-fermi-6kseE`; merge to `main` with `--no-ff`.
- This compose owns only `price-lookup` + `ollama` + `ollama-init`. Homebox and
  Companion run separately and are not bundled here.
