from pathlib import Path

import pytest

from common.database import Database
from modules.shopee.affiliate_api import ShopeeAffiliateAPI


class FakeShopeeAffiliateAPI(ShopeeAffiliateAPI):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.settings.integrations.shopee_affiliate_token = "fake_token"
        self.calls = 0

    async def _graphql(self, query: str, variables: dict):
        self.calls += 1
        if "GenerateShortLink" in query:
            return {"generateShortLink": {"shortLink": "https://shope.ee/test123"}}
        if "ProductOfferV2" in query:
            return {
                "productOfferV2": {
                    "nodes": [
                        {
                            "productId": "1001",
                            "productName": "Ao khoac",
                            "commissionRate": "8%",
                            "price": 250000,
                            "priceMax": 350000,
                            "imageUrl": "https://example.com/image.jpg",
                            "offerLink": "https://shopee.vn/product",
                            "shopName": "Shop Test",
                            "soldCount": 140,
                            "ratingStar": 4.8,
                        }
                    ]
                }
            }
        return {"conversionReportV2": {"nodes": []}}


@pytest.mark.asyncio
async def test_generate_affiliate_link_uses_cache(tmp_path: Path) -> None:
    import os
    os.environ["SHOPEE_AFFILIATE_TOKEN"] = "fake_token"
    db = Database(tmp_path / "test.db")
    client = FakeShopeeAffiliateAPI(db)
    first = await client.generate_affiliate_link("https://shopee.vn/test")
    second = await client.generate_affiliate_link("https://shopee.vn/test")
    assert first == "https://shope.ee/test123"
    assert second == first
    assert client.calls == 1


@pytest.mark.asyncio
async def test_get_trending_products_maps_response(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    client = FakeShopeeAffiliateAPI(db)
    products = await client.get_trending_products("thời trang nữ", limit=5)
    assert len(products) == 1
    assert products[0].product_id == "1001"
    assert products[0].commission_rate == 8.0
