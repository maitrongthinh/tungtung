# Shopee Affiliate Agent - Quick Start

## 1. Cài đặt (copy-paste)

```bash
pip install -r requirements.txt
python main.py
```

**Xong!** Hệ thống sẽ tự động:
- Cài dependencies nếu thiếu
- Cài Playwright Chromium browser
- Tạo `.env` với password tự动生成
- Tạo cấu trúc thư mục
- Tạo account mẫu
- Tạo runtime config
- Khởi động dashboard + agent

## 2. Mở Dashboard

Mở trình duyệt: **http://localhost:8080**

Login bằng password hiện trong terminal khi khởi động.

## 3. Cấu hình (trên web)

Vào trang **Config** trên dashboard:

1. **AI API Key**: Dán OpenAI/Gemini key vào `integrations.ai_api_key`
2. **Shopee Cookie**: Copy cookie từ browser Shopee Affiliate
3. **Facebook Token**: Dán Page Access Token vào account `acc_001`
4. Bấm **Save**

## 4. Hoặc sửa trực tiếp file `.env`

```env
ANTHROPIC_API_KEY=sk-xxx          # OpenAI key
SHOPEE_AFFILIATE_TOKEN=xxx        # Shopee affiliate token
META_APP_ID=xxx                   # Facebook App ID
META_APP_SECRET=xxx               # Facebook App Secret
```

## 5. Chạy lại

```bash
python main.py
```

## Hệ thống tự động làm gì?

| Thời gian | Hoạt động |
|-----------|-----------|
| 06:00-10:30 | Crawl sản phẩm Shopee hot |
| 10:50 | Kiểm tra token trước window |
| 11:00-13:00 | **Đăng bài Window A** (mỗi 5 phút) |
| 13:00-19:00 | Tiếp tục crawl + tạo draft |
| 19:50 | Kiểm tra token trước window |
| 20:00-22:00 | **Đăng bài Window B** (mỗi 5 phút) |
| 22:15 | Tổng kết ngày + track revenue |
| 00:00 | Compact memory + cleanup |

## API Endpoints chính

- `GET /` - Dashboard
- `GET /posts` - Quản lý bài đăng
- `GET /chat` - Chat với AI
- `GET /config` - Cấu hình
- `GET /activity` - Nhật ký hoạt động
- `GET /api/revenue` - Doanh thu
- `GET /api/funnel` - Conversion funnel
- `POST /api/posts/manual` - Đăng bài thủ công

## Không cần Redis

Mặc định chạy local mode, không cần Redis.
Nếu muốn distributed mode, sửa `config.yaml`:
```yaml
runtime:
  execution_mode: "distributed"
```
