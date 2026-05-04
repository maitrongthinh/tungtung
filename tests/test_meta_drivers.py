"""Tests for the Facebook driver abstraction layer."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.models import AccountConfig, PostContent, PostRecord, ProductRecord
from modules.meta.drivers import get_driver_for_account
from modules.meta.drivers.base import MetaDriver
from modules.meta.drivers.graph_api import GraphAPIDriver
from modules.meta.drivers.cookie_page import CookiePageDriver
from modules.meta.drivers.cookie_profile import CookieProfileDriver
from modules.meta.drivers.cookie_utils import (
    build_cookie_header,
    extract_fb_dtsg,
    extract_jazoest,
    extract_post_id_from_html,
    extract_post_id_from_url,
    extract_user_id,
    parse_cookies,
    validate_essential_cookies,
)


# ── Fixtures ────────────────────────────────────────────────────

def _make_account(auth_mode: str = "api", **kwargs) -> AccountConfig:
    defaults = {
        "id": f"test_{auth_mode}",
        "page_id": "123456789",
        "access_token": "test_token" if auth_mode == "api" else "",
        "fb_cookies": 'c_user=999; xs=abc123; datr=xyz' if auth_mode != "api" else "",
        "fb_user_id": "999" if auth_mode == "cookie_profile" else "",
        "status": "active",
    }
    defaults.update(kwargs)
    return AccountConfig(auth_mode=auth_mode, **defaults)


def _make_post() -> PostRecord:
    product = ProductRecord(
        product_id="prod_001",
        name="Test Product",
        price=150000,
        category="thời trang nữ",
        product_url="https://shopee.vn/product-i.123.456",
        affiliate_link="https://s.shopee.vn/test123",
    )
    return PostRecord(
        post_id="post_001",
        account="test_acc",
        product=product,
        content=PostContent(
            title="Test Title",
            body="Test body content",
            hashtags=["#test", "#shopee"],
            cta="Mua ngay!",
            affiliate_link="https://s.shopee.vn/test123",
        ),
        image_path="",
    )


# ── Driver factory tests ───────────────────────────────────────

class TestDriverFactory:
    def test_api_mode_returns_graph_api(self):
        acc = _make_account("api")
        driver = get_driver_for_account(acc)
        assert isinstance(driver, GraphAPIDriver)

    def test_cookie_page_returns_cookie_page_driver(self):
        acc = _make_account("cookie_page")
        driver = get_driver_for_account(acc)
        assert isinstance(driver, CookiePageDriver)

    def test_cookie_profile_returns_cookie_profile_driver(self):
        acc = _make_account("cookie_profile")
        driver = get_driver_for_account(acc)
        assert isinstance(driver, CookieProfileDriver)

    def test_legacy_account_defaults_to_api(self):
        acc = AccountConfig(id="legacy", page_id="111", access_token="tok")
        driver = get_driver_for_account(acc)
        assert isinstance(driver, GraphAPIDriver)

    def test_all_drivers_extend_meta_driver(self):
        for mode in ("api", "cookie_page", "cookie_profile"):
            acc = _make_account(mode)
            driver = get_driver_for_account(acc)
            assert isinstance(driver, MetaDriver)


# ── Cookie parsing tests ───────────────────────────────────────

class TestCookieParsing:
    def test_parse_json_array_format(self):
        raw = '[{"name":"c_user","value":"123456"},{"name":"xs","value":"abc"}]'
        result = parse_cookies(raw)
        assert result == {"c_user": "123456", "xs": "abc"}

    def test_parse_semicolon_string_format(self):
        raw = "c_user=123456; xs=abc; datr=xyz"
        result = parse_cookies(raw)
        assert result == {"c_user": "123456", "xs": "abc", "datr": "xyz"}

    def test_parse_empty_string(self):
        assert parse_cookies("") == {}
        assert parse_cookies("   ") == {}

    def test_parse_json_with_extra_fields(self):
        raw = '[{"name":"c_user","value":"123","domain":".facebook.com","path":"/"}]'
        result = parse_cookies(raw)
        assert result == {"c_user": "123"}

    def test_parse_malformed_json_falls_back_to_semicolon(self):
        raw = "not_json; but=semicolon"
        result = parse_cookies(raw)
        assert result == {"but": "semicolon"}

    def test_build_cookie_header(self):
        cookies = {"c_user": "123", "xs": "abc"}
        header = build_cookie_header(cookies)
        assert "c_user=123" in header
        assert "xs=abc" in header
        assert "; " in header

    def test_validate_essential_cookies_valid(self):
        cookies = {"c_user": "123", "xs": "abc", "datr": "xyz"}
        valid, reason = validate_essential_cookies(cookies)
        assert valid is True
        assert reason == "ok"

    def test_validate_missing_c_user(self):
        cookies = {"xs": "abc"}
        valid, reason = validate_essential_cookies(cookies)
        assert valid is False
        assert "c_user" in reason

    def test_validate_missing_xs(self):
        cookies = {"c_user": "123"}
        valid, reason = validate_essential_cookies(cookies)
        assert valid is False
        assert "xs" in reason

    def test_validate_empty(self):
        valid, reason = validate_essential_cookies({})
        assert valid is False


# ── Token extraction tests ─────────────────────────────────────

class TestTokenExtraction:
    def test_extract_fb_dtsg_from_html(self):
        html = 'some html "DTSGInitialData":{"token":"abc123token"} more'
        assert extract_fb_dtsg(html) == "abc123token"

    def test_extract_fb_dtsg_from_input(self):
        html = '<input type="hidden" name="fb_dtsg" value="hidden_token_here">'
        assert extract_fb_dtsg(html) == "hidden_token_here"

    def test_extract_fb_dtsg_missing(self):
        assert extract_fb_dtsg("<html>no token</html>") is None

    def test_extract_jazoest(self):
        html = '<input type="hidden" name="jazoest" value="12345">'
        assert extract_jazoest(html) == "12345"

    def test_extract_jazoest_missing(self):
        assert extract_jazoest("<html></html>") is None

    def test_extract_user_id_from_html(self):
        html = 'data {"USER_ID":"1000123456"} more'
        assert extract_user_id(html) == "1000123456"

    def test_extract_user_id_via_actor_id(self):
        html = '"actorID":"987654321"'
        assert extract_user_id(html) == "987654321"


# ── Post ID extraction tests ───────────────────────────────────

class TestPostIdExtraction:
    def test_extract_from_story_fbid(self):
        url = "https://mbasic.facebook.com/story.php?story_fbid=123456&id=789"
        assert extract_post_id_from_url(url) == "123456"

    def test_extract_from_posts(self):
        url = "https://facebook.com/page/posts/789012"
        assert extract_post_id_from_url(url) == "789012"

    def test_extract_from_permalink(self):
        url = "https://facebook.com/permalink/345678"
        assert extract_post_id_from_url(url) == "345678"

    def test_extract_from_html_body(self):
        html = '<script>"post_id":"999888777"</script>'
        assert extract_post_id_from_html(html) == "999888777"

    def test_extract_missing(self):
        assert extract_post_id_from_url("https://facebook.com/home") is None


# ── Base driver compose message test ───────────────────────────

class TestBaseDriverHelpers:
    def test_compose_message(self):
        post = _make_post()
        # Use GraphAPIDriver as concrete subclass
        driver = GraphAPIDriver()
        msg = driver._compose_message(post)
        assert "Test Title" in msg
        assert "Test body" in msg
        assert "#test" in msg
        assert "Mua ngay!" in msg

    def test_should_use_link_field_for_shopee_short(self):
        driver = GraphAPIDriver()
        assert driver._should_use_link_field("https://s.shopee.vn/abc") is True
        assert driver._should_use_link_field("https://bit.ly/abc") is True
        assert driver._should_use_link_field("https://example.com/r/abc") is True
        assert driver._should_use_link_field("https://shopee.vn/product-i.123.456") is False
        assert driver._should_use_link_field("") is False


# ── AccountConfig model tests ──────────────────────────────────

class TestAccountConfigModel:
    def test_default_auth_mode_is_api(self):
        acc = AccountConfig(id="test")
        assert acc.auth_mode == "api"

    def test_cookie_page_mode(self):
        acc = AccountConfig(id="test", auth_mode="cookie_page", fb_cookies="c_user=1; xs=2")
        assert acc.auth_mode == "cookie_page"
        assert acc.fb_cookies == "c_user=1; xs=2"

    def test_cookie_profile_mode_with_user_id(self):
        acc = AccountConfig(id="test", auth_mode="cookie_profile", fb_cookies="c_user=1", fb_user_id="1")
        assert acc.auth_mode == "cookie_profile"
        assert acc.fb_user_id == "1"

    def test_backward_compat_old_account_json(self):
        """Old account JSON without auth_mode/fb_cookies should work."""
        import json
        old_data = json.loads('{"id":"old_acc","page_id":"123","access_token":"tok"}')
        acc = AccountConfig.model_validate(old_data)
        assert acc.auth_mode == "api"
        assert acc.fb_cookies == ""
        assert acc.fb_user_id == ""

    def test_graph_api_url_generation(self):
        driver = GraphAPIDriver()
        # _graph_url requires settings, test with direct call
        url = driver._graph_url("/123456/feed")
        assert "graph.facebook.com" in url
        assert "/123456/feed" in url
