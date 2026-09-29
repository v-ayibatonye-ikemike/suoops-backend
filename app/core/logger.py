from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.core.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # pragma: no cover - formatting logic
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in payload:
                continue
            payload.setdefault("extra", {})[key] = value
        return json.dumps(payload, default=str)


def init_logging(level: int | None = None) -> None:
    if logging.getLogger().handlers:
        return
    effective_level = level or getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_FORMAT.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.setLevel(effective_level)
    root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy_logger in [
        "fontTools",
        "fontTools.subset",
        "fontTools.ttLib",
        "fontTools.subset.timer",
        "httpx",
        "httpcore",
        "PIL",
    ]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
