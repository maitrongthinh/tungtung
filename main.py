from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import subprocess
import sys
from pathlib import Path
from threading import Thread

PROJECT_ROOT = Path(__file__).resolve().parent
BOOTSTRAP_ENV_FLAG = "SHOPEE_AGENT_BOOTSTRAPPED"


def _banner():
    print(r"""
  ____  _           _         ____
 / ___|| |__   ___ | |_ ___  / ___|  ___  _ __   ___
 \___ \| '_ \ / _ \| __/ _ \ \___ \ / _ \| '_ \ / _ \
  ___) | | | | (_) | || (_) | ___) | (_) | |_) |  __/
 |____/|_| |_|\___/ \__\___/ |____/ \___/| .__/ \___|
                                          |_|
  Shopee Affiliate x Facebook Agent
  Auto-pilot mode: crawl -> score -> write -> publish -> earn
    """)


def _check_python():
    if sys.version_info < (3, 11):
        print(f"[ERROR] Python 3.11+ required, you have {sys.version}")
        print("  Install: https://www.python.org/downloads/")
        sys.exit(1)


def _install_deps():
    """Auto install dependencies if missing."""
    required = ["fastapi", "uvicorn", "sse_starlette", "openai", "httpx", "pydantic"]
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if not missing:
        return
    print(f"[setup] Installing {len(missing)} missing packages...")
    req_file = PROJECT_ROOT / "requirements.txt"
    if req_file.exists():
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"])
    else:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *missing])


def _install_playwright():
    """Auto install Playwright chromium browser if available."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[info] Playwright not installed - crawler will use API-only mode")
        print("       To enable browser crawling: pip install playwright && playwright install chromium")
        return
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print("[info] Playwright browser install skipped (run manually if needed)")


def _ensure_env():
    """Auto create .env if missing."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        return
    secret = secrets.token_urlsafe(32)
    env_content = f"""# Shopee Affiliate Agent - Auto-generated config
# Edit these values to connect your services

# Dashboard password (auto-generated, change if you want)
SECRET_KEY={secret}

# AI API Key (get from https://platform.openai.com or compatible provider)
ANTHROPIC_API_KEY=
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini

# Shopee Affiliate (get from https://affiliate.shopee.vn)
SHOPEE_AFFILIATE_TOKEN=
SHOPEE_PUBLISHER_ID=

# Facebook Page Token (get from Graph API Explorer)
META_APP_ID=
META_APP_SECRET=

# Optional
REDIS_URL=redis://localhost:6379/0
PUBLIC_BASE_URL=
"""
    env_path.write_text(env_content, encoding="utf-8")
    print(f"[setup] Created .env with auto-generated SECRET_KEY")
    print(f"       Dashboard password: {secret}")


def _ensure_dirs():
    """Create all required directories."""
    dirs = ["accounts", "farm/drafts", "farm/scheduled", "farm/published", "farm/assets",
            "memory/snapshots", "memory/chroma_db", "data", "logs", "tmp"]
    for d in dirs:
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)


