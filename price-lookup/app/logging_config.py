"""Logging setup for the sidecar.

Two output styles, chosen at startup via LOG_FORMAT:

  - "plain" (default): the familiar human-readable single-line format, so
    nothing changes for anyone tailing logs locally.
  - "json": one JSON object per line, suitable for a log shipper. Extra
    structured fields (item_id, sweep counts, …) are attached per-record via
    the stdlib ``extra=`` kwarg and surface as top-level JSON keys.

We stay on stdlib ``logging`` deliberately — no extra dependency, and a custom
Formatter is all the structure we need.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# LogRecord attributes that are intrinsic to the record; anything *not* in this
# set was passed by the caller via ``extra=`` and is worth promoting to a
# top-level JSON field.
_STD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON line with any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Promote caller-supplied structured fields (extra=...) to top level.
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_format: str = "plain", level: int = logging.INFO) -> None:
    """Install a single stdout handler in the chosen format (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any handlers so re-configuration (tests, reload) stays clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.addHandler(handler)
