# Build Tasks

Phased, ordered checklist. Each phase produces something runnable before moving on. Checkbox format is import-friendly for GitHub issues / project boards.

---

## Phase 0 — Prerequisites (host setup)

**Platform decision (settled):** Docker Desktop for Windows with the WSL2 backend. The
machine is a daily-driver Windows desktop with spare RAM — not a dedicated Linux host —
so Docker Desktop is the pragmatic choice. It hides WSL2 networking and auto-forwards
published container ports to the Windows host. There is no GPU-accelerated path for Linux
containers on Windows that avoids WSL2 short of installing native Linux, which we are not
doing.

**Already in place:** Docker Desktop installed · CPU virtualization enabled in BIOS ·
NVIDIA driver present (RTX 3070).

> **Legend for running this in Claude Code:**
> 🟢 = Claude Code can do this from the WSL shell.
> 🔴 = **Windows-host action — outside WSL, so Claude Code cannot do it.** Do these
> yourself in Windows (PowerShell as admin, Docker Desktop GUI, or a text editor), then
> tell Claude Code they're done.

### Claude Code environment (do first)

- [ ] 🔴 Confirm Docker Desktop is running and **WSL integration is enabled** for your
      distro (Docker Desktop → Settings → Resources → WSL Integration → toggle your distro)
- [ ] 🟢 In the WSL distro, confirm the Docker CLI talks to the daemon: `docker version`
      and `docker run --rm hello-world`
- [ ] 🔴 Install Claude Code in the WSL distro (native installer, no Node needed):
      `curl -fsSL https://claude.ai/install.sh | bash` — *(listed 🔴 because it's a
      one-time bootstrap before any Claude Code session exists)*
- [ ] 🟢 Create the project inside the **WSL filesystem** (e.g. `~/homebox-pricer`),
      **not** under `/mnt/c/...` — Windows-mounted paths are slow and cause file-watch and
      permission issues
- [ ] 🟢 `git init` (or clone) the repo and drop these MD files in

### GPU passthrough

- [ ] 🟢 Verify the GPU is visible to containers (the gate for everything Ollama):
      `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`
      → must print the RTX 3070. With Docker Desktop the NVIDIA Container Toolkit ships in
      the WSL backend; no separate toolkit install is needed.
- [ ] 🔴 If it errors: update the Windows NVIDIA driver and ensure Docker Desktop is
      current, then re-test

### VRAM & RAM headroom

- [ ] 🟢 Sanity-check the VRAM budget: the local text model `qwen2.5:3b` uses ~2–3 GB of the
      3070's **8 GB VRAM** (capture/vision runs in the cloud on Claude Haiku 4.5, so it does
      not consume VRAM). Plenty of headroom. (VRAM is the GPU's own memory — separate from
      the WSL system-RAM cap below.)
- [ ] 🔴 (If containers feel starved) raise the WSL2 **system RAM** cap in
      `%UserProfile%\.wslconfig` — this governs host RAM for the containers/Ollama overhead,
      not VRAM:
      ```ini
      [wsl2]
      memory=12GB
      ```
      Then `wsl --shutdown` in PowerShell and restart Docker Desktop. *(Editing this file
      and `wsl --shutdown` are Windows-host actions.)*

### iPhone reachability (LAN)

- [ ] 🔴 Add **one inbound Windows Firewall rule** allowing TCP on ports **8000**
      (Companion) and **8090** (review queue). Docker Desktop forwards published ports to
      the host, but Windows Firewall blocks inbound LAN connections by default.
- [ ] 🔴 Find the Windows LAN IP (`ipconfig` → IPv4) and reserve a static DHCP lease for it
      on the router so it doesn't change
- [ ] 🟢 / 🔴 Smoke test from the iPhone once Phase 1 is up: `http://<windows-LAN-IP>:8000`
      *(Claude Code can confirm the port is listening on the host; the actual phone test is
      yours.)*

### Homebox connectivity & auth

- [ ] 🟢 Confirm containers can reach the NAS — Homebox lives on `172.16.0.125` (a
      different subnet from the LAN), so verify routing works:
      `docker run --rm curlimages/curl -sf http://172.16.0.125:3900/api/v1/status`
- [ ] 🔴 Generate a Homebox bearer token (log in via the API) — credentials are yours to
      enter; don't paste them into a Claude Code transcript:
      `POST /api/v1/users/login` → `token`
- [ ] 🟢 Confirm the token works: `GET /api/v1/items` with `Authorization: Bearer <token>`
- [ ] 🔴 Decide token strategy: static `HOMEBOX_TOKEN` (expires ~monthly) **vs** stored
      `HOMEBOX_USER` + `HOMEBOX_PASSWORD` for auto-refresh — affects Phase 2's client

