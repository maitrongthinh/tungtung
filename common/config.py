from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_CONFIG_PATH = BASE_DIR / "memory" / "runtime_config.json"
RUNTIME_EDITABLE_SECTIONS = ("runtime", "web", "shopee", "meta", "kpi", "memory", "loop", "features", "ai", "integrations", "focus")


class ShopeeSettings(BaseModel):
    rate_limit_per_second: int = 30
    degraded_rate_limit_per_second: int = 5
    proxy_rotate_every: int = 75
    min_product_score: int = 65
    image_min_width: int = 400
    max_products_per_cycle: int = 80
    search_terms_per_category: int = 4
    crawler_headless: bool = True
    request_timeout_seconds: int = 30
    affiliate_cache_ttl_hours: int = 24
    affiliate_auth_mode: str = "bearer"


class MetaSettings(BaseModel):
    window_a_start: str = "11:00"
    window_a_end: str = "13:00"
    window_b_start: str = "20:00"
    window_b_end: str = "22:00"
    graph_api_version: str = "v23.0"
    verify_before_window_minutes: int = 10
    token_refresh_days_before_expiry: int = 5
    publish_mode: str = "publish"
    require_human_approval: bool = False
    recent_post_refresh_hours: int = 24


class KPISettings(BaseModel):
    posts_per_day: int = 20
    posts_per_account: int = 7
    min_product_score: int = 65
    max_same_category: int = 3
    draft_buffer: int = 25


class MemorySettings(BaseModel):
    compact_trigger_hour: int = 1
    max_context_tokens: int = 80000
    snapshot_retention_days: int = 90
    collection_name: str = "agent_insights"


class LoopSettings(BaseModel):
    crawl_start_hour: int = 6
    cleanup_start_hour: int = 0
    idle_crawl: bool = True
    improvement_sync_hours: int = 4


class CategoryFocusSettings(BaseModel):
    enabled: bool = False
    focus_category: str = ""
    duration_days: int = 21
    started_at: str | None = None


class RuntimeSettings(BaseModel):
    execution_mode: str = "local"
    local_queue_workers: int = 4


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"


class StorageSettings(BaseModel):
    sqlite_path: str = "data/post_farm.db"
    log_dir: str = "logs"
    temp_dir: str = "tmp"
    asset_retention_days: int = 30
    temp_retention_hours: int = 24
    processed_command_retention_days: int = 14


class FeatureSettings(BaseModel):
    auto_reply_enabled: bool = False
    comment_monitoring_enabled: bool = True
    download_assets: bool = True
    fun_post_enabled: bool = True
    fun_post_probability: float = 0.20
    fun_post_types: list[str] = Field(default_factory=lambda: ["meme", "tip"])


class AISettings(BaseModel):
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    max_daily_requests: int = 18
    max_daily_input_tokens: int = 180000
    max_daily_output_tokens: int = 30000
    analyzer_max_tokens: int = 500
    writer_max_tokens: int = 900
    score_top_products_per_cycle: int = 10
    write_top_posts_per_cycle: int = 6
    scorer_cache_ttl_hours: int = 24
    writer_cache_ttl_hours: int = 72


class IntegrationSettings(BaseModel):
    redis_url: str = "redis://redis:6379/0"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    # Danh sách providers để rotate. Mỗi item: {"provider": "gemini"|"trollllm"|"openai", "key": "...", "model": "...", "base_url": "..."}
    # Khi ai_providers không rỗng, sẽ được ưu tiên hơn ai_api_key đơn lẻ
    ai_providers: list[dict] = Field(default_factory=list)
    shopee_affiliate_token: str = ""
    shopee_affiliate_credential: str = ""
    shopee_affiliate_secret: str = ""
    shopee_affiliate_cookie: str = ""
    shopee_publisher_id: str = ""
    shopee_sub_id_prefix: str = "shopee-agent"
    proxy_list: list[str] = Field(default_factory=list)
    meta_app_id: str = ""
    meta_app_secret: str = ""
    public_base_url: str = ""
    web_secret_key: str = ""


