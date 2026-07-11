# Farmers Market API v2.1

Production-ready backend for the Farm-Connect Farmers Market platform (Nigeria).

## Quick Start

```bash
# 1. Create virtual environment
python -m venv venv
.\venv\Scripts\Activate  # Windows
# source venv/bin/activate  # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy environment file and configure
cp .env.example .env
# Edit .env with your credentials

# 4. Run the server
uvicorn main:app --reload
```

The server starts at `http://localhost:8000`.

## API Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## Environment Variables

Copy `.env.example` to `.env` and configure all required variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SUPABASE_URL` | Yes | — | Supabase project URL |
| `SUPABASE_KEY` | Yes | — | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | — | Supabase service role key (admin) |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection for rate limiting + pub/sub |
| `PAYSTACK_SECRET_KEY` | Yes | — | Paystack secret key |
| `RESEND_API_KEY` | No | — | Resend.ai API key (primary email) |
| `SENDGRID_API_KEY` | No | — | SendGrid API key (email fallback) |
| `GOOGLE_MAPS_API_KEY` | No | — | Google Maps for geocoding + ETA |
| `GOOGLE_CLIENT_ID` | No | — | Google OAuth client ID |
| `ENVIRONMENT` | No | `development` | `development` / `production` |

## Project Structure

```
fmlbackend/
├── main.py              # FastAPI app entry + CORS + lifespan
├── config.py            # Centralized settings (pydantic-settings)
├── database.py          # Supabase client (public + admin)
├── dependencies.py      # Auth dependencies (get_current_user, require_role)
├── celery_app.py        # Celery config
├── celery_worker.py     # Celery worker entry point
├── middleware/
│   └── auth.py          # JWT auth + role-based dependencies
├── models/              # SQLAlchemy ORM models (schema documentation)
├── schemas/             # Pydantic v2 request/response models
│   ├── auth.py, wallet.py, order.py, product.py
│   ├── vendor.py, admin.py, dispute.py
├── routers/             # FastAPI route handlers
│   ├── auth.py, products.py, orders.py, vendors.py
│   ├── wallet.py, analytics.py, admin.py
│   ├── disputes.py, uploads.py, websockets.py
├── services/            # Business logic + infrastructure
│   ├── auth_service.py, payment_service.py
│   ├── order_service.py, dispute_service.py
│   ├── email.py, payments.py, google_maps.py
│   ├── rate_limiter.py, websocket_manager.py
├── utils/
│   └── security.py      # Local JWT/password fallback
├── test/                # pytest test suite (123 tests)
├── docs/                # Architecture docs, API docs, OpenAPI spec
├── .env.example         # Template for environment variables
└── requirements.txt
```

## Key Conventions

- **Prices**: All in **kobo** (integer). Frontend divides by 100 for display (e.g., 150000 = ₦1,500).
- **Errors**: Always include `detail` field in error responses.
- **Auth**: Bearer JWT in `Authorization` header. Tokens from Supabase Auth.
- **PATCH**: Accept partial payloads (all fields optional).
- **Status enums**: Case-sensitive strings (`Processing`, `In Transit`, `Delivered`, `Cancelled`).
- **Upload field**: `"file"` in FormData for image uploads.

## API Endpoints

### Auth
- `POST /auth/register` — Register new user (role: customer|vendor)
- `POST /auth/login` — Login, returns Supabase session
- `GET /auth/me` — Current user profile
- `POST /auth/logout` — Sign out (Supabase)
- `POST /auth/send-otp` — Send OTP verification email
- `POST /auth/verify-otp` — Verify OTP code
- `POST /auth/google` — Google Sign-In

### Products
- `GET /products/` — List approved products (public, with filters)
- `GET /products/:id` — Get single product
- `POST /products/` — Create product (vendor/admin)
- `PATCH /products/:id` — Update product
- `DELETE /products/:id` — Delete product (returns 204)
- `PATCH /products/:id/image` — Update image URL
- `PATCH /products/:id/status` — Approve/reject (admin, sends email)

### Orders
- `GET /orders/` — List orders (role-filtered)
- `GET /orders/:id` — Get order
- `POST /orders/` — Place order (customer, with geocoding + email)
- `PATCH /orders/:id/status` — Update status (vendor/admin, stock restore on cancel)
- `GET /orders/detect-location` — Auto-detect location from IP
- `PATCH /orders/:id/assign-driver` — Assign driver to order
- `PATCH /orders/:id/location` — Driver GPS location update
- `GET /orders/:id/tracking` — Tracking snapshot with ETA

### Vendors
- `GET /vendors/` — List vendors (admin)
- `GET /vendors/:id` — Public vendor profile + products
- `GET /vendors/me/profile` — Own profile (vendor)
- `PATCH /vendors/me/profile` — Update own profile
- `PATCH /vendors/:id/status` — Activate/suspend (admin, sends email)
- `PATCH /vendors/:id` — Admin edit vendor (any field)

### Wallet
- `GET /wallet/balance` — Get balance
- `POST /wallet/topup` — Top up wallet (idempotent, duplicate reference check)
- `GET /wallet/history` — Transaction history (last 50)
- `POST /wallet/payout` — Request payout (vendor, bank check)
- `POST /wallet/paystack/init` — Initialize Paystack payment
- `POST /wallet/paystack/verify` — Verify Paystack payment
- `POST /wallet/paystack/webhook` — Paystack webhook (HMAC-signed)

### Admin
- `GET /admin/config` — Get platform configuration
- `PATCH /admin/config` — Update platform configuration
- `GET /admin/categories` — List product categories
- `POST /admin/categories` — Add category
- `DELETE /admin/categories` — Remove category

### Analytics
- `GET /analytics/` — Platform KPI summary
- `GET /analytics/revenue` — Revenue breakdown by period
- `GET /analytics/vendors` — Vendor performance ranking (by revenue)

### Disputes
- `POST /disputes/` — Open dispute
- `GET /disputes/` — List disputes
- `GET /disputes/:id` — Get dispute with notes/evidence
- `POST /disputes/:id/notes` — Add note
- `POST /disputes/:id/evidence` — Add evidence (file upload + URL)
- `PATCH /disputes/:id/resolve` — Resolve dispute (admin, automated wallet reconciliation)

### Uploads
- `POST /uploads/image` — Upload image (magic-byte validation, Supabase Storage)
- `DELETE /uploads/image` — Delete image (path-traversal protection)

### WebSockets
- `ws://localhost:8000/ws/orders/:order_id` — Live order tracking
  - Events: `connected`, `order_status_update`, `delivery_location_ping`, `vendor_order_alert`
- `ws://localhost:8000/ws/vendor/orders` — Vendor notifications

## Background Jobs

- **Email notifications**: 15 transactional email types via Celery (Resend → SendGrid → SMTP fallback)
- **Weekly vendor digest**: Every Monday 08:00 WAT via APScheduler
- **Rate limiting**: Redis-based sliding window (configurable via env vars)

## Testing

```bash
# Run all tests (no external dependencies)
python -m pytest

# Run with integration tests (requires Supabase)
RUN_INTEGRATION=1 python -m pytest
```

## Security Notes

- All prices in kobo (integer) to avoid floating-point issues
- Magic-byte validation on uploads prevents file spoofing
- Paystack webhook HMAC signature verification
- Path-traversal protection on file operations
- Rate limiting on all endpoints (Redis-backed)
- OTP codes with 10-minute expiry
