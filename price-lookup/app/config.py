"""Settings loaded from environment variables.

Only the Homebox connection details are strictly needed to boot; everything
else has a sensible default so the service (and its /health probe) can come up
even before the rest of the stack is configured. See the root .env.example for
the full list and descriptions.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Homebox ---
    homebox_url: str = "http://172.16.0.125:3900"
    homebox_token: str = ""
    homebox_user: str = ""
    homebox_password: str = ""

    # --- Ollama (local price-text parsing) ---
    ollama_url: str = "http://ollama:11434"
    price_text_model: str = "qwen2.5:3b"

    # --- Pricing behaviour ---
    price_region: str = "au-en"
    price_currency: str = "AUD"
    price_min_confidence: str = "low"
    check_interval: int = 3600
    # Use staticICE.com.au (AU price-comparison aggregator) as the primary
    # price source. When it returns AU listings the local model is skipped
    # entirely; otherwise the lookup falls through to DDG + Ollama.
    price_use_staticice: bool = True
    # PricesAPI.io — a paid product-search API (multi-retailer, AU default,
    # real-time). When a key is set it runs as the *second* tier, after
    # staticICE and before the DDG + Ollama fallback. Empty key disables it.
    prices_api_key: str = ""
    prices_api_url: str = "https://api.pricesapi.io/api/v1/products/search"
    prices_api_country: str = "au"
    # PricesAPI scrapes in real time (30–90s cold, <100ms cached). Docs recommend
    # a 95s client read timeout. Rate limit: 6 req/min on Personal plan.
    prices_api_timeout: float = 95.0
    # How many top search-result pages to fetch and scrape structured price
    # data (JSON-LD / og:price) from. 0 disables page fetching (snippets only).
    price_fetch_pages: int = 3
    # Per-page fetch timeout in seconds.
    price_fetch_timeout: float = 8.0

    # --- Server ---
    server_port: int = 8091
    db_path: str = "/data/price.db"

    # --- Observability ---
    # "plain" keeps the human-readable logs; "json" emits one JSON object per
    # line for log shippers. Default plain so existing readable logs stay.
    log_format: str = "plain"


def get_settings() -> Settings:
    """Return a fresh Settings instance (kept callable for easy test overrides)."""
    return Settings()
