# Architecture

## Overview

Three containers plus an external Homebox instance. Only one container (`price-lookup`) is original to this repo; the other two are upstream images wired together.

```
                         ┌─────────────────────────────────────────┐
                         │              Docker (WSL2)               │
                         │                                          │
   iPhone browser        │   ┌──────────────────┐                   │
       │                 │   │ homebox-companion│                   │
       ├── :8000 ────────┼──▶│  capture + AI    │──┐                │
       │  (capture)      │   └──────────────────┘  │                │
       │                 │            │            │ creates items  │
       │                 │            ▼            │                │
       │                 │   ┌──────────────────┐  │                │
       │                 │   │     ollama       │  │                │
       │                 │   │  qwen2.5vl:3b    │  │                │
       │                 │   │  (vision + text) │  │                │
       │                 │   └──────────────────┘  │                │
       │                 │            ▲            │                │
       │                 │            │ price parse│                │
       │                 │   ┌──────────────────┐  │                │
       ├── :8090 ────────┼──▶│  price-lookup    │  │                │
          (review queue) │   │  scan/search/    │  │                │
                         │   │  queue/review    │  │                │
                         │   └──────────────────┘  │                │
                         │            │            │                │
                         └────────────┼────────────┼────────────────┘
                                      │            │
                                      ▼            ▼
                              ┌─────────────────────────┐
                              │   Homebox (on NAS)       │
                              │   172.16.0.125:3900      │
                              │   /api/v1                │
                              └─────────────────────────┘
```

---

## Data flow

### Capture path (handled entirely by upstream Companion)

1. User selects a Homebox location, then photographs items.
2. Companion sends images to Ollama (`qwen2.5vl:3b`) for identification.
3. User reviews/edits the AI's suggestions.
4. Companion creates items in Homebox via the API, attaching photos.

The capture path needs no code from us — it works out of the box once Companion is pointed at Ollama and Homebox.

### Pricing path (the sidecar — what we build)

```
            ┌────────── every CHECK_INTERVAL seconds ──────────┐
            ▼                                                   │
1. GET /api/v1/items  ─────────────────────────────────────────┘
   (paginate, fetch all)
            │
            ▼
2. Filter to items where purchasePrice is 0 / null
   AND not already in the local queue
            │
            ▼
3. For each candidate, build a query:
   "{manufacturer} {name} {modelNumber} price"  (region au-en)
            │
            ▼
4. Search via duckduckgo_search (DDGS().text(...))
            │
            ▼
5. Feed result titles + snippets to Ollama (`qwen2.5vl:3b`, the same model, used
   text-only here):
   "Extract the current AUD new retail price. Return JSON:
    {price: number|null, currency: string, source: string,
     confidence: 'high'|'medium'|'low', reason: string}"
            │
            ▼
6. Store candidate in local SQLite queue (status = 'pending')
            │
            ▼
7. Human opens :8090 review queue → Approve / Reject / Edit
            │
   approve  ▼
8. Read item from Homebox → merge price → PUT full object back
   (PUT, not PATCH — see "Homebox API quirks")
   Mark queue row 'applied'.
```

---

## The sidecar in detail

### Why it owns state

Because prices go to a **review queue**, the sidecar needs somewhere to hold candidates between discovery and approval. A single SQLite file is enough. Homebox stays the source of truth for inventory; the sidecar's DB only holds pending/decided price candidates and a small audit trail.

### SQLite schema (proposed)

```sql
CREATE TABLE price_candidates (
    id            INTEGER PRIMARY KEY,
    homebox_id    TEXT NOT NULL,
    item_name     TEXT NOT NULL,
    query         TEXT NOT NULL,
    price         REAL,
    currency      TEXT DEFAULT 'AUD',
    source_url    TEXT,
    confidence    TEXT,            -- high | medium | low
    reason        TEXT,            -- model's justification
    status        TEXT DEFAULT 'pending',  -- pending | applied | rejected
    created_at    TEXT,
    decided_at    TEXT
);

CREATE UNIQUE INDEX ux_pending_item
    ON price_candidates(homebox_id)
    WHERE status = 'pending';     -- one open candidate per item
```

