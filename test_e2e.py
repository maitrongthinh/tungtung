"""
End-to-end test: affiliate link → AI content → Facebook publish.
Skips Shopee crawl; uses a known product URL directly.
Run: python test_e2e.py
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from uuid import uuid4

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_settings
from common.database import Database
from common.models import AccountConfig, PostContent, PostRecord, ProductRecord
from modules.ai.writer import ContentWriter
from modules.meta.publisher import MetaPublisher
from modules.shopee.affiliate_api import ShopeeAffiliateAPI
from modules.shopee.cookie_affiliate import load_cookie_client_from_config

PRODUCT_URL = "https://shopee.vn/T%C3%A1i-Nghe-Bluetooth-5.0-TWS-M10-Tai-Nghe-Kh%C3%B4ng-D%C3%A2y-i.272808687.22196893940"
ACCOUNT_FILE = Path(__file__).resolve().parent / "accounts" / "acc_001.json"


async def main() -> None:
    settings = load_settings(refresh=True)
    db = Database(settings.sqlite_path)

    print("=" * 60)
    print("STEP 1: Cookie affiliate — generate link")
    print("=" * 60)
    cookie_client = load_cookie_client_from_config()
    if not cookie_client:
        print("ERROR: No cookie client configured. Check integrations.shopee_affiliate_cookie in config.yaml")
        return

    affiliate_api = ShopeeAffiliateAPI(db)
    affiliate_link = await affiliate_api.generate_affiliate_link(PRODUCT_URL)
    print(f"  Product URL : {PRODUCT_URL[:80]}")
    print(f"  Affiliate   : {affiliate_link}")
    if not affiliate_link or not affiliate_link.startswith("http"):
        print("ERROR: No affiliate link generated")
        return

    print("\n" + "=" * 60)
    print("STEP 2: AI — write post content")
    print("=" * 60)
    import json
    account_data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    account = AccountConfig.model_validate(account_data)
    print(f"  Account     : {account.page_name} ({account.id})")

    product = ProductRecord(
        product_id="22196893940",
        name="Tai Nghe Bluetooth 5.0 TWS M10",
        price=89000,
        original_price=150000,
        discount_percent=40,
        sold_count=5200,
        rating=4.7,
        review_count=312,
        shop_name="TechViet Store",
        category="do cong nghe gia re",
        product_url=PRODUCT_URL,
        affiliate_link=affiliate_link,
    )

    from modules.memory.improvement_updater import ImprovementUpdater
    improvement = ImprovementUpdater(db)
    improvement_ctx = improvement.load_context()

    writer = ContentWriter(database=db)
    generated = await writer.write_post(
        product,
        account,
        improvement_ctx,
        recent_posts=[],
        use_ai=True,
    )
    print(f"  Title       : {generated.title}")
    print(f"  Body (50ch) : {generated.body[:100]}...")
    print(f"  Hashtags    : {generated.hashtags}")
    print(f"  CTA         : {generated.cta[:100]}")

    print("\n" + "=" * 60)
    print("STEP 3: Facebook — publish post")
    print("=" * 60)
    post_id = str(uuid4())
    post = PostRecord(
        post_id=post_id,
        account=account.id,
        product=product,
        content=PostContent(
            title=generated.title,
            body=generated.body,
            hashtags=generated.hashtags,
            cta=generated.cta,
            affiliate_link=affiliate_link,
        ),
        image_path=generated.image_path or "",
        status="scheduled",
    )
    from datetime import UTC, datetime
    post.scheduled_at = datetime.now(UTC)
    db.upsert_post(post)

    publisher = MetaPublisher()
    try:
        fb_post_id = await publisher.publish_post(account, post)
        post.fb_post_id = fb_post_id
        post.status = "published"
        post.published_at = datetime.now(UTC)
        db.upsert_post(post)
        print(f"  SUCCESS! FB post ID: {fb_post_id}")
        print(f"  Post link: https://www.facebook.com/{fb_post_id}")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        post.status = "failed"
        post.error_message = str(exc)
        db.upsert_post(post)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
