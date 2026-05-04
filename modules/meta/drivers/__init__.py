"""Meta drivers package — exports all concrete driver classes + factory function.

Usage::

    from modules.meta.drivers import get_driver_for_account
    from modules.meta.drivers.base import MetaDriver
"""
from __future__ import annotations

from common.models import AccountConfig
from modules.meta.drivers.base import MetaDriver
from modules.meta.drivers.graph_api import GraphAPIDriver


def get_driver_for_account(account: AccountConfig) -> MetaDriver:
    """Factory: return the correct driver based on account.auth_mode."""
    mode = getattr(account, "auth_mode", "api")
    if mode == "cookie_page":
        from modules.meta.drivers.cookie_page import CookiePageDriver
        return CookiePageDriver()
    elif mode == "cookie_profile":
        from modules.meta.drivers.cookie_profile import CookieProfileDriver
        return CookieProfileDriver()
    return GraphAPIDriver()


__all__ = [
    "MetaDriver",
    "GraphAPIDriver",
    "get_driver_for_account",
]
