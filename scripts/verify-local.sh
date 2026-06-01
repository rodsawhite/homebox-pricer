#!/usr/bin/env bash
# Local Phase 1 verification — run this on the WSL host where Docker and your
# .env (Anthropic key, Homebox token) are available. CI cannot do these steps
# because they need a Docker daemon and real secrets.
#
# It is safe to re-run. Pass --no-build to skip the image build.
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD=1
[[ "${1:-}" == "--no-build" ]] && BUILD=0

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

step "Bringing up price-lookup (+ its deps)"
docker compose up -d price-lookup

step "Probing /health (up to ~30s)"
ok=0
for _ in $(seq 1 15); do
  if curl -fsS http://localhost:8090/health | grep -q '"status":"ok"'; then
    ok=1; break
  fi
  sleep 2
done

if [[ "$ok" == "1" ]]; then
  echo "health OK:"
  curl -fsS http://localhost:8090/health; echo
  echo "status:"
  curl -fsS http://localhost:8090/status; echo
  printf '\n\033[1;32mPhase 1 local verification passed.\033[0m\n'
else
  echo "health check FAILED — recent logs:"
  docker compose logs --tail=50 price-lookup
  exit 1
fi
