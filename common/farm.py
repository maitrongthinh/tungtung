from __future__ import annotations

from pathlib import Path

from common.config import load_settings
from common.files import write_json
from common.models import PostRecord


class FarmManager:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.farm_dir = self.settings.farm_dir
        self.drafts_dir = self.farm_dir / "drafts"
        self.scheduled_dir = self.farm_dir / "scheduled"
        self.published_dir = self.farm_dir / "published"
        self.by_category_dir = self.published_dir / "by_category"
        self.by_date_dir = self.published_dir / "by_date"
        self.by_account_dir = self.published_dir / "by_account"
        for path in [
            self.drafts_dir,
            self.scheduled_dir,
            self.published_dir,
            self.by_category_dir,
            self.by_date_dir,
            self.by_account_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def save_draft(self, post: PostRecord) -> Path:
        path = self.drafts_dir / f"{post.post_id}.json"
        write_json(path, post.model_dump(mode="json"))
        return path

    def save_scheduled(self, post: PostRecord) -> Path:
        self._cleanup_old_status_files(post.post_id)
        path = self.scheduled_dir / f"{post.post_id}.json"
        write_json(path, post.model_dump(mode="json"))
        return path

    def save_published(self, post: PostRecord) -> list[Path]:
        self._cleanup_old_status_files(post.post_id)
        published_month = (post.published_at or post.created_at).strftime("%Y-%m")
        date_dir = self.by_date_dir / published_month
        category_dir = self.by_category_dir / self._slug(post.product.category)
        account_dir = self.by_account_dir / post.account
        canonical_dir = self.published_dir / post.post_id
        for path in [date_dir, category_dir, account_dir]:
            path.mkdir(parents=True, exist_ok=True)
        canonical_dir.mkdir(parents=True, exist_ok=True)
        payload = post.model_dump(mode="json")
        write_json(canonical_dir / "post.json", payload)
        write_json(canonical_dir / "comments.json", [comment.model_dump(mode="json") for comment in post.comments])
        paths = [
            self.published_dir / f"{post.post_id}.json",
            date_dir / f"{post.post_id}.json",
            category_dir / f"{post.post_id}.json",
            account_dir / f"{post.post_id}.json",
        ]
        for path in paths:
            write_json(path, payload)
        return paths

    def save_failed(self, post: PostRecord) -> Path:
        self._cleanup_old_status_files(post.post_id)
        failed_dir = self.farm_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        path = failed_dir / f"{post.post_id}.json"
        write_json(path, post.model_dump(mode="json"))
        return path

    def _cleanup_old_status_files(self, post_id: str) -> None:
        patterns = [
            self.drafts_dir / f"{post_id}.json",
            self.scheduled_dir / f"{post_id}.json",
            self.published_dir / f"{post_id}.json",
        ]
        for path in patterns:
            path.unlink(missing_ok=True)

    def cleanup_storage(self, *, asset_retention_days: int, temp_dir: Path, temp_retention_hours: int) -> dict[str, int]:
        from datetime import UTC, datetime, timedelta
        import shutil

        deleted_assets = 0
        deleted_temp = 0
        asset_cutoff = datetime.now(UTC) - timedelta(days=asset_retention_days)
        for directory in self.farm_dir.glob("assets/*"):
            if not directory.is_dir():
                continue
            modified = datetime.fromtimestamp(directory.stat().st_mtime, UTC)
            if modified < asset_cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                deleted_assets += 1

        temp_cutoff = datetime.now(UTC) - timedelta(hours=temp_retention_hours)
        if temp_dir.exists():
            for file in temp_dir.rglob("*"):
                if not file.is_file():
                    continue
                modified = datetime.fromtimestamp(file.stat().st_mtime, UTC)
                if modified < temp_cutoff:
                    file.unlink(missing_ok=True)
                    deleted_temp += 1
        return {"deleted_assets": deleted_assets, "deleted_temp_files": deleted_temp}

    def _slug(self, value: str) -> str:
        return (
            value.lower()
            .replace("/", "-")
            .replace(" ", "_")
            .replace("đ", "d")
            .replace("Đ", "d")
        )
