from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from common.database import Database
from common.logging import get_logger

logger = get_logger(__name__)


class WindowOptimizer:
    """Analyze historical data to suggest optimal posting windows."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def analyze_engagement_by_hour(self, days: int = 14) -> dict[int, dict[str, float]]:
        """Get average engagement metrics per hour of day."""
        since = datetime.now(UTC) - timedelta(days=days)
        from common.models import PostFilters
        posts = self.database.list_posts(PostFilters(date_from=since, status="published", limit=500))
        hour_data: dict[int, dict[str, list]] = {}
        for post in posts:
            if not post.published_at:
                continue
            hour = post.published_at.astimezone(UTC).hour
            if hour not in hour_data:
                hour_data[hour] = {"clicks": [], "likes": [], "comments": []}
            hour_data[hour]["clicks"].append(post.performance.clicks)
            hour_data[hour]["likes"].append(post.performance.likes)
            hour_data[hour]["comments"].append(post.performance.comments)
        result = {}
        for hour, data in hour_data.items():
            n = max(len(data["clicks"]), 1)
            result[hour] = {
                "avg_clicks": round(sum(data["clicks"]) / n, 1),
                "avg_likes": round(sum(data["likes"]) / n, 1),
                "avg_comments": round(sum(data["comments"]) / n, 1),
                "post_count": n,
                "engagement_score": round(
                    (sum(data["clicks"]) + sum(data["likes"]) * 0.5 + sum(data["comments"]) * 2) / n, 1
                ),
            }
        return result

    def suggest_windows(self) -> list[dict[str, Any]]:
        """Suggest 2 optimal posting windows based on data."""
        hourly = self.analyze_engagement_by_hour()
        if not hourly:
            return [
                {"start": "11:00", "end": "13:00", "reason": "Default lunch window"},
                {"start": "20:00", "end": "22:00", "reason": "Default evening window"},
            ]
        # Sort hours by engagement score
        ranked = sorted(hourly.items(), key=lambda x: x[1]["engagement_score"], reverse=True)
        windows = []
        used_hours: set[int] = set()
        for hour, stats in ranked:
            if hour in used_hours:
                continue
            # Find a 2-hour window around this hour
            start_h = max(0, hour - 1)
            end_h = min(23, hour + 1)
            windows.append({
                "start": f"{start_h:02d}:00",
                "end": f"{end_h:02d}:00",
                "avg_clicks": stats["avg_clicks"],
                "avg_engagement": stats["engagement_score"],
                "sample_size": stats["post_count"],
                "reason": f"Data-driven: {stats['post_count']} posts, {stats['avg_clicks']} avg clicks",
            })
            for h in range(start_h, end_h + 1):
                used_hours.add(h)
            if len(windows) >= 2:
                break
        return windows

    def get_slow_hours(self) -> list[int]:
        """Hours with lowest engagement - avoid posting during these."""
        hourly = self.analyze_engagement_by_hour()
        if not hourly:
            return [0, 1, 2, 3, 4, 5]
        ranked = sorted(hourly.items(), key=lambda x: x[1]["engagement_score"])
        return [h for h, _ in ranked[:6]]
