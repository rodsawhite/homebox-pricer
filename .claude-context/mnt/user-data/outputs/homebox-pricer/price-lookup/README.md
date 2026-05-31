# price-lookup

The pricing sidecar for [Homebox Pricer](../README.md). It scans Homebox for items missing a purchase price, looks up current **AUD** retail pricing, and presents candidates in a **review queue** for human approval. Nothing is written to Homebox until you approve it.

Built with FastAPI + SQLite, Python 3.12, managed with `uv`. Runs as one container.

---

## How it works

Every `CHECK_INTERVAL` seconds (default hourly) the sidecar:

1. Pulls all items from Homebox and keeps the ones with no `purchasePrice`.
2. Builds a search query from the item's name, manufacturer, and model number.
3. Searches DuckDuckGo (region `au-en`) and feeds the result snippets to the local model (`qwen2.5vl:3b` via Ollama — the same vision model used for capture, here used text-only) that extracts a price as structured JSON.
4. Stores each result as a **pending candidate** in its own SQLite DB.
5. Waits for you to Approve / Reject / Edit in the review queue at `:8090`.
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
| `PRICE_TEXT_MODEL` | `qwen2.5vl:3b` | Model used to extract prices (same one used for capture) |
| `PRICE_REGION` | `au-en` | DuckDuckGo region |
| `PRICE_CURRENCY` | `AUD` | Expected currency |
| `PRICE_MIN_CONFIDENCE` | `low` | Hide candidates below this in the queue (`low`/`medium`/`high`) |
| `CHECK_INTERVAL` | `3600` | Seconds between sweeps |
| `SERVER_PORT` | `8090` | Port for the review queue + API |
| `DB_PATH` | `/data/price.db` | SQLite file location |

> Provide **either** `HOMEBOX_TOKEN` (simple, expires ~monthly) **or** `HOMEBOX_USER` + `HOMEBOX_PASSWORD` (auto-refresh). If both are set, credentials win and the token is treated as a warm start.

---

## Running

As part of the stack (recommended): see the root README. Standalone, for development:

```bash
cd price-lookup
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8090
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
curl -X POST http://localhost:8090/api/lookup \
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
