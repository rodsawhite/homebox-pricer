# Homebox Pricer

A pricing sidecar for [Homebox](https://github.com/sysadminsmedia/homebox). It periodically scans your inventory for items with no purchase price, searches the web for current **AUD retail pricing**, and presents the results in a **review queue** — nothing is written to Homebox until you approve it.

Photo capture and AI item identification are handled separately by [Homebox Companion](https://github.com/Duelion/homebox-companion), which you run and configure independently.

> **Not affiliated with Homebox or Homebox Companion.**

---

## What this repo does

1. **Price** — Scans Homebox for items with no `purchasePrice`, builds a search query per item, fetches DuckDuckGo results (`au-en`), and extracts a price + confidence level using a local text model on Ollama.
2. **Review** — Prices land in a card-based **review queue** at `:8091`. Approve, reject, or manually edit each one. Approved prices are written back to Homebox via a read-modify-write PUT (no PATCH).

```
Homebox (NAS) ◀─── approved prices ───┐
      │                                │
      └──── items list ────▶ price-lookup sidecar
                               (scan ▸ search ▸ queue ▸ review)
                                        │
                                        ▼
                                   Ollama (local
                                 text model, pricing)
```

The capture path (iPhone → Companion → Homebox) is entirely outside this repo.

---

## Components managed by this compose file

| Service | Source | Role |
| --- | --- | --- |
| `price-lookup` | This repo (`./price-lookup`) | Price scan, lookup, review queue — built locally |
| `ollama` | [ollama/ollama](https://github.com/ollama/ollama) | Local text inference for price parsing (GPU) |
| `ollama-init` | one-shot init container | Pulls `PRICE_TEXT_MODEL` into the shared volume on first run |

**Not managed here:** Homebox (runs on NAS), Homebox Companion (runs separately — see its own docs).

---

## Requirements

- Docker running under WSL2 (or Linux)
- An NVIDIA GPU for the local price-parsing model (`qwen2.5:3b` uses ~2–3 GB VRAM). Docker Desktop on Windows ships GPU passthrough — no separate Container Toolkit install needed.
- A reachable Homebox instance (v0.21+) and a bearer token or login credentials
- ~2 GB disk for the local text model (auto-downloaded on first start)

---

## Quick start

```bash
git clone <this-repo> homebox-pricer
cd homebox-pricer

cp .env.example .env
# edit .env — set HOMEBOX_URL and HOMEBOX_TOKEN at minimum

docker compose up -d
```

No manual model download required. On first start `ollama-init` automatically pulls `PRICE_TEXT_MODEL` (default `qwen2.5:3b`) into a persistent volume, and `price-lookup` waits until that pull finishes before starting.

Watch the model pull with `docker compose logs -f ollama-init`.

Then open the **price review queue** at `http://<host>:8091`.

---

## Configuration

All config is via environment variables in `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Required | Description |
| --- | --- | --- |
| `HOMEBOX_URL` | yes | Homebox base URL, e.g. `http://172.16.0.125:3900` |
| `HOMEBOX_TOKEN` | yes* | Bearer token from Homebox login |
| `HOMEBOX_USER` / `HOMEBOX_PASSWORD` | yes* | Login credentials for auto token-refresh (alternative to static token) |
| `PRICE_TEXT_MODEL` | no | Local Ollama model for price parsing (default `qwen2.5:3b`) |
| `PRICE_REGION` | no | DuckDuckGo region (default `au-en`) |
| `PRICE_CURRENCY` | no | Currency code (default `AUD`) |
| `PRICE_MIN_CONFIDENCE` | no | Hide queue candidates below this confidence — `low`/`medium`/`high` (default `low`) |
| `CHECK_INTERVAL` | no | Seconds between price sweeps (default `3600`) |
| `LOG_FORMAT` | no | `plain` (default) or `json` structured log output |

*Provide either `HOMEBOX_TOKEN` **or** `HOMEBOX_USER`+`HOMEBOX_PASSWORD`. If both are set, credentials win and the token is used as a warm start.

### Getting a Homebox token

```bash
curl -X POST "http://<homebox>/api/v1/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=you@example.com&password=yourpassword"
# → {"token": "...", "expiresAt": "..."}
```

Tokens expire after roughly a month. Use `HOMEBOX_USER`/`HOMEBOX_PASSWORD` for automatic refresh.

---

## Currency note (AUD)

The parser prefers prices from `.com.au` domains and results that explicitly mention `AUD`/`A$`. Anything ambiguous (bare `$` from a non-AU source) is flagged as low-confidence and shown in the review queue rather than auto-suggested.

---

## Homebox Companion setup

Companion handles photo capture — you configure and run it independently. Key env vars for Companion:

```
HBC_LLM_API_KEY=sk-ant-...                          # Anthropic key
HBC_LLM_MODEL=anthropic/claude-haiku-4-5-20251001   # full versioned ID
HBC_HOMEBOX_URL=http://172.16.0.125:3900
HBC_SERVER_HOST=0.0.0.0
HBC_SERVER_PORT=8090
```

No `HBC_LLM_API_BASE` needed — Companion resolves Anthropic natively. See [Companion's docs](https://github.com/Duelion/homebox-companion) for the full setup.

---

## Repo layout

```
homebox-pricer/
├── README.md              ← you are here
├── ARCHITECTURE.md        ← design + data flow detail
├── TASKS.md               ← phased build checklist
├── .env.example
├── docker-compose.yml     ← ollama + ollama-init + price-lookup
└── price-lookup/          ← the one service we build
    ├── README.md
    ├── Dockerfile
    ├── app/
    └── tests/
```

## Licence

GPL-3.0, to stay compatible with the Homebox Companion codebase it sits alongside.
