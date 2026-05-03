from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.config import load_settings
from common.database import Database
from common.files import atomic_write_text, load_accounts
from common.logging import get_logger
from modules.memory.improvement_updater import ImprovementUpdater

logger = get_logger(__name__)


class DailyPlanner:
    def __init__(self, database: Database) -> None:
        self.settings = load_settings()
        self.database = database
        self.improvement = ImprovementUpdater(database)
        self.path = Path(self.settings.memory_dir / "daily_plan.md")

    def generate(self, for_day: datetime | None = None) -> Path:
        day = (for_day or datetime.now(UTC)).astimezone(UTC)
        accounts = load_accounts(self.settings.accounts_dir)
        watch_list = self.improvement.category_watch_list()[:8]
        committed_today = self.database.count_committed_posts(day)
        remaining = max(0, self.settings.kpi.posts_per_day - committed_today)
        lines = [
            "# Daily Plan",
            f"Date: {day.date().isoformat()}",
            f"Generated at: {datetime.now(UTC).isoformat()}",
            "",
            "## KPI Targets",
            f"- Posts today target: {self.settings.kpi.posts_per_day}",
            f"- Already committed: {committed_today}",
            f"- Remaining today: {remaining}",
            f"- Draft buffer goal: {self.settings.kpi.draft_buffer}",
            "",
            "## Meta Windows",
            f"- Window A: {self.settings.meta.window_a_start} - {self.settings.meta.window_a_end}",
            f"- Window B: {self.settings.meta.window_b_start} - {self.settings.meta.window_b_end}",
            "",
            "## Category Watch List",
        ]
        for category in watch_list:
            lines.append(f"- {category}")
        lines.extend(["", "## Account Plan"])
        for account in accounts:
            lines.append(
                f"- {account.id} ({account.page_name}): niche={account.niche}, limit={account.daily_post_limit}, tone={account.tone}"
            )
        lines.extend(
            [
                "",
                "## Operational Notes",
                "- Crawl theo category watch list và ưu tiên sản phẩm chưa được schedule trong 3 ngày tới.",
                "- Nếu đủ KPI hôm nay, tiếp tục tạo queue cho 2 ngày tiếp theo nếu idle crawl bật.",
                "- Kiểm tra token và queue trước mỗi window.",
                "- Theo dõi comment và cập nhật improvement.md sau các window.",
            ]
        )
        atomic_write_text(self.path, "\n".join(lines) + "\n")
        logger.info("Generated daily plan at %s", self.path)
        return self.path
