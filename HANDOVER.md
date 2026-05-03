# HANDOVER

## 1. Mục tiêu bàn giao

Bản bàn giao này tập trung vào một codebase vận hành được, có cấu trúc production-oriented, có test, có logging, có Docker Compose, có scheduler và có dashboard theo dõi. Những phần phụ thuộc 100% vào môi trường thật như token, App Review, quota, schema GraphQL thực tế của Shopee Affiliate và quyền Meta Page Publishing vẫn cần xác thực ở môi trường live.

## 2. Dòng chạy tổng quát

1. `agent` chạy `core.loop_controller`.
2. `APScheduler` trong agent enqueue job sang Redis/RQ.
3. `worker` nhận job và thực thi các cycle trong `core/tasks.py`.
4. `LangGraph` orchestrator xử lý các bước crawl, score, draft, schedule, publish, monitor, update memory, compact.
5. `web` đọc SQLite + file farm/memory và cho phép chỉnh runtime config/accounts trực tiếp.

## 2.1. Chế độ chạy đơn giản

Mặc định code hiện tại chạy với:

- `runtime.execution_mode = "local"`
- chỉ cần `python main.py`
- không cần Docker
- không cần Redis
- không cần worker riêng

Trong mode này:

- web server chạy cùng process với agent
- scheduler đẩy job vào local thread pool
- dashboard vẫn dùng đầy đủ command queue qua SQLite

## 3. Checklist trước khi bật production

- Điền `.env` tối thiểu nếu cần bootstrap nhanh lần đầu.
- Điền đúng `page_id` trong `accounts/acc_001.json`, `acc_002.json`, `acc_003.json`.
- Kiểm tra `token_expires_at` ban đầu.
- Kiểm tra `integrations.public_base_url` trỏ đúng domain public nếu dùng click tracking.
- Chạy `docker compose build`.
- Chạy `docker compose up -d`.
- Xem `docker compose ps`.
- Kiểm tra `GET /health` trả `200 OK`.
- Mở dashboard và xác nhận status, queue stats, account health.
- Kiểm tra dashboard đã hiện `Runtime Config` và `Accounts` editor.

Nếu chạy local:

- `py -3.11 -m pip install -e .[dev]`
- `py -3.11 -m playwright install chromium`
- `python main.py`

## 4. Runtime jobs hiện có

- `00:00` compact memory.
- `00:15` cleanup assets/temp/cache/processed commands.
- `06:00-10:30` prepare cycles mỗi 30 phút.
- `10:50` pre-window verify.
- `11:00-12:55` publish cycle mỗi 5 phút.
- `11:00-12:50` monitor cycle mỗi 10 phút.
- `13:00-19:00` prepare cycles mỗi giờ.
- `19:50` pre-window verify.
- `20:00-21:55` publish cycle mỗi 5 phút.
- `20:00-21:50` monitor cycle mỗi 10 phút.
- `22:15` wrap-up cycle.

## 5. Tracking click

Nếu `integrations.public_base_url` có giá trị:

- mỗi bài sẽ dùng link dạng `https://your-domain/r/<post_id>`;
- endpoint redirect sẽ tăng `performance.clicks` trong SQLite;
- chuyển tiếp 307 sang `product.affiliate_link` gốc.

Nếu không khai báo `integrations.public_base_url`:

- bài dùng trực tiếp link affiliate gốc;
- click nội bộ không được đếm.

## 6. Token refresh Meta

`MetaSessionManager.refresh_token_if_needed()` sẽ:

- gọi endpoint exchange token trước ngày hết hạn theo config;
- nếu nhận được token mới thì cập nhật `access_token` ngay trong file account JSON;
- cập nhật `token_expires_at` trong file account JSON;
- ghi log khi refresh thành công hoặc thất bại.

## 7. File/DB quan trọng

- `data/post_farm.db`: index, runtime state, affiliate cache, command queue.
- `memory/improvement.md`: memory rút gọn để writer/analyzer đọc lại.
- `memory/daily_plan.md`: kế hoạch ngày mới được regenerate định kỳ.
- `memory/runtime_config.json`: cấu hình runtime chính được sửa từ web.
- `memory/snapshots/*.json`: snapshot compact cuối ngày.
- `memory/chroma_db/`: insight dài hạn.
- `logs/agent.log`: log chính.
- `farm/published/<post_id>/post.json`: archive canonical của bài.
- `farm/published/<post_id>/comments.json`: archive comment của bài.

## 8. Những thứ cần xác thực live sau bàn giao

- Shopee Affiliate auth mode thật của tài khoản bạn: `bearer` hay `sha256`.
- Query/field thực tế trả về từ GraphQL của Shopee Affiliate tài khoản bạn.
- Quyền đăng bài Page qua Meta Graph API với app và token của bạn.
- Tính đúng của `scheduled_publish_time` nếu bạn muốn chuyển sang schedule trực tiếp qua Meta thay vì publish trong window.
- Tốc độ crawler thực tế trên VPS Ubuntu và mức chịu tải Playwright.

## 9. Những điểm đã được harden

- Retry khi gọi Shopee Affiliate API.
- Cache affiliate links 24 giờ.
- Cache output Claude cho scorer/writer.
- Budget ngày cho Claude theo requests + token.
- Cho phép đổi `AI API key + base URL + auth mode + extra headers` ngay trên web.
- Token bucket limiter.
- Proxy health check + degrade rate khi crawl lỗi.
- Queue tách scheduler và worker.
- Cleanup định kỳ.
- Auto-archive post/comment.
- Full-text search post index.
- Test pass cho limiter, affiliate API, writer, farm, links, database.

## 10. Cách debug nhanh khi lỗi

### Agent không chạy
- `docker compose logs -f agent`
- xem `logs/agent.log`
- kiểm tra `REDIS_URL`
- kiểm tra `integrations.redis_url` trong `Runtime Config`

### Worker không ăn job
- `docker compose logs -f worker`
- kiểm tra queue stats trên dashboard
- kiểm tra Redis health

### Web không hiện dữ liệu
- kiểm tra `data/post_farm.db` có mount đúng không
- kiểm tra `accounts/`, `memory/`, `data/`, `logs/` có mount ghi cho service `web`
- kiểm tra `GET /api/status`
- kiểm tra `GET /api/posts`

### Không đăng được Meta
- kiểm tra `account_health`
- kiểm tra `access_token` trong file account JSON
- kiểm tra permission của app/page
- xem log trả về từ Graph API

### AI không chạy
- kiểm tra `Runtime Config -> integrations.anthropic_api_key`
- kiểm tra `Runtime Config -> integrations.anthropic_base_url`
- nếu dùng gateway riêng, kiểm tra `anthropic_auth_mode`
- xem `GET /api/ai/usage`

### Click không tăng
- kiểm tra `integrations.public_base_url`
- kiểm tra domain public có trỏ về web service
- thử truy cập `GET /r/<post_id>` trực tiếp

## 11. Lệnh smoke test sau deploy

```bash
curl -i http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/api/status
curl http://127.0.0.1:8080/api/posts/counts
```

## 12. Giới hạn còn lại

- Chưa có integration test live vì không có credential thật trong workspace.
- Chưa có auto-reply nâng cao cho comment, mới ở mức optional simple reply theo keyword.
- Chưa có reverse proxy config đi kèm; nên thêm Nginx/Caddy ở môi trường production.
- Chưa có migration framework riêng; schema SQLite hiện bootstrap trực tiếp trong code.
