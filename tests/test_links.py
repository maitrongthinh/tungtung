import os

from common.links import build_tracking_link


def test_build_tracking_link_uses_public_base_url(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agent.example")
    assert build_tracking_link("post-123", "https://shope.ee/raw") == "https://agent.example/r/post-123"


def test_build_tracking_link_falls_back_without_public_base_url(monkeypatch) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert build_tracking_link("post-123", "https://shope.ee/raw") == "https://shope.ee/raw"
