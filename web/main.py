from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from common.ai import ai_budget_status
from common.config import load_settings, runtime_config_payload, save_runtime_config
from common.database import Database
from common.files import load_accounts_payload, save_account, save_accounts_bulk
from common.logging import configure_logging, get_logger
from common.models import AccountConfig, PostFilters
from modules.ai.client import OpenAIJSONClient, get_key_pool_stats

# ────────────────────────────────────────────────────────────────
# Security: Rate limiter
# ────────────────────────────────────────────────────────────────
_rate_store: dict[str, list[float]] = defaultdict(list)

def _check_rate_limit(key: str, max_requests: int = 30, window_seconds: int = 60) -> bool:
    """Return True if allowed, False if rate limited."""
    now = time.monotonic()
    bucket = _rate_store[key]
    # Prune old entries
    bucket[:] = [t for t in bucket if now - t < window_seconds]
    if len(bucket) >= max_requests:
        return False
    bucket.append(now)
    return True

# ────────────────────────────────────────────────────────────────
# Security: Auth helpers
# ────────────────────────────────────────────────────────────────
def _mask_secret(value: str) -> str:
    if not value or len(value) <= 8:
        return "***" if value else ""
    return value[:4] + "*" * (len(value) - 8) + value[-4:]

def _mask_config_secrets(payload: dict) -> dict:
    """Mask sensitive fields so API responses never leak full keys."""
    import copy
    out = copy.deepcopy(payload)
    integrations = out.get("integrations", {})
    secret_fields = [
        "ai_api_key", "shopee_affiliate_token", "shopee_affiliate_credential",
        "shopee_affiliate_secret", "shopee_affiliate_cookie", "meta_app_secret",
        "web_secret_key",
    ]
    for field in secret_fields:
        if field in integrations and integrations[field]:
            integrations[field] = _mask_secret(str(integrations[field]))
    # Mask ai_providers keys
    for provider in integrations.get("ai_providers", []):
        if isinstance(provider, dict) and provider.get("key"):
            provider["key"] = _mask_secret(str(provider["key"]))
    return out

async def _require_auth(request: Request) -> None:
    """Check Bearer token or X-API-Key header against web_secret_key."""
    settings = load_settings(refresh=True)
    expected = settings.integrations.web_secret_key
    if not expected or expected in ("replace-me", ""):
        # If no key configured, allow all (dev mode) but warn once
        return
    # Check Authorization: Bearer <key>
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, expected):
            return
    # Check X-API-Key header
    api_key = request.headers.get("x-api-key", "")
    if api_key and hmac.compare_digest(api_key, expected):
        return
    # Check cookie
    cookie_token = request.cookies.get("agent_token", "")
    if cookie_token and hmac.compare_digest(cookie_token, expected):
        return
    raise HTTPException(status_code=401, detail="Unauthorized — provide Bearer token or X-API-Key header")
settings = load_settings()
configure_logging(settings.log_dir)
logger = get_logger(__name__)
database = Database(settings.sqlite_path)
app = FastAPI(title="Shopee Affiliate Agent Dashboard")

# ── CORS middleware ─────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
LOG_FILE = settings.log_dir / "agent.log"
IMPROVEMENT_FILE = settings.memory_dir / "improvement.md"
DAILY_PLAN_FILE = settings.memory_dir / "daily_plan.md"


def current_settings():
    return load_settings(refresh=True)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    live_settings = current_settings()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "status": database.get_runtime_status().model_dump(mode="json"),
            "kpi": database.get_daily_kpi(datetime.now(UTC)),
            "posts": database.serialize_post_listing(database.list_posts(PostFilters(limit=50))),
            "counts": database.get_post_counts(),
            "runtime_config": runtime_config_payload(),
            "accounts_config": load_accounts_payload(live_settings.accounts_dir),
            "ai_usage": ai_budget_status(database, live_settings.ai),
        },
    )


@app.get("/posts", response_class=HTMLResponse)
async def posts_page(request: Request) -> HTMLResponse:
    counts = database.get_post_counts()
    return templates.TemplateResponse(request, "posts.html", {"counts": counts})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "chat.html", {})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request) -> HTMLResponse:
    live_settings = current_settings()
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "runtime_config": runtime_config_payload(),
            "accounts_config": load_accounts_payload(live_settings.accounts_dir),
        },
    )


