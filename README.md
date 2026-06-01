# Homebox Pricer

A self-hosted stack for photo-based inventory capture into [Homebox](https://github.com/sysadminsmedia/homebox), with automatic **AUD price lookup** for items that are missing a value.

Image recognition for capture uses **Anthropic's Claude Haiku 4.5** (cloud vision); the price lookup runs **locally** on a small text model via Ollama. Inventory and approval stay self-hosted — nothing is written to Homebox without your say-so.

> **Not affiliated with Homebox or Homebox Companion.** This repo orchestrates two existing open-source projects and adds one small service of its own.

---

## What it does

1. **Capture** — Snap photos from your iPhone. [Homebox Companion](https://github.com/Duelion/homebox-companion) sends them to **Claude Haiku 4.5** to identify items (name, manufacturer, model, serial, visible price) and writes them into Homebox.
2. **Price** — A small **price-lookup** sidecar periodically scans Homebox for items with no purchase price, searches the web for current AUD retail pricing, and parses the results with a local text model on Ollama.
3. **Review** — Found prices land in a **review queue** (a minimal web page served by the sidecar). You approve or reject each one. Approved prices are written back to Homebox. Nothing touches your inventory without your say-so.

```
iPhone ──photo──▶ Homebox Companion ──▶ Homebox (NAS)
                        │                   ▲
                        ▼                   │ approved prices
              Claude Haiku 4.5             │
                (cloud vision)       price-lookup sidecar
                                     (scan ▸ search ▸ queue ▸ review)
                                              │
                                              ▼
                                         Ollama (local
                                       text model, pricing)
```

---

## Components

| Service | Source | Role | Image / Build |
| --- | --- | --- | --- |
| `homebox-companion` | [Duelion/homebox-companion](https://github.com/Duelion/homebox-companion) | Photo capture + AI item detection | `ghcr.io/duelion/homebox-companion:latest` |
| `ollama` | [ollama/ollama](https://github.com/ollama/ollama) | Local text inference for price parsing (GPU) | `ollama/ollama:latest` |
| `price-lookup` | This repo (`./price-lookup`) | Price scan, lookup, review queue | built locally |
| `homebox` | [sysadminsmedia/homebox](https://github.com/sysadminsmedia/homebox) | Inventory system (source of truth) | runs separately on NAS |

Homebox itself is **not** managed by this stack — it already runs on the NAS at `http://172.16.0.125:3900`.

---

## Requirements

- A Windows host with Docker running under WSL2
- An NVIDIA GPU for the local price-parsing model. A lean 3B text model needs only ~2–3 GB VRAM, so the RTX 3070's 8 GB is comfortable. Docker Desktop ships the GPU passthrough; no separate Container Toolkit install needed.
- A reachable Homebox instance (v0.21+) and a bearer token
- An **Anthropic API key** for Claude Haiku 4.5 (capture/image recognition)
- ~2 GB disk for the local text model

---

## Quick start

```bash
git clone <this-repo> homebox-pricer
cd homebox-pricer

cp .env.example .env
# edit .env — set HOMEBOX_TOKEN and HBC_LLM_API_KEY (Anthropic) at minimum

docker compose up -d
```

That's it — no manual model download. On first start the `ollama-init` service
automatically pulls the local price-parsing model (`PRICE_TEXT_MODEL`, default
`qwen2.5:3b`) into a persistent volume, and `price-lookup` waits until that pull
finishes before it starts. The model is cached, so subsequent starts are instant.

> Capture/vision runs on Claude Haiku 4.5 (cloud); Ollama only needs this lean
> text model to parse price snippets, so it fits easily in 8 GB VRAM. To use a
> different model, set `PRICE_TEXT_MODEL` in `.env` before `docker compose up`.

Watch the model pull on first run with `docker compose logs -f ollama-init`.

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
| `HBC_LLM_API_KEY` | yes | Anthropic API key used by Companion for Claude Haiku 4.5 |
| `HBC_LLM_MODEL` | no | Capture model (default `anthropic/claude-haiku-4-5`) |
| `PRICE_TEXT_MODEL` | no | Local Ollama model for price parsing (default `qwen2.5:3b`) |
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
