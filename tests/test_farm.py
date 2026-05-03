from pathlib import Path
from types import SimpleNamespace

from common.farm import FarmManager
from common.models import CommentRecord, PerformanceMetrics, PostContent, PostRecord, ProductRecord


def test_save_published_writes_post_and_comments(tmp_path: Path, monkeypatch) -> None:
    fake_settings = SimpleNamespace(farm_dir=tmp_path / "farm")
    monkeypatch.setattr("common.farm.load_settings", lambda: fake_settings)
    manager = FarmManager()
    product = ProductRecord(
        product_id="abc",
        name="Product",
        price=100000,
        category="đồ gia dụng",
        product_url="https://shopee.vn/item",
        affiliate_link="https://shope.ee/raw",
    )
    post = PostRecord(
        post_id="post-1",
        account="acc_001",
        status="published",
        product=product,
        content=PostContent(title="T", body="B", hashtags=[], cta="CTA", affiliate_link="https://example.com/r/post-1"),
        image_path="",
        comments=[CommentRecord(id="c1", message="xin giá")],
        performance=PerformanceMetrics(comments=1),
    )

    paths = manager.save_published(post)
    assert any(path.name == "post-1.json" for path in paths)
    canonical_dir = manager.published_dir / "post-1"
    assert (canonical_dir / "post.json").exists()
    assert (canonical_dir / "comments.json").exists()
