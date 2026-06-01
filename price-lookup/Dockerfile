# syntax=docker/dockerfile:1
# Multi-stage build: install deps with uv, then run a slim final image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached layer), then the app.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-cache .

COPY app ./app

FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app
# Bring over installed site-packages + console scripts from the build stage.
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY app ./app

EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8091/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8091"]
