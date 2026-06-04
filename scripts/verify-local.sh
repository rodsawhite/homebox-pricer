#!/usr/bin/env bash
# Local Phase 1 verification — run this on the WSL host where Docker and your
# .env (Anthropic key, Homebox token) are available. CI cannot do these steps
# because they need a Docker daemon and real secrets.
#
# Usage:
#   scripts/verify-local.sh [--no-build] [--with-companion]
#
#   --no-build        Skip the price-lookup image build (reuse the last one).
#   --with-companion  Also start homebox-companion and probe its :8090 UI.
#                     Confirms it boots and loads the Anthropic key; it does
#                     NOT perform a real photo capture (that's a manual phone
#                     test). Requires HBC_LLM_API_KEY in .env.
#
# It is safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD=1
WITH_COMPANION=0
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    --with-companion) WITH_COMPANION=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }

step "Validating docker-compose.yml"
docker compose config >/dev/null
echo "compose config OK"

step "Running unit tests (host Python)"
if command -v uv >/dev/null 2>&1; then
  (cd price-lookup && uv run --extra dev pytest -q)
else
  echo "uv not found; falling back to pytest on PATH"
  (cd price-lookup && python -m pytest -q)
fi

if [[ "$BUILD" == "1" ]]; then
  step "Building the price-lookup image"
  docker compose build price-lookup
fi

step "Bringing up price-lookup (+ its deps: ollama, ollama-init)"
# This blocks on ollama-init pulling the model; the first run may take a while
# while the model downloads.
docker compose up -d price-lookup

step "Confirming the price model is present in Ollama"
MODEL="$(grep -E '^PRICE_TEXT_MODEL=' .env 2>/dev/null | cut -d= -f2)"
MODEL="${MODEL:-qwen2.5:3b}"
if docker compose exec -T ollama ollama list | grep -q "${MODEL%%:*}"; then
  echo "model present: ${MODEL}"
  docker compose exec -T ollama ollama list
else
  echo "model ${MODEL} NOT found in Ollama — ollama-init logs:"
  docker compose logs --tail=50 ollama-init
  exit 1
fi

step "Probing /health (up to ~30s)"
# price-lookup publishes on host port 8091 (8090 is Homebox Companion).
ok=0
for _ in $(seq 1 15); do
  if curl -fsS http://localhost:8091/health | grep -q '"status":"ok"'; then
    ok=1; break
  fi
  sleep 2
done

if [[ "$ok" == "1" ]]; then
  echo "health OK:"
  curl -fsS http://localhost:8091/health; echo
  echo "status:"
  curl -fsS http://localhost:8091/status; echo
else
  echo "health check FAILED — recent logs:"
  docker compose logs --tail=50 price-lookup
  exit 1
fi

if [[ "$WITH_COMPANION" == "1" ]]; then
  step "Bringing up homebox-companion"
  docker compose up -d homebox-companion

  step "Probing Companion UI on :8090 (up to ~30s)"
  cok=0
  for _ in $(seq 1 15); do
    # Companion serves its UI on 8090; any HTTP response means it booted.
    if curl -fsS -o /dev/null http://localhost:8090/; then
      cok=1; break
    fi
    sleep 2
  done

  if [[ "$cok" == "1" ]]; then
    echo "Companion UI reachable on http://localhost:8090/"
    echo "NOTE: this confirms Companion booted and loaded its config; it does"
    echo "      NOT verify a real photo capture — do that from the iPhone."
  else
    echo "Companion did NOT respond — recent logs (check HBC_LLM_API_KEY):"
    docker compose logs --tail=50 homebox-companion
    exit 1
  fi
fi

printf '\n\033[1;32mPhase 1 local verification passed.\033[0m\n'
