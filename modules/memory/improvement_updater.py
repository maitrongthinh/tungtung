from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.config import load_settings
from common.database import Database
from common.files import atomic_write_text
from common.logging import get_logger
from common.models import ImprovementContext, PostRecord

logger = get_logger(__name__)

DEFAULT_IMPROVEMENT = """# Agent Improvement Log
Last updated: never

## Trending Categories (auto-updated)
- thoi trang nu: 0 - Chua co du lieu

## Target Audience Insights
- Khung gio tot nhat: 11:00 - 13:00
- Content type hieu qua nhat: deal review
- Engagement triggers: hoi dap gia, so sanh nhanh, uu dai ro rang

## Lessons Learned
### Bootstrap
- Bat dau thu thap du lieu hieu suat de toi uu dan.

## Category Watch List
### Tang uu tien
- thoi trang nu: niche mac dinh
- gia dung thong minh: niche mac dinh
- do gia dung: niche mac dinh
- do cong nghe gia re: niche mac dinh
- my pham: niche mac dinh
### Giam uu tien
- khong co

## Blacklist
- Products: []
- Keywords: []

## Weekly Stats
- Tong bai dang: 0
- Avg engagement: 0
- Best performing category: N/A
- Affiliate clicks: 0
"""


class ImprovementUpdater:
    def __init__(self, database: Database) -> None:
        self.settings = load_settings()
        self.database = database
        self.path = Path(self.settings.memory_dir / "improvement.md")
        if not self.path.exists():
            atomic_write_text(self.path, DEFAULT_IMPROVEMENT)

    def load_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def load_context(self) -> ImprovementContext:
        text = self.load_text()
        increase = self._extract_section_items(text, "### Tang uu tien")
        decrease = self._extract_section_items(text, "### Giam uu tien")
        blacklist_products = self._extract_list_payload(text, "- Products:")
        blacklist_keywords = self._extract_list_payload(text, "- Keywords:")
        lessons = self._extract_section_items(text, "## Lessons Learned")
        return ImprovementContext(
            watch_list_increase=increase,
            watch_list_decrease=decrease,
            blacklist_products=blacklist_products,
            blacklist_keywords=blacklist_keywords,
            lessons=lessons,
            audience_insights={"raw_markdown": text},
        )

    def category_watch_list(self) -> list[str]:
        if self.settings.focus.enabled and self.settings.focus.focus_category:
            if self.settings.focus.started_at:
                try:
                    start = datetime.fromisoformat(self.settings.focus.started_at)
                    if (datetime.now(UTC) - start).days <= self.settings.focus.duration_days:
                        return [self.settings.focus.focus_category]
                except ValueError:
                    pass
            else:
                return [self.settings.focus.focus_category]
                
        context = self.load_context()
        return context.watch_list_increase or [
            "thoi trang nu",
            "gia dung thong minh",
            "do gia dung",
            "do cong nghe gia re",
            "my pham",
        ]

    def update(
        self,
        *,
        posts: list[PostRecord],
        top_categories: list[tuple[str, float]],
        audience_insights: dict[str, Any],
        blacklist_products: list[str],
        blacklist_keywords: list[str],
    ) -> None:
        now = datetime.now(UTC)
        total_posts = len(posts)
        total_engagement = sum(post.performance.likes + post.performance.comments + post.performance.shares for post in posts)
        avg_engagement = round(total_engagement / max(total_posts, 1), 2)
        best_category = top_categories[0][0] if top_categories else "N/A"
        clicks = sum(post.performance.clicks for post in posts)
        lines = [
            "# Agent Improvement Log",
            f"Last updated: {now.isoformat()}",
            "",
            "## Trending Categories (auto-updated)",
        ]
        if top_categories:
            for category, score in top_categories:
                lines.append(f"- {category}: {score:.2f} - Tu du lieu hien tai")
        else:
            lines.append("- chua co du lieu: 0 - Dang thu thap")
        lines.extend(
            [
                "",
                "## Target Audience Insights",
                f"- Khung gio tot nhat: {audience_insights.get('best_hours', '11:00 - 13:00')}",
                f"- Content type hieu qua nhat: {audience_insights.get('best_content_type', 'deal review')}",
                f"- Engagement triggers: {', '.join(audience_insights.get('triggers', ['hoi dap gia', 'uu dai ro rang']))}",
                "",
                "## Lessons Learned",
                f"### {now.date().isoformat()}",
            ]
        )
        if posts:
            for post in sorted(posts, key=lambda item: item.performance.clicks, reverse=True)[:5]:
                lines.append(
                    f"- Bai {post.post_id} ({post.product.category}) dat {post.performance.clicks} clicks, {post.performance.comments} comments."
                )
        else:
            lines.append("- Chua co bai dang moi de tong ket.")
        lines.extend(["", "## Category Watch List", "### Tang uu tien"])
        for category, _score in top_categories[:5] or [("thoi trang nu", 1.0)]:
            lines.append(f"- {category}: co dau hieu perform tot")
        lines.extend(["### Giam uu tien"])
        low_priority = [category for category in self.category_watch_list() if category not in {name for name, _ in top_categories[:5]}]
        for category in low_priority[:5] or ["khong co"]:
            lines.append(f"- {category}: hieu suat chua noi bat")
        lines.extend(
            [
                "",
                "## Blacklist",
                f"- Products: {blacklist_products}",
                f"- Keywords: {blacklist_keywords}",
                "",
                "## Weekly Stats",
                f"- Tong bai dang: {total_posts}",
                f"- Avg engagement: {avg_engagement}",
                f"- Best performing category: {best_category}",
                f"- Affiliate clicks: {clicks}",
            ]
        )
        atomic_write_text(self.path, "\n".join(lines) + "\n")
        logger.info("Updated improvement.md")

    def _extract_section_items(self, text: str, header: str) -> list[str]:
        if header not in text:
            return []
        section = text.split(header, 1)[1]
        lines: list[str] = []
        for line in section.splitlines()[1:]:
            if line.startswith("## ") or line.startswith("### "):
                break
            if line.strip().startswith("- "):
                lines.append(line.strip()[2:].split(":", 1)[0].strip())
        return [line for line in lines if line and line.lower() != "khong co"]

    def _extract_list_payload(self, text: str, prefix: str) -> list[str]:
        for line in text.splitlines():
            if line.startswith(prefix):
                raw = line.split(":", 1)[1].strip().strip("[]")
                if not raw:
                    return []
                return [item.strip().strip("'").strip('"') for item in raw.split(",") if item.strip()]
        return []
