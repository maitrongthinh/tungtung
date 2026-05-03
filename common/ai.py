from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha1
from typing import Any

from common.config import AISettings
from common.database import Database


def estimate_tokens(*chunks: Any) -> int:
    text = "\n".join(_normalize_chunk(chunk) for chunk in chunks if chunk is not None)
    if not text.strip():
        return 0
    return max(1, len(text) // 4)


def cache_key(prefix: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = sha1(serialized.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def ai_budget_status(database: Database, settings: AISettings, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    usage = database.get_ai_usage_summary(current)
    return {
        "enabled": settings.enabled,
        "requests": usage["requests"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "remaining_requests": max(0, settings.max_daily_requests - usage["requests"]),
        "remaining_input_tokens": max(0, settings.max_daily_input_tokens - usage["input_tokens"]),
        "remaining_output_tokens": max(0, settings.max_daily_output_tokens - usage["output_tokens"]),
        "limits": {
            "max_daily_requests": settings.max_daily_requests,
            "max_daily_input_tokens": settings.max_daily_input_tokens,
            "max_daily_output_tokens": settings.max_daily_output_tokens,
        },
        "by_purpose": usage["by_purpose"],
    }


def can_consume_ai_budget(
    database: Database,
    settings: AISettings,
    *,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    now: datetime | None = None,
) -> bool:
    if not settings.enabled:
        return False
    status = ai_budget_status(database, settings, now=now)
    if status["remaining_requests"] <= 0:
        return False
    if estimated_input_tokens > status["remaining_input_tokens"]:
        return False
    if estimated_output_tokens > status["remaining_output_tokens"]:
        return False
    return True


def _normalize_chunk(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, (dict, list, tuple)):
        return json.dumps(chunk, ensure_ascii=False, default=str)
    return str(chunk)
