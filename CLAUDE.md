# homebox-pricer — Project Memory

Pricing sidecar for Homebox: scans items lacking a price, searches the web,
extracts a price with a local Ollama model, and queues candidates for review.
Approving a candidate writes `purchasePrice` back to Homebox.

## Reference sources (use in this order)

When working on anything that touches the **Homebox API**, consult these
*before* guessing — most bugs here have been payload/schema mismatches:

1. **Official Homebox API docs — https://homebox.software/en/api/**
   The source of truth for endpoints and request/response schemas
   (`ItemUpdate`, `ItemOut`, login, etc.).
   - The site is Cloudflare-protected and returns **403 to automated
     fetchers** (WebFetch/curl). To read the schema programmatically, pull the
     generated swagger spec from the Homebox source instead, e.g. the
     `swagger.json` under `backend/app/api/static/docs/` (or
     `internal/data/repo/repo_items.go` for the raw Go structs). Use GitHub
     code search / raw.githubusercontent for these.
   - Key structs live in `repo_items.go`: `ItemUpdate` (PUT body) vs
     `ItemOut` (GET response). They are **not** the same shape.

2. **Homebox Companion codebase — https://github.com/Duelion/homebox-companion**
   A mature, working Homebox client. Treat it as "what good looks like" and a
   fallback when the docs are ambiguous. Most relevant file:
   `src/homebox_companion/homebox/client.py` (login, get_item, update_item,
   token normalization). Mirror its behaviour when in doubt.
   - Note: the GitHub MCP in this environment is scoped to
     `rodsawhite/homebox-pricer` only, so `get_file_contents` on Companion is
     denied. Use **`search_code`** (works cross-repo) or
     `raw.githubusercontent.com/Duelion/homebox-companion/main/...` via
     WebFetch to read its files.

## Homebox integration gotchas (resolved)

These were all real bugs hit while wiring up auth + price writeback. Keep them
in mind before changing `price-lookup/app/homebox.py`:

- **Login must send `stayLoggedIn: True`.** Without it Homebox issues a
  short-lived session token that 401s on the very next API call
  (login 200 → immediate 401). Companion sends this; we now do too.
- **Strip the `Bearer ` prefix from tokens.** Homebox v0.22.0+ returns the
  login token with `Bearer ` already prepended; re-adding it yields
  `Authorization: Bearer Bearer <token>` → 401. Strip on both the login
  response and the static `HOMEBOX_TOKEN` env var.
- **PUT uses the `ItemUpdate` schema, not the GET response.** `GET /items/{id}`
  returns `ItemOut` with read-only fields and nested objects; feeding it back
  to PUT causes `500 {"error":"Unknown Error"}`. Build a clean payload
  (`_build_put_payload`):
  - nested edges → flat IDs: `location` → `locationId`, `labels` →
    `labelIds`, `parent` → `parentId`
  - drop read-only fields entirely: `attachments`, timestamps, etc.
  - empty UUID/date fields must be **`null`, not `""`** — an empty string
    fails Homebox's UUID/date parse and surfaces as the same generic 500.
    (`parentId`, `purchasePrice`, `soldPrice` are `x-nullable` per the spec;
    `locationId` is required.)
- **PUT not PATCH** for writes — PATCH has been seen to silently drop custom
  fields on some Homebox versions.
- A generic **`500 {"error":"Unknown Error"}`** is Homebox's *sanitized*
  client response. The real Go error is in the **Homebox container logs**
  (`docker logs <homebox>`), and validation errors come back as **422**, not
  500 — so a 500 means a parse/DB error, usually a malformed field value.

### Debugging a PUT 500 (current open path)

`put_item` logs the exact outgoing JSON body and Homebox's response at ERROR
level on any non-2xx. To diagnose:
1. Reproduce the approve, then read `docker compose logs price-lookup` for the
   `ERROR ... PUT ... failed: 500 ... — sent body: {...}` line.
2. Diff that `sent body` field-by-field against `ItemUpdate` in the swagger
   spec.
3. Cross-check the same operation in Companion's `update_item` if a field's
   expected shape is unclear.
4. Check the Homebox container logs for the underlying Go error.

Leading remaining suspects for the open 500: the `fields` (custom fields)
array shape, or a date returned by GET as RFC3339 but expected by PUT as
`YYYY-MM-DD`.

## Dev workflow

- Package manager: **`uv`**. Run tests: `cd price-lookup && uv run --extra dev pytest -q`.
- Tests mock all external I/O (httpx, DDGS, sleep) — no network/Homebox needed.
- Branch for development: `claude/hopeful-fermi-6kseE`; changes are merged to
  `main` with `--no-ff` and pushed.
- Local stack verification (needs Docker + real `.env`): `./scripts/verify-local.sh`.
- Web search uses the **`ddgs`** package (the old `duckduckgo_search` was
  renamed and now returns empty results — do not revert to it).

## Security constraints (must always hold)

- Secrets (Homebox token, any API keys) live only in `.env` (gitignored).
  **Never** commit secrets, embed keys in compose files, or echo them into
  committed code.
- This compose owns only the pricing sidecar (`price-lookup` + `ollama` +
  `ollama-init`). Homebox Companion and Homebox itself run separately and are
  **not** managed here.
