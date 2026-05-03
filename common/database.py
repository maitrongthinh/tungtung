from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from common.models import AgentRuntimeStatus, CommentRecord, PostFilters, PostRecord

_thread_local = threading.local()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(_thread_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA cache_size=-64000;")  # 64MB cache
            conn.execute("PRAGMA temp_store=MEMORY;")
            _thread_local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close_thread_connection(self) -> None:
        conn = getattr(_thread_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            _thread_local.conn = None

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    category TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    fb_post_id TEXT,
                    created_at TEXT NOT NULL,
                    scheduled_at TEXT,
                    published_at TEXT,
                    likes INTEGER NOT NULL DEFAULT 0,
                    comments INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    reach INTEGER NOT NULL DEFAULT 0,
                    clicks INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
                CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account);
                CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
                CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
                CREATE INDEX IF NOT EXISTS idx_posts_scheduled_at ON posts(scheduled_at);

                CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                    post_id UNINDEXED,
                    product_name,
                    content_text
                );

                CREATE TABLE IF NOT EXISTS affiliate_links (
                    product_url TEXT PRIMARY KEY,
                    affiliate_link TEXT NOT NULL,
                    link_id TEXT,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS control_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purpose TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage_events(created_at);

                CREATE TABLE IF NOT EXISTS ai_cache (
                    cache_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS replied_comments (
                    comment_id TEXT PRIMARY KEY,
                    post_id TEXT NOT NULL,
                    replied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    detail_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
                CREATE INDEX IF NOT EXISTS idx_activity_event_type ON activity_log(event_type);
                """
            )

    def upsert_post(self, post: PostRecord) -> None:
        payload_json = post.model_dump_json()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO posts (
                    post_id, account, status, category, product_name, fb_post_id,
                    created_at, scheduled_at, published_at, likes, comments, shares,
                    reach, clicks, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    account=excluded.account,
                    status=excluded.status,
                    category=excluded.category,
                    product_name=excluded.product_name,
                    fb_post_id=excluded.fb_post_id,
                    created_at=excluded.created_at,
                    scheduled_at=excluded.scheduled_at,
                    published_at=excluded.published_at,
                    likes=excluded.likes,
                    comments=excluded.comments,
                    shares=excluded.shares,
                    reach=excluded.reach,
                    clicks=excluded.clicks,
                    payload_json=excluded.payload_json
                """,
                (
                    post.post_id,
                    post.account,
                    post.status,
                    post.product.category,
                    post.product.name,
                    post.fb_post_id,
                    post.created_at.isoformat(),
                    post.scheduled_at.isoformat() if post.scheduled_at else None,
                    post.published_at.isoformat() if post.published_at else None,
                    post.performance.likes,
                    post.performance.comments,
                    post.performance.shares,
                    post.performance.reach,
                    post.performance.clicks,
                    payload_json,
                ),
            )
            conn.execute("DELETE FROM posts_fts WHERE post_id = ?", (post.post_id,))
            conn.execute(
                "INSERT INTO posts_fts (post_id, product_name, content_text) VALUES (?, ?, ?)",
                (post.post_id, post.product.name, f"{post.content.title}\n{post.content.body}\n{' '.join(post.content.hashtags)}"),
            )

    def update_post_status(
        self,
        post_id: str,
        status: str,
        *,
        fb_post_id: str | None = None,
        published_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        post = self.get_post(post_id)
        if not post:
            return
        post.status = status  # type: ignore[assignment]
        if fb_post_id:
            post.fb_post_id = fb_post_id
        if published_at:
            post.published_at = published_at
        if error_message:
            post.error_message = error_message
        self.upsert_post(post)

    def get_post(self, post_id: str) -> PostRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not row:
            return None
        return PostRecord.model_validate_json(row["payload_json"])

    def list_posts(self, filters: PostFilters) -> list[PostRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.account:
            clauses.append("account = ?")
            params.append(filters.account)
        if filters.category:
            clauses.append("category = ?")
            params.append(filters.category)
        if filters.status:
            clauses.append("status = ?")
            params.append(filters.status)
        if filters.date_from:
            clauses.append("created_at >= ?")
            params.append(filters.date_from.isoformat())
        if filters.date_to:
            clauses.append("created_at <= ?")
            params.append(filters.date_to.isoformat())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT payload_json FROM posts {where} ORDER BY created_at DESC LIMIT ?"
        params.append(filters.limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [PostRecord.model_validate_json(row["payload_json"]) for row in rows]

    def get_recent_post_texts(self, account: str, limit: int = 20) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM posts WHERE account = ? ORDER BY created_at DESC LIMIT ?",
                (account, limit),
            ).fetchall()
        results: list[str] = []
        for row in rows:
            post = PostRecord.model_validate_json(row["payload_json"])
            results.append(f"{post.content.title}\n{post.content.body}")
        return results

    def get_due_posts(self, before: datetime, limit: int = 20) -> list[PostRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM posts
                WHERE status IN ('scheduled', 'approved')
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT ?
                """,
                (before.isoformat(), limit),
            ).fetchall()
        return [PostRecord.model_validate_json(row["payload_json"]) for row in rows]

    def list_recent_published_posts(self, hours: int = 24, limit: int = 50) -> list[PostRecord]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM posts
                WHERE status = 'published'
                  AND published_at IS NOT NULL
                  AND published_at >= ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return [PostRecord.model_validate_json(row["payload_json"]) for row in rows]

    def count_committed_posts(self, day: datetime) -> int:
        day_start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total FROM posts
                WHERE (
                    (scheduled_at IS NOT NULL AND scheduled_at >= ? AND scheduled_at < ?)
                    OR
                    (published_at IS NOT NULL AND published_at >= ? AND published_at < ?)
                )
                AND status IN ('scheduled', 'approved', 'published')
                """,
                (day_start.isoformat(), day_end.isoformat(), day_start.isoformat(), day_end.isoformat()),
            ).fetchone()
        return int(row["total"]) if row else 0

    def get_account_post_totals(self, day: datetime) -> dict[str, int]:
        day_start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT account, COUNT(*) AS total
                FROM posts
                WHERE (
                    (scheduled_at IS NOT NULL AND scheduled_at >= ? AND scheduled_at < ?)
                    OR
                    (published_at IS NOT NULL AND published_at >= ? AND published_at < ?)
                )
                AND status IN ('scheduled', 'approved', 'published')
                GROUP BY account
                """,
                (day_start.isoformat(), day_end.isoformat(), day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        return {row["account"]: int(row["total"]) for row in rows}

    def get_account_category_usage(self, day: datetime) -> dict[tuple[str, str], int]:
        day_start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT account, category, COUNT(*) AS total
                FROM posts
                WHERE (
                    (scheduled_at IS NOT NULL AND scheduled_at >= ? AND scheduled_at < ?)
                    OR
                    (published_at IS NOT NULL AND published_at >= ? AND published_at < ?)
                    OR
                    (created_at IS NOT NULL AND created_at >= ? AND created_at < ?)
                )
                AND status IN ('draft', 'scheduled', 'approved', 'published')
                GROUP BY account, category
                """,
                (
                    day_start.isoformat(),
                    day_end.isoformat(),
                    day_start.isoformat(),
                    day_end.isoformat(),
                    day_start.isoformat(),
                    day_end.isoformat(),
                ),
            ).fetchall()
        return {(row["account"], row["category"]): int(row["total"]) for row in rows}

    def get_reserved_product_ids(self, date_from: datetime, date_to: datetime) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM posts
                WHERE status IN ('draft', 'scheduled', 'approved', 'published')
                  AND (
                    (created_at IS NOT NULL AND created_at >= ? AND created_at < ?)
                    OR
                    (scheduled_at IS NOT NULL AND scheduled_at >= ? AND scheduled_at < ?)
                    OR
                    (published_at IS NOT NULL AND published_at >= ? AND published_at < ?)
                  )
                """,
                (
                    date_from.isoformat(), date_to.isoformat(),
                    date_from.isoformat(), date_to.isoformat(),
                    date_from.isoformat(), date_to.isoformat(),
                ),
            ).fetchall()
        product_ids: set[str] = set()
        for row in rows:
            post = PostRecord.model_validate_json(row["payload_json"])
            product_ids.add(post.product.product_id)
        return product_ids

    def get_all_active_product_ids(self) -> set[str]:
        """Trả về product_id của tất cả bài draft/scheduled/approved — dùng để tránh trùng lặp."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM posts WHERE status IN ('draft','scheduled','approved')"
            ).fetchall()
        product_ids: set[str] = set()
        for row in rows:
            try:
                post = PostRecord.model_validate_json(row["payload_json"])
                product_ids.add(post.product.product_id)
            except Exception:
                pass
        return product_ids

    def log_activity(
        self,
        event_type: str,
        message: str,
        phase: str = "",
        detail: dict | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (ts, event_type, phase, message, detail_json) VALUES (?,?,?,?,?)",
                (now, event_type, phase, message, detail_json),
            )

    def get_activity_log(self, limit: int = 500, offset: int = 0, event_type: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT id, ts, event_type, phase, message, detail_json FROM activity_log WHERE event_type=? ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (event_type, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, event_type, phase, message, detail_json FROM activity_log ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        result = []
        for row in rows:
            entry = {
                "id": row["id"],
                "ts": row["ts"],
                "event_type": row["event_type"],
                "phase": row["phase"],
                "message": row["message"],
            }
            if row["detail_json"]:
                try:
                    entry["detail"] = json.loads(row["detail_json"])
                except Exception:
                    pass
            result.append(entry)
        return result

    def cache_affiliate_link(
        self,
        product_url: str,
        affiliate_link: str,
        *,
        ttl_hours: int,
        link_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=ttl_hours)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO affiliate_links (product_url, affiliate_link, link_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(product_url) DO UPDATE SET
                    affiliate_link=excluded.affiliate_link,
                    link_id=excluded.link_id,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (product_url, affiliate_link, link_id, expires_at.isoformat(), now.isoformat()),
            )

    def get_cached_affiliate_link(self, product_url: str) -> tuple[str, str | None] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT affiliate_link, link_id, expires_at FROM affiliate_links WHERE product_url = ?",
                (product_url,),
            ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return None
        return row["affiliate_link"], row["link_id"]

    def save_comments(self, post_id: str, comments: list[CommentRecord]) -> None:
        post = self.get_post(post_id)
        if not post:
            return
        post.comments = comments
        post.performance.comments = len(comments)
        self.upsert_post(post)

    def get_daily_kpi(self, day: datetime) -> dict[str, Any]:
        day_start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT account, COUNT(*) AS total, COALESCE(SUM(clicks), 0) AS clicks,
                       COALESCE(SUM(comments), 0) AS comments, COALESCE(SUM(likes), 0) AS likes
                FROM posts
                WHERE published_at >= ? AND published_at < ? AND status = 'published'
                GROUP BY account
                """,
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        summary = {
            "posts_published": sum(row["total"] for row in rows),
            "per_account": {row["account"]: row["total"] for row in rows},
            "clicks": sum(row["clicks"] for row in rows),
            "comments": sum(row["comments"] for row in rows),
            "likes": sum(row["likes"] for row in rows),
        }
        return summary

    def save_runtime_status(self, status: AgentRuntimeStatus) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                ("agent_status", status.model_dump_json(), datetime.now(UTC).isoformat()),
            )

    def get_runtime_status(self) -> AgentRuntimeStatus:
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM runtime_state WHERE key = 'agent_status'").fetchone()
        if not row:
            return AgentRuntimeStatus()
        return AgentRuntimeStatus.model_validate_json(row["value_json"])

    def push_command(self, command: str, payload: dict[str, Any] | None = None) -> int:
        created_at = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO control_commands (command, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (command, json.dumps(payload or {}), created_at),
            )
            return int(cursor.lastrowid)

    def fetch_pending_commands(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, command, payload_json, created_at
                FROM control_commands
                WHERE processed_at IS NULL
                ORDER BY created_at ASC
                """
            ).fetchall()
        commands: list[dict[str, Any]] = []
        for row in rows:
            commands.append(
                {
                    "id": int(row["id"]),
                    "command": row["command"],
                    "payload": json.loads(row["payload_json"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return commands

    def mark_command_processed(self, command_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE control_commands SET processed_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), command_id),
            )

    def purge_processed_commands(self, retention_days: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM control_commands WHERE processed_at IS NOT NULL AND processed_at < ?",
                (cutoff,),
            )

    def search_posts(self, query: str, limit: int = 20) -> list[PostRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT post_id FROM posts_fts WHERE posts_fts MATCH ? LIMIT ?",
                (query, limit),
            ).fetchall()
        post_ids = [row["post_id"] for row in rows]
        return [post for post_id in post_ids if (post := self.get_post(post_id))]

    def purge_expired_cache(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM affiliate_links WHERE expires_at < ?",
                (datetime.now(UTC).isoformat(),),
            )
            conn.execute(
                "DELETE FROM ai_cache WHERE expires_at < ?",
                (datetime.now(UTC).isoformat(),),
            )

    def serialize_post_listing(self, posts: list[PostRecord]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for post in posts:
            item = post.model_dump(mode="json")
            item["thumbnail"] = post.image_path
            payload.append(item)
        return payload

    def get_post_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM posts
                GROUP BY status
                """
            ).fetchall()
        counts = {"draft": 0, "scheduled": 0, "published": 0, "failed": 0, "approved": 0}
        for row in rows:
            counts[row["status"]] = int(row["total"])
        counts["all"] = sum(counts.values())
        return counts

    def increment_post_clicks(self, post_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE posts SET clicks = clicks + 1 WHERE post_id = ?",
                (post_id,),
            )
            row = conn.execute(
                "SELECT payload_json, clicks FROM posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            if not row:
                return
            post = PostRecord.model_validate_json(row["payload_json"])
            post.performance.clicks = int(row["clicks"])
            conn.execute(
                "UPDATE posts SET payload_json = ? WHERE post_id = ?",
                (post.model_dump_json(), post_id),
            )

    def has_replied_comment(self, comment_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM replied_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
        return row is not None

    def mark_comment_replied(self, comment_id: str, post_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO replied_comments (comment_id, post_id, replied_at)
                VALUES (?, ?, ?)
                ON CONFLICT(comment_id) DO NOTHING
                """,
                (comment_id, post_id, datetime.now(UTC).isoformat()),
            )

    def record_ai_usage(
        self,
        *,
        purpose: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        created_at: datetime | None = None,
    ) -> None:
        timestamp = (created_at or datetime.now(UTC)).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_usage_events (purpose, model, input_tokens, output_tokens, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (purpose, model, input_tokens, output_tokens, timestamp),
            )

    def get_ai_usage_summary(self, day: datetime | None = None) -> dict[str, Any]:
        target_day = day or datetime.now(UTC)
        day_start = target_day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self.connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM ai_usage_events
                WHERE created_at >= ? AND created_at < ?
                """,
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT purpose,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM ai_usage_events
                WHERE created_at >= ? AND created_at < ?
                GROUP BY purpose
                """,
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        by_purpose = {
            row["purpose"]: {
                "requests": int(row["requests"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
            }
            for row in rows
        }
        return {
            "requests": int(totals["requests"] or 0),
            "input_tokens": int(totals["input_tokens"] or 0),
            "output_tokens": int(totals["output_tokens"] or 0),
            "by_purpose": by_purpose,
        }

    def get_ai_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json, expires_at
                FROM ai_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return None
        return json.loads(row["payload_json"])

    def set_ai_cache(
        self,
        *,
        cache_key: str,
        kind: str,
        payload: dict[str, Any],
        ttl_hours: int,
    ) -> None:
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=ttl_hours)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_cache (cache_key, kind, payload_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    kind=excluded.kind,
                    payload_json=excluded.payload_json,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cache_key,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def vacuum_database(self) -> None:
        """Reclaim space and defragment the database. Call during low-traffic hours."""
        with self.connect() as conn:
            conn.execute("VACUUM")
            conn.execute("PRAGMA optimize")
        logger.info("Database VACUUM complete")

    def purge_old_activity_log(self, retention_days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM activity_log WHERE ts < ?",
                (cutoff,),
            )
            return cursor.rowcount or 0
