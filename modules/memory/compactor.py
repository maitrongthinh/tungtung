from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import chromadb
    _HAS_CHROMADB = True
except ImportError:
    _HAS_CHROMADB = False

from common.config import load_settings
from common.database import Database
from common.files import read_json, write_json
from common.logging import get_logger
from common.models import PostFilters, PostRecord

logger = get_logger(__name__)


class ContextCompactor:
    def __init__(self, database: Database) -> None:
        self.settings = load_settings()
        self.database = database
        self.snapshot_dir = Path(self.settings.memory_dir / "snapshots")
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        if _HAS_CHROMADB:
            self.client = chromadb.PersistentClient(path=str(self.settings.memory_dir / "chroma_db"))
            self.collection = self.client.get_or_create_collection(self.settings.memory.collection_name)
        else:
            self.client = None
            self.collection = None

    def compact_day(self, on_day: datetime | None = None) -> Path:
        day = (on_day or datetime.now(UTC)).astimezone(UTC)
        posts = self.database.list_posts(
            PostFilters(
                date_from=day.replace(hour=0, minute=0, second=0, microsecond=0),
                date_to=day.replace(hour=23, minute=59, second=59, microsecond=999999),
                limit=500,
            )
        )
        snapshot = self._snapshot_payload(day, posts)
        path = self.snapshot_dir / f"{day.date().isoformat()}.json"
        write_json(path, snapshot)
        self._persist_insights(snapshot)
        self._cleanup_old_snapshots()
        logger.info("Compacted memory snapshot for %s", day.date().isoformat())
        return path

    def query_insights(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search insights using keyword matching (lightweight TF-IDF-like)."""
        try:
            # Load all snapshots
            results: list[dict[str, Any]] = []
            query_words = set(query.lower().split())
            for path in sorted(self.snapshot_dir.glob("*.json"), reverse=True)[:30]:
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                    for post in snapshot.get("top_posts", []):
                        content = post.get("content", {}).get("body", "")
                        metadata = {
                            "date": snapshot.get("date", ""),
                            "account": post.get("account", ""),
                            "category": post.get("product", {}).get("category", ""),
                            "clicks": post.get("performance", {}).get("clicks", 0),
                        }
                        # Simple keyword relevance score
                        content_lower = content.lower()
                        score = sum(1 for word in query_words if word in content_lower)
                        if score > 0 or not query_words:
                            results.append({
                                "document": content[:500],
                                "metadata": metadata,
                                "relevance": score,
                            })
                except Exception:
                    continue
            # Sort by relevance then by clicks
            results.sort(key=lambda x: (x.get("relevance", 0), x.get("metadata", {}).get("clicks", 0)), reverse=True)
            return results[:limit]
        except Exception:
            return []

    def _snapshot_payload(self, day: datetime, posts: list[PostRecord]) -> dict[str, Any]:
        return {
            "date": day.date().isoformat(),
            "post_count": len(posts),
            "posts": [post.model_dump(mode="json") for post in posts],
            "top_posts": [
                post.model_dump(mode="json")
                for post in sorted(posts, key=lambda item: item.performance.clicks, reverse=True)[:10]
            ],
        }

    def _persist_insights(self, snapshot: dict[str, Any]) -> None:
        if not self.collection:
            return
        date_key = snapshot["date"]
        for index, post in enumerate(snapshot.get("top_posts", [])):
            content = post["content"]["body"]
            metadata = {
                "date": date_key,
                "account": post["account"],
                "category": post["product"]["category"],
                "clicks": post["performance"]["clicks"],
            }
            self.collection.upsert(
                ids=[f"{date_key}-{index}-{post['post_id']}"],
                documents=[content],
                metadatas=[metadata],
            )

    def _cleanup_old_snapshots(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.memory.snapshot_retention_days)
        for path in self.snapshot_dir.glob("*.json"):
            try:
                file_date = datetime.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                path.unlink(missing_ok=True)
