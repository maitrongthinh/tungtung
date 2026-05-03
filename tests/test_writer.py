import pytest

from common.models import AccountConfig, ImprovementContext, ProductRecord
from modules.ai.writer import ContentWriter


class FakeWriterClient:
    async def generate_json(self, **kwargs):
        return {
            "title": "Deal xinh cho mùa này",
            "body": "Mẫu này đang giảm mạnh, phối đồ cũng dễ và nhìn rất gọn.",
            "hashtags": ["#thoitrangnu", "#dealhot"],
            "cta": "Xem link mình để ở dưới nhé",
            "best_post_time": "11:15",
            "target_account": "acc_001",
        }


@pytest.mark.asyncio
async def test_content_writer_normalizes_payload() -> None:
    writer = ContentWriter(client=FakeWriterClient())
    product = ProductRecord(
        product_id="1",
        name="Áo len mỏng",
        price=199000,
        original_price=299000,
        discount_percent=33,
        sold_count=500,
        rating=4.9,
        review_count=120,
        shop_name="Shop X",
        shop_rating=4.8,
        category="thời trang nữ",
        product_url="https://shopee.vn/item",
        affiliate_link="https://shope.ee/abc",
        image_path="farm/assets/1/cover.jpg",
    )
    account = AccountConfig(
        id="acc_001",
        page_id="1",
        access_token="token",
        token_expires_at="2026-12-31",
        page_name="Test Page",
        niche="thời trang nữ",
        tone="thân thiện",
    )
    result = await writer.write_post(product, account, ImprovementContext(), [])
    assert result.target_account == "acc_001"
    assert "https://shope.ee/abc" in result.cta
    assert result.hashtags == ["#thoitrangnu", "#dealhot"]