def _ensure_default_account():
    """Create a default account file if none exists."""
    accounts_dir = PROJECT_ROOT / "accounts"
    if list(accounts_dir.glob("acc_*.json")):
        return
    import json
    default = {
        "id": "acc_001",
        "page_id": "",
        "access_token": "",
        "token_expires_at": "",
        "page_name": "My Page",
        "niche": "thoi trang nu",
        "tone": "tu nhien, than thien, Gen Z",
        "daily_post_limit": 7,
        "post_delay_minutes": 8,
        "auto_reply": False,
        "status": "active",
        "timezone": "Asia/Saigon"
    }
    path = accounts_dir / "acc_001.json"
    path.write_text(json.dumps(default, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[setup] Created default account: accounts/acc_001.json")


def _ensure_runtime_config():
    """Create runtime config if missing."""
    config_path = PROJECT_ROOT / "memory" / "runtime_config.json"
    if config_path.exists():
        return
    import json
    secret = os.getenv("SECRET_KEY", "")
    config = {
        "runtime": {"execution_mode": "local", "local_queue_workers": 4},
        "web": {"host": "0.0.0.0", "port": 8080, "log_level": "info"},
        "shopee": {
            "rate_limit_per_second": 15,
            "min_product_score": 30,
            "max_products_per_cycle": 60,
            "crawler_headless": True,
            "affiliate_auth_mode": "bearer"
        },
        "meta": {
            "window_a_start": "11:00", "window_a_end": "13:00",
            "window_b_start": "20:00", "window_b_end": "22:00",
            "require_human_approval": False
        },
        "kpi": {"posts_per_day": 15, "posts_per_account": 7, "min_product_score": 30, "max_same_category": 3, "draft_buffer": 20},
        "ai": {
            "enabled": True, "provider": "openai", "model": "gpt-4o-mini",
            "max_daily_requests": 200, "max_daily_input_tokens": 1000000,
            "max_daily_output_tokens": 200000, "writer_max_tokens": 900
        },
        "integrations": {
            "redis_url": "redis://localhost:6379/0",
            "web_secret_key": secret,
        },
        "features": {
            "comment_monitoring_enabled": True,
            "download_assets": True,
            "fun_post_enabled": True,
            "fun_post_probability": 0.15
        }
    }
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[setup] Created runtime config: memory/runtime_config.json")


def _load_env():
    """Load .env file."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)


def _print_status():
    """Print startup status."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    
    has_ai = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("AI_API_KEY"))
    has_shopee = bool(os.getenv("SHOPEE_AFFILIATE_TOKEN"))
    has_meta_app = bool(os.getenv("META_APP_ID"))
    
    # Check accounts
    accounts_dir = PROJECT_ROOT / "accounts"
    acc_count = len(list(accounts_dir.glob("acc_*.json")))
    acc_with_token = 0
    import json
    for f in accounts_dir.glob("acc_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("access_token"):
                acc_with_token += 1
        except Exception:
            pass

    print("=" * 60)
    print("  SYSTEM STATUS")
    print("=" * 60)
    print(f"  AI API Key:      {'OK' if has_ai else 'MISSING - set ANTHROPIC_API_KEY in .env'}")
    print(f"  Shopee Token:    {'OK' if has_shopee else 'MISSING - set SHOPEE_AFFILIATE_TOKEN in .env'}")
    print(f"  Facebook App:    {'OK' if has_meta_app else 'MISSING - set META_APP_ID in .env'}")
    print(f"  Accounts:        {acc_count} configured, {acc_with_token} with token")
    print(f"  Secret Key:      {'OK' if os.getenv('SECRET_KEY') else 'MISSING'}")
    print("=" * 60)
    
    if not has_ai and not has_shopee:
        print("\n  TIP: Open dashboard and fill in API keys in Config page")
        print("       Or edit .env file directly")
    
    print(f"\n  Dashboard: http://localhost:8080")
    secret = os.getenv("SECRET_KEY", "")
    if secret:
        print(f"  Login password: {secret[:8]}... (from .env SECRET_KEY)")
    print()


# ── Auto-setup on import ──────────────────────────────────────
_banner()
_check_python()
_install_deps()
_install_playwright()
_load_env()
_ensure_env()
_load_env()  # Reload after creating .env
_ensure_dirs()
_ensure_default_account()
_ensure_runtime_config()

# Now import the actual application
from common.config import load_settings
from common.logging import get_logger, configure_logging
from core.bootstrap import build_runtime
from core.loop_controller import DailyLoopController
from core.scheduler import AgentScheduler
from web.main import app

import uvicorn


class WebServerThread(Thread):
    def __init__(self) -> None:
        super().__init__(name="web-server", daemon=True)
        settings = load_settings(refresh=True)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.web.host,
                port=settings.web.port,
                reload=False,
                log_level=settings.web.log_level,
            )
        )

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def main() -> None:
    _print_status()
    
    settings = load_settings(refresh=True)
    configure_logging(settings.log_dir)
    logger = get_logger(__name__)
    
    runtime = build_runtime()
    scheduler = AgentScheduler(
        database=runtime.database,
        session_manager=runtime.session_manager,
        proxy_pool=runtime.proxy_pool,
    )
    controller = DailyLoopController(runtime.database, scheduler)
    web_server = WebServerThread()
    web_server.start()
    
    logger.info("Agent started - dashboard at http://localhost:%s", settings.web.port)
    
    try:
        asyncio.run(controller.run_forever())
    finally:
        web_server.stop()
        web_server.join(timeout=15)
        logger.info("Agent stopped")


if __name__ == "__main__":
    main()