**Done when:** `nvidia-smi` works inside a container, the WSL project dir exists, the host
can reach Homebox with a valid token, and the firewall rule + LAN IP are ready for the
iPhone.

---

## Phase 1 — Stack skeleton

- [ ] Write `docker-compose.yml` with three services: `homebox-companion`, `ollama`, `price-lookup`
- [ ] Add GPU `deploy.resources` block to the `ollama` service
- [ ] Add a healthcheck to `ollama`; make `homebox-companion` and `price-lookup` depend on it
- [ ] Create `.env.example` and document every variable (incl. `HBC_LLM_API_KEY` Anthropic key)
- [ ] Configure Companion for Claude Haiku 4.5: `HBC_LLM_MODEL=anthropic/claude-haiku-4-5`,
      Anthropic key in `HBC_LLM_API_KEY`, `HBC_LLM_API_BASE` left blank
- [ ] **Verify the Companion↔Anthropic assumption**: confirm the Companion build accepts an
      Anthropic provider and this model alias (check its LLM config docs); adjust env names/format if needed
- [ ] Bring the stack up with a placeholder `price-lookup` (health endpoint only)
- [ ] Pull the local text model: `qwen2.5:3b` (price parsing only; capture is cloud)
- [ ] Verify Companion reaches Anthropic and Homebox (do one test capture end-to-end)

**Done when:** a photo taken on the iPhone creates an item in Homebox (via Claude Haiku 4.5), and `price-lookup` answers `/health`.

---

## Phase 2 — Homebox client + scan

- [ ] `config.py` — load settings from env via pydantic-settings
- [ ] `homebox.py` — login / token-refresh, `list_items` (paginated), `get_item`, `put_item`
- [ ] `db.py` — create SQLite schema, insert/query/update candidates
- [ ] `scheduler.py` — background loop every `CHECK_INTERVAL`
- [ ] Scan logic: fetch all items, filter to unpriced, skip ones with an open candidate
- [ ] `/status` and `/api/sweep` endpoints
- [ ] Log how many items are unpriced on each sweep

**Done when:** a manual `POST /api/sweep` populates the candidates table with the right items (price still null).

---

## Phase 3 — Pricing pipeline

- [ ] Add `duckduckgo_search` (DDGS) to dependencies
- [ ] `pricing.py` — build query from name + manufacturer + model, search region `au-en`
- [ ] Pass titles + snippets to Ollama `qwen2.5:3b` (local text model) with a strict JSON-output prompt
- [ ] Parse model output → `{price, currency, source, confidence, reason}`
- [ ] AUD disambiguation: prefer `.com.au` / explicit `AUD`; mark `$`-only results low-confidence
- [ ] Store results back on the candidate rows
- [ ] `/api/lookup` ad-hoc endpoint for one-off queries (no Homebox write)
- [ ] Add basic rate limiting / politeness delay between searches

**Done when:** a sweep fills candidates with plausible AUD prices and confidence levels.

---

## Phase 4 — Review queue UI

- [ ] `templates/queue.html` — server-rendered list: thumbnail, name, candidate price, source link, confidence badge
- [ ] Approve / Reject / Edit actions wired to the API
- [ ] Approve flow: GET item → set `purchasePrice` → PUT back → mark `applied`
- [ ] Edit flow: let the human override price/source before approving
- [ ] Empty state + last-sweep summary on the page
- [ ] Pull the item thumbnail from Homebox for visual confirmation

**Done when:** approving a candidate writes the price into Homebox and it shows in the Homebox UI.

---

## Phase 5 — Hardening + polish

- [ ] Handle token expiry mid-sweep (re-login if creds present, else pause + warn)
- [ ] Retry/backoff for transient Homebox or search failures
- [ ] Confidence threshold config (`PRICE_MIN_CONFIDENCE`) to pre-filter the queue
- [ ] Structured logging
- [ ] `tests/` — unit tests for query building, price parsing, AUD disambiguation
- [ ] Dockerfile multi-stage build; pin base image
- [ ] README pass: real screenshots of the review queue

**Done when:** the stack survives a token expiry and a Homebox restart without manual intervention.

---

## Nice-to-haves (backlog)

- [ ] eBay AU sold-listings as a second-hand price source alongside retail
- [ ] Bulk approve for high-confidence candidates
- [ ] Per-category query hints (electronics vs furniture search differently)
- [ ] Notification (e.g. ntfy / webhook) when new candidates are ready to review
- [ ] Optional: store price history over time for revaluation