### Module layout

```
price-lookup/
├── Dockerfile
├── pyproject.toml          # uv-managed, Python 3.12
├── app/
│   ├── main.py             # FastAPI app + background scheduler
│   ├── config.py           # env-var settings (pydantic-settings)
│   ├── db.py               # SQLite access (sqlite3 or aiosqlite)
│   ├── homebox.py          # Homebox client: login, list, get, put
│   ├── pricing.py          # DDG search + Ollama price extraction
│   ├── scheduler.py        # periodic sweep loop
│   └── templates/
│       └── queue.html      # server-rendered review page
└── tests/
```

### HTTP surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Review queue page (HTML) |
| GET | `/health` | Liveness probe |
| GET | `/status` | Last sweep time, counts (pending / applied / rejected) |
| GET | `/api/candidates` | JSON list of candidates (filterable by status) |
| POST | `/api/candidates/{id}/approve` | Apply price to Homebox, mark applied |
| POST | `/api/candidates/{id}/reject` | Mark rejected |
| POST | `/api/candidates/{id}` | Edit price/source before approving |
| POST | `/api/lookup` | Ad-hoc lookup `{ "query": "Sony WH-1000XM5" }` |
| POST | `/api/sweep` | Trigger a sweep immediately |

---

## Model choice

| Job | Model | Why |
| --- | --- | --- |
| Image → item identity **and** search snippets → price | `qwen2.5vl:3b` | One vision-language model covers both jobs. At Q4 it's ~3–4 GB, fitting comfortably in the RTX 3070's 8 GB VRAM with headroom for image tokens and context. |

**Why one model, not two.** The 8 GB card can't hold a 7B vision model *and* a separate
text model at once — Ollama would swap them in and out on every price sweep, adding latency
and complexity. A single 3B vision-language model does both jobs and stays resident. Using
a capable model to interpret messy search snippets is far more reliable than regex alone,
which cannot distinguish a product price from shipping, an old sale price, or an unrelated
number on the page.

**Tuning for 8 GB.** Keep Ollama's context modest (e.g. `num_ctx` 4096–8192) to bound KV
cache, and downscale very large iPhone photos before sending them so image tokens don't
blow the VRAM budget.

**If 3B identification proves too weak.** Step up to `qwen2.5vl:7b` and accept that the
hourly price sweeps will trigger a model swap (the 7B fills most of the 8 GB on its own).
That's a one-line change to `HBC_LLM_MODEL` / `PRICE_TEXT_MODEL`.

---

## Homebox API quirks (designed around)

- **Auth**: bearer token from `POST /api/v1/users/login` (form-encoded `username`/`password`), returned in the `token` field. No long-lived API keys yet; tokens expire after ~1 month. The sidecar supports either a static `HOMEBOX_TOKEN` or credentials for auto-refresh.
- **Updates**: prefer **PUT with the full item object** over PATCH. PATCH has been reported to silently drop some fields (notes, custom fields). The sidecar always read-modify-writes: GET the item, set `purchasePrice`, PUT it back.
- **Base path**: all endpoints live under `/api/v1`.

---

## Failure handling

- **Token expired** → sidecar re-logs in (if creds provided) or logs a clear error and pauses sweeps.
- **No price found** → candidate stored with `price = null`, `confidence = low`, still shown in queue so the human knows it was attempted.
- **Ambiguous currency** → forced to low confidence; never auto-suggested as high.
- **Homebox unreachable** → sweep skipped, retried next interval; sidecar stays up.
- **Duplicate prevention** → unique index ensures one pending candidate per item; already-priced items are skipped on scan.

---

## Out of scope

- No custom inventory dashboard (Homebox + Companion cover this).
- No writing prices without human approval.
- No managing the Homebox container itself.
- No multi-user / multi-tenant concerns (single household instance).
