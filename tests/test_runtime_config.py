from pathlib import Path

from common.config import load_settings, save_runtime_config


def test_runtime_config_can_override_defaults(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    runtime_path = tmp_path / "runtime_config.json"
    config_path.write_text("meta:\n  window_a_start: '11:00'\n", encoding="utf-8")
    monkeypatch.setattr("common.config.RUNTIME_CONFIG_PATH", runtime_path)
    save_runtime_config(
        {
            "meta": {"window_a_start": "09:30"},
            "integrations": {"public_base_url": "https://example.com"},
        },
        config_path=config_path,
    )
    settings = load_settings(config_path=config_path, refresh=True)
    assert settings.meta.window_a_start == "09:30"
    assert settings.integrations.public_base_url == "https://example.com"
