from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProductRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: str
    name: str
    price: float
    original_price: float = 0.0
    discount_percent: float = 0.0
    sold_count: int = 0
    rating: float = 0.0
    review_count: int = 0
    shop_name: str = ""
    shop_rating: float = 0.0
    category: str
    subcategory: str = ""
    images: list[str] = Field(default_factory=list)
    product_url: str
    affiliate_link: str = ""
    crawled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trend_score: float = 0.0
    commission_rate: float = 0.0
    image_path: str | None = None
    notes: list[str] = Field(default_factory=list)


class GeneratedContent(BaseModel):
    title: str
    body: str
    hashtags: list[str] = Field(default_factory=list)
    cta: str
    image_path: str
    best_post_time: str
    target_account: str


class PerformanceMetrics(BaseModel):
    likes: int = 0
    comments: int = 0
    shares: int = 0
    reach: int = 0
    clicks: int = 0


class CommentRecord(BaseModel):
    id: str
    author: str = ""
    message: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    flagged: bool = False


class PostContent(BaseModel):
    title: str
    body: str
    hashtags: list[str] = Field(default_factory=list)
    cta: str
    affiliate_link: str


class PostRecord(BaseModel):
    post_id: str
    account: str
    fb_post_id: str | None = None
    status: Literal["draft", "scheduled", "published", "failed", "approved"] = "draft"
    product: ProductRecord
    content: PostContent
    image_path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scheduled_at: datetime | None = None
    published_at: datetime | None = None
    performance: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    comments: list[CommentRecord] = Field(default_factory=list)
    error_message: str | None = None


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    page_id: str = ""
    access_token: str = ""
    access_token_env: str | None = None
    token_expires_at: str = ""
    page_name: str = ""
    niche: str = ""
    tone: str = ""
    daily_post_limit: int = 7
    post_delay_minutes: int = 8
    auto_reply: bool = False
    status: Literal["active", "paused", "error"] = "active"
    timezone: str = "Asia/Saigon"

    def resolved_access_token(self) -> str | None:
        import os

        if self.access_token.strip():
            return self.access_token.strip()
        if self.access_token_env:
            return os.getenv(self.access_token_env)
        return None


class WindowSlot(BaseModel):
    name: str
    start: str
    end: str


class AgentRuntimeStatus(BaseModel):
    status: Literal["RUNNING", "SLEEPING", "IN_WINDOW", "PAUSED", "ERROR"] = "SLEEPING"
    current_phase: str = "boot"
    next_window_name: str | None = None
    next_window_at: datetime | None = None
    paused: bool = False
    message: str = ""
    proxy_health: dict[str, Any] = Field(default_factory=dict)
    account_health: dict[str, str] = Field(default_factory=dict)
    queue_stats: dict[str, int] = Field(default_factory=dict)
    today_posts: int = 0
    target_posts: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PostFilters(BaseModel):
    account: str | None = None
    category: str | None = None
    status: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 100


class ImprovementContext(BaseModel):
    watch_list_increase: list[str] = Field(default_factory=list)
    watch_list_decrease: list[str] = Field(default_factory=list)
    blacklist_products: list[str] = Field(default_factory=list)
    blacklist_keywords: list[str] = Field(default_factory=list)
    audience_insights: dict[str, Any] = Field(default_factory=dict)
    lessons: list[str] = Field(default_factory=list)
    long_term_insights: list[str] = Field(default_factory=list)


class DailyKPI(BaseModel):
    posts_published: int = 0
    target_posts: int = 0
    per_account: dict[str, int] = Field(default_factory=dict)
    clicks: int = 0
    comments: int = 0
    likes: int = 0


class WorkflowState(BaseModel):
    cycle_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    categories: list[str] = Field(default_factory=list)
    crawled_products: list[ProductRecord] = Field(default_factory=list)
    scored_products: list[ProductRecord] = Field(default_factory=list)
    drafted_posts: list[PostRecord] = Field(default_factory=list)
    scheduled_posts: list[PostRecord] = Field(default_factory=list)
    published_posts: list[PostRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class LogEvent(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: str
    module: str
    message: str