class AppSettings(BaseModel):
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    shopee: ShopeeSettings = Field(default_factory=ShopeeSettings)
    meta: MetaSettings = Field(default_factory=MetaSettings)
    kpi: KPISettings = Field(default_factory=KPISettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    ai: AISettings = Field(default_factory=AISettings)
    integrations: IntegrationSettings = Field(default_factory=IntegrationSettings)
    focus: CategoryFocusSettings = Field(default_factory=CategoryFocusSettings)

    @property
    def sqlite_path(self) -> Path:
        return (BASE_DIR / self.storage.sqlite_path).resolve()

    @property
    def log_dir(self) -> Path:
        return (BASE_DIR / self.storage.log_dir).resolve()

    @property
    def temp_dir(self) -> Path:
        return (BASE_DIR / self.storage.temp_dir).resolve()

    @property
    def accounts_dir(self) -> Path:
        return (BASE_DIR / "accounts").resolve()

    @property
    def farm_dir(self) -> Path:
        return (BASE_DIR / "farm").resolve()

    @property
    def memory_dir(self) -> Path:
        return (BASE_DIR / "memory").resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_runtime_config() -> dict[str, Any]:
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8-sig"))


def _apply_env_fallbacks(settings: AppSettings) -> AppSettings:
    integrations = settings.integrations
    integrations.ai_api_key = integrations.ai_api_key or os.getenv("AI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    integrations.ai_base_url = integrations.ai_base_url or os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    integrations.redis_url = integrations.redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    integrations.shopee_affiliate_token = integrations.shopee_affiliate_token or os.getenv("SHOPEE_AFFILIATE_TOKEN", "")
    integrations.shopee_affiliate_credential = integrations.shopee_affiliate_credential or os.getenv("SHOPEE_AFFILIATE_CREDENTIAL", "")
    integrations.shopee_affiliate_secret = integrations.shopee_affiliate_secret or os.getenv("SHOPEE_AFFILIATE_SECRET", "")
    integrations.shopee_affiliate_cookie = integrations.shopee_affiliate_cookie or os.getenv("SHOPEE_AFFILIATE_COOKIE", "")
    integrations.shopee_publisher_id = integrations.shopee_publisher_id or os.getenv("SHOPEE_PUBLISHER_ID", "")
    integrations.shopee_sub_id_prefix = integrations.shopee_sub_id_prefix or os.getenv("SHOPEE_SUB_ID_PREFIX", "shopee-agent")
    integrations.meta_app_id = integrations.meta_app_id or os.getenv("META_APP_ID", "")
    integrations.meta_app_secret = integrations.meta_app_secret or os.getenv("META_APP_SECRET", "")
    integrations.public_base_url = integrations.public_base_url or os.getenv("PUBLIC_BASE_URL", "")
    integrations.web_secret_key = integrations.web_secret_key or os.getenv("SECRET_KEY", "")
    if not integrations.proxy_list:
        integrations.proxy_list = [item.strip() for item in os.getenv("PROXY_LIST", "").split(",") if item.strip()]
    return settings


def runtime_config_payload() -> dict[str, Any]:
    settings = load_settings(refresh=True)
    return {
        section: getattr(settings, section).model_dump(mode="json")
        for section in RUNTIME_EDITABLE_SECTIONS
    }


def save_runtime_config(payload: dict[str, Any], config_path: str | Path | None = None) -> AppSettings:
    load_dotenv(BASE_DIR / ".env")
    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    editable_payload = {key: value for key, value in payload.items() if key in RUNTIME_EDITABLE_SECTIONS}
    merged = _deep_merge(data, editable_payload)
    settings = AppSettings.model_validate(merged)
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    runtime_payload = {
        section: getattr(settings, section).model_dump(mode="json")
        for section in RUNTIME_EDITABLE_SECTIONS
    }
    RUNTIME_CONFIG_PATH.write_text(json.dumps(runtime_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _load_settings_cached.cache_clear()
    return load_settings(refresh=True)


@lru_cache(maxsize=2)
def _load_settings_cached(config_path_str: str) -> AppSettings:
    load_dotenv(BASE_DIR / ".env")
    path = Path(config_path_str) if config_path_str else BASE_DIR / "config.yaml"
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    runtime_data = _load_runtime_config()
    settings = AppSettings.model_validate(_deep_merge(data, runtime_data))
    settings = _apply_env_fallbacks(settings)
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.accounts_dir.mkdir(parents=True, exist_ok=True)
    settings.farm_dir.mkdir(parents=True, exist_ok=True)
    settings.memory_dir.mkdir(parents=True, exist_ok=True)
    return settings


def load_settings(config_path: str | Path | None = None, *, refresh: bool = False) -> AppSettings:
    if refresh:
        _load_settings_cached.cache_clear()
    path = str(Path(config_path).resolve()) if config_path else ""
    settings = _load_settings_cached(path)
    if not settings.integrations.web_secret_key:
        import warnings
        warnings.warn(
            "web_secret_key is empty — set SECRET_KEY env var or integrations.web_secret_key in config",
            stacklevel=2,
        )
    return settings
