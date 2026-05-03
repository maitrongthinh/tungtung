# Shopee Affiliate x Facebook Agent

Một codebase Python 3.11+ để vận hành pipeline thu thập sản phẩm Shopee, tạo nội dung qua Claude, quản lý post farm trong SQLite + file JSON, điều phối lịch đăng trong 2 khung giờ bằng Meta Graph API, theo dõi comment, cập nhật `improvement.md`, lưu snapshot dài hạn với ChromaDB và hiển thị dashboard bằng FastAPI + Jinja2.

## Thành phần chính

- `LangGraph StateGraph` để điều phối chu kỳ `load_context -> crawl -> score -> draft -> schedule -> publish -> monitor -> improve -> compact`.
- `Playwright` crawler với rate limiter token bucket, proxy pool tùy chọn, downloader ảnh tối ưu JPEG.
- `ShopeeAffiliateAPI` có retry 3 lần, cache link 24 giờ trong SQLite và fallback link tracking nếu API lỗi.
- `MetaSessionManager` kiểm tra token trước window, auto-refresh token gần hết hạn và ghi lại token mới ngay trong file account JSON.
- `DailyPlanner` tạo `memory/daily_plan.md` cho chu kỳ ngày mới.
- `MetaPublisher` và `MetaMonitor` để đăng bài, lấy insight, theo dõi comment.
- `SQLite` cho post index + full-text search + runtime state + control commands.
- `ChromaDB` cho memory dài hạn và `memory/snapshots/*.json` cho compact cuối ngày.
- Dashboard `FastAPI + Jinja2 + SSE` dạng tối giản, có status, filter post, live log, editor cấu hình runtime và editor accounts.
- `Redis + RQ` để tách scheduler và worker jobs.

## Những gì đã hoàn thiện

- Scheduler theo ngày và hai cửa sổ Meta.
- Queue nền cho `crawl`, `publish`, `memory`.
- Pause, resume và force crawl từ dashboard qua command queue trong SQLite.
- Click tracking nội bộ qua redirect `GET /r/{post_id}` nếu khai báo `PUBLIC_BASE_URL`.
- Tự lưu archive published theo:
  - `farm/published/<post_id>/post.json`
  - `farm/published/<post_id>/comments.json`
  - `farm/published/by_date/YYYY-MM/`
  - `farm/published/by_category/<slug>/`
  - `farm/published/by_account/<acc_id>/`
- Cleanup định kỳ cho asset cũ, temp file, processed commands, expired affiliate cache.
- Unit tests đang pass.

## Cấu trúc thư mục

- `common/`: config, models, database, files, queue, farm manager, runtime helpers.
- `core/`: bootstrap, orchestrator, scheduler, loop controller, RQ tasks, worker.
- `modules/shopee/`: crawler, affiliate API, proxy pool, rate limiter.
- `modules/ai/`: Claude JSON client, analyzer, writer.
- `modules/meta/`: session manager, publisher, monitor.
- `modules/memory/`: improvement updater, compactor.
- `web/`: dashboard app, template, CSS.
- `accounts/`: cấu hình từng page.
- `farm/`: draft/scheduled/published/assets.
- `memory/`: `improvement.md`, `daily_plan.md`, `runtime_config.json`, snapshots, Chroma persistence.
- `data/`: SQLite database.
- `logs/`: file log runtime.

## Cấu hình vận hành

### Khuyến nghị

- Cấu hình vận hành hằng ngày nên sửa trực tiếp trên web tại `Runtime Config` và `Accounts`.
- `.env` chỉ còn vai trò fallback/bootstrap ban đầu khi chưa có `memory/runtime_config.json`.

### `memory/runtime_config.json`

File này được web tự sinh và là nguồn cấu hình runtime chính cho:

- `integrations.redis_url`
- `integrations.anthropic_api_key`
- `integrations.anthropic_base_url`
- `integrations.anthropic_version`
- `integrations.anthropic_auth_mode`
- `integrations.anthropic_extra_headers`
- `integrations.shopee_affiliate_token`
- `integrations.proxy_list`
- `integrations.public_base_url`
- `integrations.meta_app_id`
- `integrations.meta_app_secret`
- `ai.max_daily_requests`
- `ai.max_daily_input_tokens`
- `ai.max_daily_output_tokens`
- các khối `shopee`, `meta`, `kpi`, `loop`, `features`

Lưu ý:

- `storage` không được đưa lên editor runtime để tránh đổi nhầm đường dẫn DB/log khi hệ thống đang chạy.

### `.env` fallback

Nếu muốn bootstrap nhanh trước khi mở web, vẫn có thể điền:

