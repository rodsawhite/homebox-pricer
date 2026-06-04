# price-lookup

The pricing sidecar for [Homebox Pricer](../README.md). It scans Homebox for items missing a purchase price, looks up current **AUD** retail pricing, and presents candidates in a **review queue** for human approval. Nothing is written to Homebox until you approve it.

Built with FastAPI + SQLite, Python 3.12, managed with `uv`. Runs as one container.

---

## How it works

Every `CHECK_INTERVAL` seconds (default hourly) the sidecar:

1. Pulls all items from Homebox and keeps the ones with no `purchasePrice`.
2. Builds a search query from the item's name, manufacturer, and model number.
3. Searches DuckDuckGo (region `au-en`) and feeds the result snippets to a lean local text model (`qwen2.5:3b` via Ollama) that extracts a price as structured JSON. (Capture/vision runs separately on Claude Haiku 4.5 in the cloud; this sidecar only does text.)
4. Stores each result as a **pending candidate** in its own SQLite DB.
5. Waits for you to Approve / Reject / Edit in the review queue at `:8091`.
6. On approval, reads the full item from Homebox, sets the price, and writes it back with **PUT** (the full object — PATCH is unreliable on Homebox items).

It does **not** touch the capture flow — that's Homebox Companion's job.

---

## Configuration

Read from environment variables (see the root [`.env.example`](../.env.example)).

| Variable | Default | Description |
| --- | --- | --- |
| `HOMEBOX_URL` | – | Homebox base URL, e.g. `http://172.16.0.125:3900` |
| `HOMEBOX_TOKEN` | – | Static bearer token (use this **or** the credential pair below) |
| `HOMEBOX_USER` | – | Login email, for automatic token refresh |
| `HOMEBOX_PASSWORD` | – | Login password, for automatic token refresh |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama endpoint for price parsing |
| `PRICE_TEXT_MODEL` | `qwen2.5:3b` | Local Ollama text model used to extract prices |
| `PRICE_REGION` | `au-en` | DuckDuckGo region |
| `PRICE_CURRENCY` | `AUD` | Expected currency |
| `PRICE_MIN_CONFIDENCE` | `low` | Hide queue candidates below this confidence (`low`/`medium`/`high`); sub-threshold candidates are still stored |
| `CHECK_INTERVAL` | `3600` | Seconds between sweeps |
| `SERVER_PORT` | `8091` | Port for the review queue + API |
| `DB_PATH` | `/data/price.db` | SQLite file location |
| `LOG_FORMAT` | `plain` | `plain` for human-readable logs, `json` for one JSON object per line |

> Provide **either** `HOMEBOX_TOKEN` (simple, expires ~monthly) **or** `HOMEBOX_USER` + `HOMEBOX_PASSWORD` (auto-refresh). If both are set, credentials win and the token is treated as a warm start.

---

## Running

As part of the stack (recommended): see the root README. Standalone, for development:

```bash
cd price-lookup
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8091
```

Persist the SQLite DB by mounting a volume at `/data`.

---

## API reference

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| GET | `/` | – | Review queue (HTML) |
| GET | `/health` | – | Liveness probe |
| GET | `/status` | – | Last sweep time + counts |
| GET | `/api/candidates?status=pending` | – | List candidates as JSON |
| POST | `/api/candidates/{id}/approve` | – | Apply price to Homebox, mark `applied` |
| POST | `/api/candidates/{id}/reject` | – | Mark `rejected` |
| POST | `/api/candidates/{id}` | `{price, source_url}` | Edit before approving |
| POST | `/api/lookup` | `{query}` | Ad-hoc lookup, returns a price guess (no write) |
| POST | `/api/sweep` | – | Run a sweep now instead of waiting |

### Example: ad-hoc lookup

```bash
curl -X POST http://localhost:8091/api/lookup \
  -H "Content-Type: application/json" \
  -d '{"query": "Sony WH-1000XM5"}'
```

```json
{
  "query": "Sony WH-1000XM5",
  "price": 549.00,
  "currency": "AUD",
  "source": "https://www.example.com.au/...",
  "confidence": "high",
  "reason": "Listed price A$549 on an .com.au retailer matching the model."
}
```

---

## Review queue UI

The page at `:8091/` is the human-in-the-loop review queue. Each pending
candidate is a card showing:

- a Homebox attachment thumbnail (or a 📦 placeholder if none),
- the item name and the search query that was used,
- the looked-up price + currency and a colour-coded **confidence** badge
  (high / medium / low),
- the model's one-line reason and the source URL,
- **Approve** / **Reject** buttons, plus an inline **edit** row to correct the
  price or source before approving.

A top bar shows the last sweep time, pending/applied/rejected counts, and a
**Sweep now** trigger.

> Screenshots: _to be added._ The UI is a single self-contained Jinja2 template
> (`app/templates/queue.html`) with no JS build step, so it renders identically
> anywhere; a captured screenshot can be dropped in here later.

### Confidence filter (and the escape hatch)

By default the queue (and `GET /api/candidates`) hides candidates whose
confidence is below `PRICE_MIN_CONFIDENCE` (order: `low` < `medium` < `high`).
Nothing is lost — sub-threshold candidates are still written to the DB. To see
**everything**, including the hidden ones:

- Web: open `/?include_all=true` (the bar shows how many are hidden and links to it).
- API: `GET /api/candidates?status=pending&include_all=true`.

---

## Resilience

The sidecar is built to ride out the flaky bits of its dependencies without
operator intervention:

- **Transient failures are retried with backoff.** Connection errors, timeouts,
  and `5xx` responses from Homebox — and rate-limit blips from DuckDuckGo — are
  retried up to 3 times with exponential backoff (1s → 2s → 4s). Hard `4xx`
  responses are *not* retried.
- **Token expiry pauses, never crashes.** If credentials (`HOMEBOX_USER` +
  `HOMEBOX_PASSWORD`) are set, an expired token is refreshed automatically and
  the request retried. If only a static `HOMEBOX_TOKEN` is configured, a `401`
  mid-sweep logs a clear warning and **pauses that sweep**; the service and the
  review queue stay up and the next interval retries (just drop in a fresh
  token). Either way the scheduler loop keeps running.
- **A failed sweep is non-fatal.** An unreachable Homebox or a search/Ollama
  failure is logged and skipped; the queue stays available and the next sweep
  tries again.

---

## Price extraction prompt (shape)

The text model is asked for strict JSON and nothing else:

```
You are given web search results for a product. Determine the current
NEW retail price in Australian dollars (AUD).

Rules:
- Prefer prices from .com.au domains or results that explicitly say AUD / A$.
- A bare "$" without an Australian source is ambiguous → confidence "low".
- If no clear price, return price: null.

Return ONLY this JSON:
{"price": number|null, "currency": "AUD", "source": "<url>",
 "confidence": "high"|"medium"|"low", "reason": "<short>"}

Product: {query}
Results:
{numbered titles + snippets + urls}
```

Output is parsed defensively (strip code fences, tolerate stray text) before being stored.

---

## Design notes

- **Read-modify-write with PUT.** Homebox PATCH can drop fields silently, so approval always fetches the full item, mutates `purchasePrice`, and PUTs it back.
- **One open candidate per item.** A partial unique index keeps sweeps from stacking duplicates.
- **The sidecar never blocks.** A failed search, an unreachable Homebox, or an expired token pauses the relevant step and retries next sweep; the service and the review queue stay available.
- **AUD-first parsing.** Currency ambiguity is treated as a signal, not noise — anything unclear is surfaced for a human rather than guessed.
