from datetime import UTC, datetime
from pathlib import Path

from common.database import Database
from common.models import PerformanceMetrics, PostContent, PostRecord, ProductRecord


def _sample_post(post_id: str, *, status: str = "scheduled") -> PostRecord:
    now = datetime.now(UTC)
    product = ProductRecord(
        product_id=f"p-{post_id}",
        name="Sample Product",
        price=100000,
        category="thời trang nữ",
        product_url="https://shopee.vn/item",
        affiliate_link="https://shope.ee/raw",
    )
    return PostRecord(
        post_id=post_id,
        account="acc_001",
        status=status,
        product=product,
        content=PostContent(
            title="Title",
            body="Body",
            hashtags=["#deal"],
            cta="CTA",
            affiliate_link="https://example.com/r/abc",
        ),
        image_path="",
        scheduled_at=now,
        published_at=now if status == "published" else None,
        performance=PerformanceMetrics(clicks=0),
    )


def test_database_counts_and_click_increment(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    draft = _sample_post("1", status="draft")
    published = _sample_post("2", status="published")
    db.upsert_post(draft)
    db.upsert_post(published)

    counts = db.get_post_counts()
    assert counts["draft"] == 1
    assert counts["published"] == 1

    db.increment_post_clicks("2")
    post = db.get_post("2")
    assert post is not None
    assert post.performance.clicks == 1