- `REDIS_URL`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_VERSION`
- `ANTHROPIC_AUTH_MODE`
- `SHOPEE_AFFILIATE_TOKEN` hoặc `SHOPEE_AFFILIATE_CREDENTIAL` + `SHOPEE_AFFILIATE_SECRET`
- `SHOPEE_PUBLISHER_ID`
- `SECRET_KEY`
- `PUBLIC_BASE_URL`
- `META_APP_ID`
- `META_APP_SECRET`

### `accounts/*.json`

Mỗi account cần:

- `page_id`
- `access_token`
- `token_expires_at`
- `niche`
- `tone`
- `daily_post_limit`
- `post_delay_minutes`

### `config.yaml`

Các khối chính:

- `shopee`: rate limit, rotate proxy, timeout, buffer.
- `meta`: 2 cửa sổ đăng, version Graph API, refresh token, recent refresh window.
- `kpi`: posts/day, posts/account, min score, max same category.
- `memory`: compact time, context size, retention.
- `storage`: SQLite path, log dir, temp dir, retention.
- `ai`: budget ngày, giới hạn token, cache TTL, số lượt score/write bằng Claude mỗi cycle.
- `integrations`: redis, Shopee, Anthropic, Meta, proxy, public base URL.

### AI URL tùy chỉnh

Trong `Runtime Config`, bạn có thể sửa trực tiếp:

- `integrations.anthropic_api_key`
- `integrations.anthropic_base_url`
- `integrations.anthropic_version`
- `integrations.anthropic_auth_mode`
- `integrations.anthropic_extra_headers`
- `ai.model`

Mặc định hệ thống gọi API theo chuẩn `Anthropic-compatible Messages API`.

Ví dụ chuẩn gốc:

- `anthropic_base_url = "https://api.anthropic.com/v1"`
- hệ thống sẽ tự gọi tới `.../messages`

Nếu bạn dùng proxy/gateway tương thích Anthropic:

- có thể đổi `anthropic_base_url`
- nếu gateway yêu cầu `Authorization: Bearer ...` thì đặt `anthropic_auth_mode = "bearer"`
- nếu cần header riêng thì thêm vào `anthropic_extra_headers`

## Chạy nhanh không cần Docker

```bash
py -3.11 -m pip install -e .[dev]
py -3.11 -m playwright install chromium
python main.py
```

Mặc định:

- `runtime.execution_mode = "local"`
- không cần Redis
- không cần RQ worker riêng
- web dashboard và agent loop chạy chung trong một process
- nếu `python` trên máy đang trỏ sang bản khác nhưng có `py -3.11`, launcher sẽ tự chuyển sang `Python 3.11`

Sau khi chạy:

- mở `http://127.0.0.1:8080/`
- điền `Runtime Config`
- điền `Accounts`

Nếu muốn quay lại mô hình nhiều service:

- đổi `runtime.execution_mode = "distributed"`
- chạy lại theo Docker Compose hoặc tách `agent + worker + web`

## Chạy bằng Docker Compose trên Ubuntu VPS

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f agent
docker compose logs -f worker
docker compose logs -f web
```

Dashboard:

- `http://YOUR_HOST:8080/`
- `http://YOUR_HOST:8080/health`

## Endpoints

- `GET /health`
- `GET /`
- `GET /api/status`
- `GET /api/posts`
- `GET /api/posts/{id}`
- `GET /api/posts/{id}/image`
- `GET /api/posts/counts`
- `GET /api/kpi/today`
- `GET /api/improvement`
- `GET /api/daily-plan`
- `GET /api/runtime-config`
- `PUT /api/runtime-config`
- `GET /api/accounts-config`
- `PUT /api/accounts-config`
- `GET /api/ai/usage`
- `GET /api/logs/stream`
- `GET /api/logs/download`
- `GET /r/{post_id}`
- `POST /api/agent/pause`
- `POST /api/agent/resume`
- `POST /api/crawl/force`

## Kiểm thử

```bash
py -3.11 -m pytest -q
```

## Ghi chú production

- Nên đặt reverse proxy Nginx/Caddy trước dashboard.
- Nên bật HTTPS vì có endpoint redirect tracking.
- Nên giới hạn truy cập dashboard bằng VPN hoặc basic auth ở reverse proxy.
- `web` cần mount `accounts/`, `memory/`, `data/`, `logs/` dạng ghi để chỉnh runtime trực tiếp.
- `PUBLIC_BASE_URL` phải là domain public nếu muốn đếm click qua redirect nội bộ.
- Claude được giới hạn bằng budget ngày + cache, mặc định khá tiết kiệm để tránh tốn credit.
- Nếu chạy publish thật, cần xác nhận quyền Meta Pages API và App Review trước.
- Khi Shopee/Meta thay đổi schema hoặc permission, chỉnh config + auth và kiểm tra log.

## Handover

Chi tiết vận hành, checklist deploy và rủi ro còn lại được ghi thêm trong [HANDOVER.md](E:/agentshopee/HANDOVER.md).