@app.get("/health")
async def health() -> PlainTextResponse:
    return PlainTextResponse("OK", status_code=200)


@app.get("/api/status")
async def api_status() -> JSONResponse:
    payload = database.get_runtime_status().model_dump(mode="json")
    payload["post_counts"] = database.get_post_counts()
    payload["ai_usage"] = ai_budget_status(database, current_settings().ai)
    return JSONResponse(payload)


@app.get("/api/posts")
async def api_posts(
    account: str | None = None,
    category: str | None = None,
    status: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=100, le=500),
) -> JSONResponse:
    if q:
        posts = database.search_posts(q, limit=limit)
        return JSONResponse(database.serialize_post_listing(posts))
    filters = PostFilters(
        account=account,
        category=category,
        status=status,
        date_from=datetime.fromisoformat(date_from) if date_from else None,
        date_to=datetime.fromisoformat(date_to) if date_to else None,
        limit=limit,
    )
    posts = database.list_posts(filters)
    return JSONResponse(database.serialize_post_listing(posts))


@app.get("/api/posts/counts")
async def api_post_counts() -> JSONResponse:
    return JSONResponse(database.get_post_counts())


@app.get("/api/posts/{post_id}")
async def api_post_detail(post_id: str) -> JSONResponse:
    post = database.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return JSONResponse(post.model_dump(mode="json"))


@app.get("/api/posts/{post_id}/image")
async def api_post_image(post_id: str) -> FileResponse:
    post = database.get_post(post_id)
    if not post or not post.image_path:
        raise HTTPException(status_code=404, detail="Image not found")
    image_path = Path(post.image_path)
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(image_path)


@app.get("/api/kpi/today")
async def api_kpi_today() -> JSONResponse:
    return JSONResponse(database.get_daily_kpi(datetime.now(UTC)))




@app.get("/api/improvement")
async def api_improvement() -> PlainTextResponse:
    content = IMPROVEMENT_FILE.read_text(encoding="utf-8") if IMPROVEMENT_FILE.exists() else ""
    return PlainTextResponse(content, media_type="text/markdown")


@app.get("/api/daily-plan")
async def api_daily_plan() -> PlainTextResponse:
    content = DAILY_PLAN_FILE.read_text(encoding="utf-8") if DAILY_PLAN_FILE.exists() else ""
    return PlainTextResponse(content, media_type="text/markdown")


@app.get("/api/runtime-config")
async def api_runtime_config() -> JSONResponse:
    return JSONResponse(_mask_config_secrets(runtime_config_payload()))


@app.put("/api/runtime-config")
async def api_runtime_config_update(payload: dict, request: Request) -> JSONResponse:
    await _require_auth(request)
    if not _check_rate_limit("config_write", max_requests=10, window_seconds=60):
        raise HTTPException(429, "Rate limited")
    updated = save_runtime_config(payload)
    command_id = database.push_command("reload_settings")
    return JSONResponse({"saved": True, "command_id": command_id, "settings": _mask_config_secrets(updated.model_dump(mode="json"))})


@app.get("/api/accounts-config")
async def api_accounts_config() -> JSONResponse:
    return JSONResponse(load_accounts_payload(current_settings().accounts_dir))


@app.put("/api/accounts-config")
async def api_accounts_config_update(payload: list[dict], request: Request) -> JSONResponse:
    await _require_auth(request)
    if not _check_rate_limit("accounts_write", max_requests=10, window_seconds=60):
        raise HTTPException(429, "Rate limited")
    accounts = [AccountConfig.model_validate(item) for item in payload]
    paths = save_accounts_bulk(current_settings().accounts_dir, accounts)
    command_id = database.push_command("reload_settings")
    return JSONResponse({"saved": True, "count": len(paths), "command_id": command_id})


