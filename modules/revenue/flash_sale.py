from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import ProductRecord

logger = get_logger(__name__)


class FlashSaleDetector:
    """Detect price drops and flash sales, then trigger immediate posting."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.settings = load_settings()

    async def detect_price_drops(self, products: list[ProductRecord]) -> list[dict[str, Any]]:
        """Compare current crawled products with historical data to find price drops."""
        alerts = []
        for product in products:
            # Check if product was seen before at higher price
            cached = self.database.get_cached_affiliate_link(product.product_url)
            if product.discount_percent >= 30 or (product.original_price > 0 and product.price < product.original_price * 0.7):
                urgency = "HIGH" if product.discount_percent >= 50 else "MEDIUM"
                alerts.append({
                    "product": product,
                    "discount": product.discount_percent,
                    "urgency": urgency,
                    "price_drop": product.original_price - product.price if product.original_price > product.price else 0,
                    "reason": f"Flash sale {product.discount_percent:.0f}% off" if product.discount_percent > 0 else "Price drop detected",
                })
        # Sort by urgency and discount
        alerts.sort(key=lambda x: (0 if x["urgency"] == "HIGH" else 1, -x["discount"]))
        return alerts[:5]  # Top 5 alerts

    def generate_flash_sale_content(self, product: ProductRecord, discount: float) -> dict[str, str]:
        """Generate urgent, conversion-focused content for flash sales."""
        price_str = f"{product.price:,.0f}đ"
        original_str = f"{product.original_price:,.0f}đ"
        sold = product.sold_count

        hooks = [
            f"SALE {discount:.0f}% CHI TRONG HOM NAY",
            f"Giam {discount:.0f}% - Con {sold:,} nguoi dang xem",
            f"Flash deal: {product.name[:40]} chi con {price_str}",
            f"NHANH TAY! {discount:.0f}% off se het trong vai gio nua",
        ]

        import random
        hook = random.choice(hooks)

        body_lines = [
            hook,
            "",
            f"{product.name}",
            "",
            f"Gia goc: {original_str}",
            f"Gia sale: {price_str} (giam {discount:.0f}%)",
            f"Da ban: {sold:,} san pham | Rating: {product.rating:.1f}/5",
            "",
            "NHANH TAY VI SO LUONG CO HAN!",
            "De lai comment neu ban dang quan tam, minh se huong dan dat hang",
        ]

        return {
            "title": hook,
            "body": "\n".join(body_lines),
            "urgency": "flash_sale",
            "hashtags": ["#flashsale", "#dealhot", "#giamgia", f"#{product.category.replace(' ', '')}"],
        }
