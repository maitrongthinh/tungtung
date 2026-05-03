from __future__ import annotations

from common.config import load_settings


def build_tracking_link(post_id: str, fallback_link: str) -> str:
    settings = load_settings(refresh=True)
    base_url = settings.integrations.public_base_url.strip().rstrip("/")
    if not base_url:
        # No public URL configured — use direct affiliate link so clicks still work
        return fallback_link
    return f"{base_url}/r/{post_id}"