@app.post("/api/accounts")
async def api_accounts_add(payload: dict, request: Request) -> JSONResponse:
    await _require_auth(request)
    if not _check_rate_limit("account_add", max_requests=5, window_seconds=60):
        raise HTTPException(429, "Rate limited")
    try:
        account = AccountConfig.model_validate(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not account.id.startswith("acc_"):
        account.id = f"acc_{account.id}"
    save_account(current_settings().accounts_dir, account)
    command_id = database.push_command("reload_settings")
    return JSONResponse({"saved": True, "account_id": account.id, "command_id": command_id})


@app.delete("/api/accounts/{account_id}")
async def api_accounts_delete(account_id: str, request: Request) -> JSONResponse:
    await _require_auth(request)
    if not account_id.startswith("acc_"):
        account_id = f"acc_{account_id}"
    path = current_settings().accounts_dir / f"{account_id}.json"
    if path.exists():
        path.unlink()
    command_id = database.push_command("reload_settings")
    return JSONResponse({"deleted": True, "account_id": account_id, "command_id": command_id})


@app.get("/api/ai/usage")
async def api_ai_usage() -> JSONResponse:
    return JSONResponse(ai_budget_status(database, current_settings().ai))


@app.put("/api/shopee/cookie")
async def api_save_shopee_cookie(payload: dict, request: Request) -> JSONResponse:
    await _require_auth(request)
    if not _check_rate_limit("cookie_write", max_requests=5, window_seconds=60):
        raise HTTPException(429, "Rate limited")
    cookie = str(payload.get("cookie", "")).strip()
    if not cookie:
        return JSONResponse({"error": "cookie is required"}, status_code=400)
    live = current_settings()
    updated = save_runtime_config({
        "integrations": {
            **live.integrations.model_dump(mode="json"),
            "shopee_affiliate_cookie": cookie,
        }
    })
    command_id = database.push_command("reload_settings")
    return JSONResponse({"saved": True, "command_id": command_id})


@app.post("/api/shopee/cookie/validate")
async def api_validate_shopee_cookie() -> JSONResponse:
    from modules.shopee.cookie_affiliate import load_cookie_client_from_config
    client = load_cookie_client_from_config()
    if not client:
        return JSONResponse({"valid": False, "reason": "No cookie configured"})
    try:
        ok = await client.validate_cookie()
        return JSONResponse({"valid": ok, "reason": "ok" if ok else "cookie expired or invalid"})
    except Exception as exc:
        return JSONResponse({"valid": False, "reason": str(exc)})


@app.post("/api/shopee/link/generate")
async def api_generate_affiliate_link(payload: dict) -> JSONResponse:
    product_url = str(payload.get("url", "")).strip()
    if not product_url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    from modules.shopee.cookie_affiliate import load_cookie_client_from_config
    client = load_cookie_client_from_config()
    if not client:
        return JSONResponse({"error": "No Shopee affiliate cookie configured"}, status_code=503)
    try:
        link = await client.generate_link(product_url)
        if link:
            return JSONResponse({"short_link": link, "original_url": product_url})
        return JSONResponse({"error": "API returned no link (failCode set)"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/meta/exchange-token")
async def api_exchange_meta_token(payload: dict, request: Request) -> JSONResponse:
    """Exchange a Facebook User Access Token for a Page Access Token and update acc_001.json."""
    await _require_auth(request)
    if not _check_rate_limit("exchange_token", max_requests=3, window_seconds=60):
        raise HTTPException(429, "Rate limited")
    import httpx as _httpx
    user_token = str(payload.get("user_token", "")).strip()
    account_id = str(payload.get("account_id", "acc_001")).strip()
    if not user_token:
        return JSONResponse({"error": "user_token is required"}, status_code=400)
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://graph.facebook.com/v23.0/me/accounts",
                params={"access_token": user_token},
            )
            if r.status_code != 200:
                return JSONResponse({"error": f"FB API error: {r.text[:200]}"}, status_code=400)
            data = r.json()
            pages = data.get("data", [])
            if not pages:
                return JSONResponse({"error": "No pages found for this user token"}, status_code=400)
            page = pages[0]
            page_token = page.get("access_token", "")
            page_name = page.get("name", "")
            page_id = page.get("id", "")
            if not page_token:
                return JSONResponse({"error": "No page access_token in response"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    # Update account file
    live = current_settings()
    acc_path = live.accounts_dir / f"{account_id}.json"
    if not acc_path.exists():
        return JSONResponse({"error": f"Account {account_id} not found"}, status_code=404)
    try:
        import json as _json
        acc_data = _json.loads(acc_path.read_text(encoding="utf-8"))
        acc_data["access_token"] = page_token
        acc_data["token_expires_at"] = "2027-12-31"
        acc_data["page_name"] = page_name or acc_data.get("page_name", "")
        acc_data["page_id"] = page_id or acc_data.get("page_id", "")
        acc_path.write_text(_json.dumps(acc_data, indent=2, ensure_ascii=False), encoding="utf-8")
        database.push_command("reload_settings")
        return JSONResponse({"saved": True, "page_name": page_name, "page_id": page_id, "token_preview": page_token[:20] + "..."})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/chat")
async def api_chat(payload: dict, request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"chat:{ip}", max_requests=15, window_seconds=60):
        raise HTTPException(429, "Rate limited — max 15 requests/minute")
    message = str(payload.get("message", "")).strip()
    history = payload.get("history", [])
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    live = current_settings()
    kpi = database.get_daily_kpi(datetime.now(UTC))
    counts = database.get_post_counts()
    recent = database.serialize_post_listing(database.list_posts(PostFilters(status="published", limit=20)))
    system_prompt = (
        "Bạn là AI phân tích hiệu suất cho hệ thống đăng bài affiliate Shopee trên Facebook. "
        "Trả lời bằng tiếng Việt, ngắn gọn, thực tế. "
        f"Dữ liệu hôm nay: đã đăng {kpi.get('posts_published',0)} bài, "
        f"tổng clicks {kpi.get('clicks',0)}, likes {kpi.get('likes',0)}, comments {kpi.get('comments',0)}. "
        f"Tổng posts: draft={counts.get('draft',0)}, scheduled={counts.get('scheduled',0)}, published={counts.get('published',0)}. "
        f"20 bài gần nhất: {[{'title': p['content']['title'][:40], 'status': p['status'], 'clicks': p['performance']['clicks']} for p in recent[:5]]}."
    )
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=live.integrations.ai_api_key or "sk-dummy",
            base_url=live.integrations.ai_base_url or None,
            timeout=60.0,
        )
        response = await client.chat.completions.create(
            model=live.ai.model,
            messages=messages,
            max_tokens=600,
            temperature=0.3,
        )
        reply = response.choices[0].message.content or ""
        return JSONResponse({"reply": reply})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/shopee/validate-url")
async def api_validate_shopee_url(payload: dict) -> JSONResponse:
    import re
    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"valid": False, "reason": "URL trống"})
    if "shopee.vn" not in url:
        return JSONResponse({"valid": False, "reason": "Không phải URL Shopee"})
    pattern = r"shopee\.vn/.+-i\.(\d+)\.(\d+)"
    match = re.search(pattern, url)
    if match:
        shop_id, item_id = match.group(1), match.group(2)
        return JSONResponse({"valid": True, "shop_id": shop_id, "item_id": item_id, "reason": "URL hợp lệ"})
    pattern2 = r"shopee\.vn/product/(\d+)/(\d+)"
    match2 = re.search(pattern2, url)
    if match2:
        shop_id, item_id = match2.group(1), match2.group(2)
        return JSONResponse({"valid": True, "shop_id": shop_id, "item_id": item_id, "reason": "URL hợp lệ (format /product/)"})
    return JSONResponse({"valid": False, "reason": "URL Shopee không đúng định dạng. Cần có dạng .../ten-san-pham-i.shopid.itemid hoặc /product/shopid/itemid"})


@app.post("/api/posts/manual")
async def api_manual_post(payload: dict, request: Request) -> JSONResponse:
    """Crawl sản phẩm từ URL Shopee, tạo nội dung AI, đăng ngay lên Facebook với ảnh."""
    await _require_auth(request)
    if not _check_rate_limit("manual_post", max_requests=5, window_seconds=300):
        raise HTTPException(429, "Rate limited — max 5 manual posts per 5 minutes")
    import re, json, tempfile, random
    from uuid import uuid4
    from io import BytesIO
    from pathlib import Path as _Path
    import httpx
    from modules.shopee.cookie_affiliate import load_cookie_client_from_config
    from modules.ai.writer import ContentWriter
    from modules.meta.publisher import MetaPublisher
    from modules.meta.session_manager import MetaSessionManager
    from modules.memory.improvement_updater import ImprovementUpdater
    from common.farm import FarmManager
    from common.links import build_tracking_link
    from common.models import PostContent, PostRecord, ProductRecord

    product_url = str(payload.get("url", "")).strip()
    account_id = str(payload.get("account_id", "")).strip() or None
    if not product_url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    if "shopee.vn" not in product_url:
        return JSONResponse({"error": "Not a Shopee URL"}, status_code=400)

    live = current_settings()
    session_manager = MetaSessionManager()
    accounts = session_manager.load_accounts()
    if not accounts:
        return JSONResponse({"error": "No active accounts configured"}, status_code=503)
    account = next((a for a in accounts if a.id == account_id), accounts[0]) if account_id else accounts[0]

    # Extract IDs
    m = re.search(r"-i\.(\d+)\.(\d+)", product_url)
    if not m:
        m2 = re.search(r"shopee\.vn/product/(\d+)/(\d+)", product_url)
        if not m2:
            return JSONResponse({"error": "URL Shopee không đúng định dạng"}, status_code=400)
        shop_id, item_id = m2.group(1), m2.group(2)
        url_slug_name = ""
    else:
        shop_id, item_id = m.group(1), m.group(2)
        # Extract product name from URL slug (before -i.shopid.itemid)
        path_part = product_url.split("?")[0]
        slug_m = re.search(r"/([^/]+)-i\.\d+\.\d+", path_part)
        url_slug_name = slug_m.group(1).replace("-", " ").strip() if slug_m else ""
        # URL-decode if needed
        try:
            from urllib.parse import unquote
            url_slug_name = unquote(url_slug_name)
        except Exception:
            pass

    # Canonical URL for affiliate API (avoids failCode=2 from encoded slug URLs)
    canonical_url = f"https://shopee.vn/product/{shop_id}/{item_id}"

    # ── Step 1: Crawl product info via Playwright ────────────────────────
    product_name = url_slug_name.title() if url_slug_name else f"Sản phẩm Shopee #{item_id}"
    price = 0.0
    discount_percent = 0.0
    sold_count = 0
    rating = 0.0
    images: list[str] = []
    image_path_local: str = ""

    try:
        from playwright.async_api import async_playwright
        import re as _re
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    locale="vi-VN",
                    timezone_id="Asia/Ho_Chi_Minh",
                    viewport={"width": 1366, "height": 768},
                )
                await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

                # Inject Shopee cookies from config to bypass login redirect
                shopee_cookie_str = live.integrations.shopee_affiliate_cookie or ""
                if shopee_cookie_str:
                    cookies = []
                    try:
                        parsed = json.loads(shopee_cookie_str)
                        if isinstance(parsed, list):
                            # JSON format: [{"name":..., "value":..., "domain":...}]
                            for ck in parsed:
                                if isinstance(ck, dict) and ck.get("name") and ck.get("value") is not None:
                                    cookies.append({
                                        "name": str(ck["name"]),
                                        "value": str(ck["value"]),
                                        "domain": ck.get("domain") or ".shopee.vn",
                                        "path": ck.get("path") or "/",
                                    })
                    except (json.JSONDecodeError, ValueError):
                        # Plain key=value; format
                        for part in shopee_cookie_str.split(";"):
                            part = part.strip()
                            eq_idx = part.find("=")
                            if eq_idx > 0:
                                name = part[:eq_idx].strip()
                                value = part[eq_idx + 1:].strip()
                                if name:
                                    cookies.append({"name": name, "value": value, "domain": ".shopee.vn", "path": "/"})
                    if cookies:
                        try:
                            await context.add_cookies(cookies)
                        except Exception as ck_exc:
                            logger.warning("Cookie injection failed, continuing without cookies: %s", ck_exc)

                page = await context.new_page()
                # Navigate directly to product using canonical URL format
                await page.goto(canonical_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                details = await page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    const imgs = Array.from(document.querySelectorAll('img'))
                        .map(i => i.src || i.getAttribute('data-src') || '')
                        .filter(s => s && (s.includes('cf.shopee') || s.includes('down.shopee')) && s.includes('/file/'))
                        .slice(0, 8);
                    const ldJson = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                        .map(s => s.textContent || '');
                    const h1 = document.querySelector('h1');
                    return { text, imgs, ldJson, h1: h1 ? h1.innerText : '' };
                }
                """)
                text = details.get("text", "")
                imgs_raw = details.get("imgs", [])
                ld_json_list = details.get("ldJson", [])

                # Parse name from LD+JSON first, then H1, then URL slug
                for ld in ld_json_list:
                    try:
                        obj = json.loads(ld)
                        if isinstance(obj, dict) and obj.get("name") and obj.get("@type") == "Product":
                            product_name = obj["name"]
                            break
                    except Exception:
                        pass
                if product_name.startswith("Sản phẩm Shopee"):
                    h1_text = details.get("h1", "").strip()
                    if h1_text and len(h1_text) > 5:
                        product_name = h1_text

                # Parse price (VND amounts like 199.000đ or 199000đ)
                price_matches = _re.findall(r"([\d]{2,3}(?:[.,]\d{3})+|\d{5,})\s*(?:₫|đ)", text)
                if price_matches:
                    try:
                        price = float(price_matches[0].replace(".", "").replace(",", ""))
                    except ValueError:
                        pass

                # Parse discount
                disc_m = _re.search(r"(\d{1,2})%\s*(?:giảm|GIẢM|off|OFF)", text)
                if not disc_m:
                    disc_m = _re.search(r"-(\d{1,2})%", text)
                if disc_m:
                    discount_percent = float(disc_m.group(1))

                # Parse sold count
                sold_m = _re.search(r"(?:Đã bán|Da ban|sold)\s*([\d\.,]+)\s*k?", text, _re.IGNORECASE)
                if sold_m:
                    val = float(sold_m.group(1).replace(",", "."))
                    sold_count = int(val * 1000) if "k" in sold_m.group(0).lower() else int(val)

                # Rating
                rat_m = _re.search(r"(\d\.\d)\s*/\s*5", text)
                if rat_m:
                    rating = float(rat_m.group(1))

                images = list(dict.fromkeys(imgs_raw))[:5]  # deduplicate
                logger.info("Manual post crawl: name=%s imgs=%d price=%.0f", product_name[:40], len(images), price)
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("Playwright crawl failed for manual post %s: %s", item_id, exc)

    # ── Step 2: Use local asset if available, otherwise download ─────────
    # Check if product image was previously downloaded by crawler
    local_asset = live.farm_dir / "assets" / item_id / "cover.jpg"
    if local_asset.exists():
        image_path_local = str(local_asset)
        logger.info("Using local asset for manual post: %s", image_path_local)
    elif images:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                for img_url in images[:3]:
                    try:
                        r = await client.get(img_url)
                        if r.status_code == 200 and len(r.content) > 5000:
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                                tmp.write(r.content)
                                image_path_local = tmp.name
                            break
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning("Image download failed for manual post: %s", exc)

    # ── Step 3: Generate affiliate link (use canonical URL for API compatibility) ───
    affiliate_link = canonical_url
    # Check DB cache first (canonical URL, then original URL)
    cached_aff = database.get_cached_affiliate_link(canonical_url) or database.get_cached_affiliate_link(product_url)
    if cached_aff and cached_aff[0].startswith("https://s.shopee.vn"):
        affiliate_link = cached_aff[0]
        logger.info("Using cached affiliate link for manual post: %s", affiliate_link)
    else:
        try:
            from modules.shopee.affiliate_api import ShopeeAffiliateAPI
            aff_api = ShopeeAffiliateAPI(database=database)
            link = await aff_api.generate_affiliate_link(canonical_url)
            if link and link != canonical_url:
                affiliate_link = link
                logger.info("Generated short affiliate link for manual post: %s", link)
        except Exception as exc:
            logger.warning("Affiliate link generation failed for manual post: %s", exc)

    # ── Step 4: Build product record ────────────────────────────────────
    # Use unique cache key per manual post so AI always generates fresh content
    import time as _time
    product = ProductRecord(
        product_id=f"{item_id}_m{int(_time.time()) // 3600}",  # hourly cache bust
        name=product_name,
        price=price, original_price=price, discount_percent=discount_percent,
        sold_count=sold_count, rating=rating, review_count=0,
        shop_name="", shop_rating=0.0,
        category=account.niche or "shopee",
        subcategory="",
        images=images, product_url=canonical_url,
        affiliate_link=affiliate_link,
        trend_score=70.0,
    )

    # ── Step 5: AI content ──────────────────────────────────────────────
    writer = ContentWriter(database=database)
    improvement_updater = ImprovementUpdater(database)
    improvement = improvement_updater.load_context()
    try:
        generated = await writer.write_post(product, account, improvement, [], use_ai=live.ai.enabled)
    except Exception as exc:
        return JSONResponse({"error": f"AI content generation failed: {exc}"}, status_code=500)

    # Use downloaded image if writer didn't produce one
    final_image_path = generated.image_path or image_path_local

    # ── Step 6: Build post record ────────────────────────────────────────
    post_id = str(uuid4())
    tracked_link = build_tracking_link(post_id, affiliate_link)
    post = PostRecord(
        post_id=post_id,
        account=account.id,
        product=product,
        content=PostContent(
            title=generated.title,
            body=generated.body,
            hashtags=generated.hashtags,
            cta=generated.cta,
            affiliate_link=tracked_link,
        ),
        image_path=final_image_path,
        status="draft",
    )

    # ── Step 7: Publish ──────────────────────────────────────────────────
    publisher = MetaPublisher()
    try:
        fb_post_id = await publisher.publish_post(account, post)
        post.fb_post_id = fb_post_id
        post.status = "published"
        post.published_at = datetime.now(UTC)
        database.upsert_post(post)
        FarmManager().save_published(post)
        # Clean up temp image
        if image_path_local and _Path(image_path_local).exists():
            try:
                _Path(image_path_local).unlink()
            except Exception:
                pass
        fb_url = f"https://www.facebook.com/{fb_post_id.replace('_', '/posts/', 1)}"
        return JSONResponse({
            "success": True,
            "post_id": post_id,
            "fb_post_id": fb_post_id,
            "fb_url": fb_url,
            "affiliate_link": affiliate_link,
            "title": generated.title,
            "product_name": product_name,
            "has_image": bool(final_image_path),
        })
    except Exception as exc:
        post.status = "failed"
        post.error_message = str(exc)
        database.upsert_post(post)
        err_msg = str(exc)
        if "Session has expired" in err_msg or "OAuthException" in err_msg or "access token" in err_msg.lower():
            err_msg = "Facebook Page Token đã hết hạn. Vui lòng cung cấp User Token mới để đổi lấy Page Token."
        return JSONResponse({"error": err_msg}, status_code=500)


@app.post("/api/agent/pause")
async def pause_agent(request: Request) -> JSONResponse:
    await _require_auth(request)
    command_id = database.push_command("pause_agent")
    return JSONResponse({"queued": True, "command_id": command_id})


@app.post("/api/agent/resume")
async def resume_agent(request: Request) -> JSONResponse:
    await _require_auth(request)
    command_id = database.push_command("resume_agent")
    return JSONResponse({"queued": True, "command_id": command_id})


@app.post("/api/crawl/force")
async def force_crawl(request: Request) -> JSONResponse:
    await _require_auth(request)
    if not _check_rate_limit("force_crawl", max_requests=3, window_seconds=300):
        raise HTTPException(429, "Rate limited")
    command_id = database.push_command("force_crawl")
    return JSONResponse({"queued": True, "command_id": command_id})


@app.get("/api/ai/keys")
async def ai_key_stats() -> JSONResponse:
    """Trạng thái các AI key trong pool (available/blocked, provider, model)."""
    return JSONResponse(get_key_pool_stats())


@app.get("/api/revenue")
async def api_revenue() -> JSONResponse:
    """Revenue metrics: commission, orders, top earners."""
    now = datetime.now(UTC)
    # Get commission tracking events from activity log
    commission_events = database.get_activity_log(limit=100, event_type="commission_tracking")
    total_commission = 0.0
    total_orders = 0
    for event in commission_events:
        detail = event.get("detail", {})
        total_commission += detail.get("commission", 0.0)
        total_orders += detail.get("orders", 0)
    # Get published posts with click data for revenue estimation
    published = database.list_recent_published_posts(hours=72, limit=100)
    top_earners = []
    for post in sorted(published, key=lambda p: p.performance.clicks, reverse=True)[:10]:
        top_earners.append({
            "post_id": post.post_id,
            "product_name": post.product.name[:60],
            "category": post.product.category,
            "commission_rate": post.product.commission_rate,
            "clicks": post.performance.clicks,
            "likes": post.performance.likes,
            "comments": post.performance.comments,
            "published_at": post.published_at.isoformat() if post.published_at else None,
        })
    kpi = database.get_daily_kpi(now)
    # Estimate revenue: clicks * avg conversion (2%) * avg commission
    est_click_value = total_commission / max(total_orders, 1) if total_orders > 0 else 5000
    estimated_revenue = kpi.get("clicks", 0) * 0.02 * est_click_value
    return JSONResponse({
        "tracked_commission": total_commission,
        "tracked_orders": total_orders,
        "estimated_daily_revenue": round(estimated_revenue),
        "today_clicks": kpi.get("clicks", 0),
        "today_posts": kpi.get("posts_published", 0),
        "top_earners": top_earners,
        "avg_commission_per_order": round(total_commission / max(total_orders, 1)),
    })


@app.post("/api/proxy/health")
async def proxy_health_check() -> JSONResponse:
    from modules.shopee.proxy_pool import ProxyPool
    pool = ProxyPool()
    if not pool.proxies:
        return JSONResponse({"total": 0, "alive": 0, "failed": 0, "message": "Chưa có proxy nào được cấu hình"})
    results = await pool.health_check()
    alive = sum(1 for ok in results.values() if ok)
    return JSONResponse({"total": len(results), "alive": alive, "failed": len(results) - alive, "details": results})


@app.post("/api/proxy/refresh")
async def proxy_refresh() -> JSONResponse:
    """Scrape proxy public miễn phí, test, lưu vào pool. Mất 1-2 phút."""
    from modules.shopee.proxy_scraper import refresh_proxy_pool
    try:
        result = await refresh_proxy_pool(force=True)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "activity.html", {})


@app.get("/api/activity")
async def api_activity(
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0),
    event_type: str | None = None,
) -> JSONResponse:
    events = database.get_activity_log(limit=limit, offset=offset, event_type=event_type)
    return JSONResponse(events)


@app.get("/api/activity/stream")
async def activity_stream() -> EventSourceResponse:
    return EventSourceResponse(_activity_events())


async def _activity_events() -> AsyncGenerator[dict[str, str], None]:
    last_id = 0
    # Gửi history từ đầu (tối đa 500 event gần nhất)
    history = database.get_activity_log(limit=500)
    history.reverse()  # Gửi từ cũ → mới
    for event in history:
        import json as _json
        yield {"event": "activity", "data": _json.dumps(event, ensure_ascii=False)}
        last_id = max(last_id, event.get("id", 0))
    # Stream real-time events tiếp theo
    while True:
        new_events = database.get_activity_log(limit=50)
        new_events.reverse()
        for event in new_events:
            eid = event.get("id", 0)
            if eid > last_id:
                import json as _json
                yield {"event": "activity", "data": _json.dumps(event, ensure_ascii=False)}
                last_id = eid
        await asyncio.sleep(2)


@app.get("/api/logs/stream")
async def log_stream() -> EventSourceResponse:
    return EventSourceResponse(_log_events())


@app.get("/api/logs/download")
async def download_logs() -> FileResponse:
    if not LOG_FILE.exists():
        LOG_FILE.touch()
    return FileResponse(LOG_FILE, filename="agent.log", media_type="text/plain")


async def _log_events() -> AsyncGenerator[dict[str, str], None]:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.touch()
    with LOG_FILE.open("r", encoding="utf-8") as handle:
        handle.seek(0, 2)
        while True:
            line = handle.readline()
            if line:
                yield {"event": "log", "data": line.rstrip("\n")}
            else:
                await asyncio.sleep(1)


@app.get("/r/{post_id}")
async def redirect_post_click(post_id: str) -> RedirectResponse:
    post = database.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Tracking link not found")
    database.increment_post_clicks(post_id)
    destination = post.product.affiliate_link or post.content.affiliate_link
    return RedirectResponse(destination, status_code=307)


def main() -> None:
    import uvicorn

    live_settings = current_settings()
    uvicorn.run(
        "web.main:app",
        host=live_settings.web.host,
        port=live_settings.web.port,
        reload=False,
        log_level=live_settings.web.log_level,
    )


if __name__ == "__main__":
    main()
