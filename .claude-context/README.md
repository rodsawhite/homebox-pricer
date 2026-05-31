# Homebox Pricer

A self-hosted stack for photo-based inventory capture into [Homebox](https://github.com/sysadminsmedia/homebox), with automatic **AUD price lookup** for items that are missing a value.

Everything runs locally. No paid AI APIs. Vision runs on a local GPU via Ollama.

> **Not affiliated with Homebox or Homebox Companion.** This repo orchestrates two existing open-source projects and adds one small service of its own.

---

## What it does

1. **Capture** — Snap photos from your iPhone. [Homebox Companion](https://github.com/Duelion/homebox-companion) uses a local vision model to identify items (name, manufacturer, model, serial, visible price) and writes them into Homebox.
2. **Price** — A small **price-lookup** sidecar periodically scans Homebox for items with no purchase price, searches the web for current AUD retail pricing, and parses the results with a local text model.
3. **Review** — Found prices land in a **review queue** (a minimal web page served by the sidecar). You approve or reject each one. Approved prices are written back to Homebox. Nothing touches your inventory without your say-so.

```
iPhone ──photo──▶ Homebox Companion ──▶ Homebox (NAS)
                        │                   ▲
                        ▼                   │ approved prices
                     Ollama                 │
                   (vision +          price-lookup sidecar
                    text models)      (scan ▸ search ▸ queue ▸ review)
```

---

## Components

| Service | Source | Role | Image / Build |
| --- | --- | --- | --- |
| `homebox-companion` | [Duelion/homebox-companion](https://github.com/Duelion/homebox-companion) | Photo capture + AI item detection | `ghcr.io/duelion/homebox-companion:latest` |
| `ollama` | [ollama/ollama](https://github.com/ollama/ollama) | Local vision + text inference (GPU) | `ollama/ollama:latest` |
| `price-lookup` | This repo (`./price-lookup`) | Price scan, lookup, review queue | built locally |
| `homebox` | [sysadminsmedia/homebox](https://github.com/sysadminsmedia/homebox) | Inventory system (source of truth) | runs separately on NAS |

Homebox itself is **not** managed by this stack — it already runs on the NAS at `http://172.16.0.125:3900`.

---

## Requirements

- A Windows host with Docker running under WSL2
- An NVIDIA GPU with ~8 GB VRAM (tested target: RTX 3070, 8 GB). Docker Desktop ships the GPU passthrough; no separate Container Toolkit install needed.
- A reachable Homebox instance (v0.21+) and a bearer token
- ~4 GB disk for the Qwen2.5-VL 3B model

---

## Quick start

```bash
git clone <this-repo> homebox-pricer
cd homebox-pricer

cp .env.example .env
# edit .env — set HOMEBOX_TOKEN at minimum

docker compose up -d

# Pull the single vision model once (first run only).
# Qwen2.5-VL 3B handles BOTH image identification and price-text parsing,
# so one model serves both Companion and the sidecar — fits in 8 GB VRAM.
docker exec ollama ollama pull qwen2.5vl:3b
```

Then open:

- **Capture UI** (Homebox Companion): `http://<host>:8000`
- **Price review queue** (sidecar): `http://<host>:8090`
- **Inventory** (Homebox, on NAS): `http://172.16.0.125:3900`

---

## Configuration

All config is via environment variables in `.env`. See [`.env.example`](.env.example) for the full list. The essentials:

| Variable | Required | Description |
| --- | --- | --- |
| `HOMEBOX_URL` | yes | Homebox base URL, e.g. `http://172.16.0.125:3900` |
| `HOMEBOX_TOKEN` | yes | Bearer token from Homebox (see note below) |
| `HBC_LLM_MODEL` | no | Vision model for capture (default `ollama/qwen2.5vl:3b`) |
| `PRICE_REGION` | no | Search region (default `au-en`) |
| `PRICE_CURRENCY` | no | Currency code (default `AUD`) |
| `CHECK_INTERVAL` | no | Seconds between price sweeps (default `3600`) |

### Getting a Homebox token

Homebox does not yet expose long-lived API keys. Obtain a bearer token by logging in:

```bash
curl -X POST "http://172.16.0.125:3900/api/v1/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=YOU@example.com&password=YOURPASSWORD"
# → {"token": "...", "expiresAt": "..."}
```

Tokens expire after roughly a month. The sidecar can be configured with login credentials instead of a static token so it can refresh automatically — see [`price-lookup/README.md`](price-lookup/README.md).

---

## Currency note (AUD)

Pricing targets the Australian market: DuckDuckGo region `au-en`, currency `AUD`. Because both AUD and USD use the `$` symbol, the parser prefers prices from `.com.au` domains and results that explicitly mention `AUD` / `A$`, and flags anything ambiguous as low-confidence (which keeps it out of the auto-fill path and surfaces it in the review queue for a human call).

---

## Repo layout

```
homebox-pricer/
├── README.md              ← you are here
├── ARCHITECTURE.md        ← design + data flow detail
├── TASKS.md               ← phased build checklist
├── .env.example
├── docker-compose.yml     ← (to be added in Phase 1)
└── price-lookup/          ← the one service we build
    ├── README.md
    └── ...                ← (added across Phases 2–4)
```

## Licence

GPL-3.0, to stay compatible with the Homebox Companion codebase it sits alongside.
