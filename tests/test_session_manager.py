from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from common.models import AccountConfig, PostContent, PostRecord, ProductRecord
from modules.meta.session_manager import MetaSessionManager


def _account(account_id: str, delay: int) -> AccountConfig:
    return AccountConfig(
        id=account_id,
        page_id=account_id,
        access_token=f"token-{account_id}",
        token_expires_at="2026-12-31",
        page_name=account_id,
        niche="thời trang nữ",
        tone="thân thiện",
        daily_post_limit=2,
        post_delay_minutes=delay,
    )


def _post(post_id: str, account_id: str, product_id: str) -> PostRecord:
    product = ProductRecord(
        product_id=product_id,
        name=f"Product {product_id}",
        price=100000,
        category="thời trang nữ",
        product_url="https://shopee.vn/item",
        affiliate_link="https://shope.ee/raw",
    )
    return PostRecord(
        post_id=post_id,
        account=account_id,
        product=product,
        content=PostContent(title="T", body="B", hashtags=[], cta="CTA", affiliate_link="https://shope.ee/raw"),
        image_path="",
    )


def test_session_manager_staggers_accounts_and_respects_horizon(monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        meta=SimpleNamespace(
            window_a_start="11:00",
            window_a_end="13:00",
            window_b_start="20:00",
            window_b_end="22:00",
            verify_before_window_minutes=10,
            token_refresh_days_before_expiry=5,
            graph_api_version="v23.0",
        ),
        accounts_dir=None,
        integrations=SimpleNamespace(meta_app_id="", meta_app_secret=""),
    )
    monkeypatch.setattr("modules.meta.session_manager.load_settings", lambda *args, **kwargs: fake_settings)
    manager = MetaSessionManager()
    accounts = [_account("acc_001", 8), _account("acc_002", 10), _account("acc_003", 12)]
    posts = [
        _post("p1", "acc_001", "prod-1"),
        _post("p2", "acc_002", "prod-2"),
        _post("p3", "acc_003", "prod-3"),
        _post("p4", "acc_001", "prod-4"),
    ]
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc).astimezone()
    scheduled = manager.schedule_posts_for_windows(posts, accounts, now=now, horizon_days=1)
    assert len(scheduled) == 4
    first_round = sorted(scheduled[:3], key=lambda item: item.scheduled_at)
    assert first_round[0].account == "acc_001"
    assert first_round[1].scheduled_at > first_round[0].scheduled_at
    assert first_round[2].scheduled_at > first_round[1].scheduled_at
    assert len({post.product.product_id for post in scheduled}) == 4
