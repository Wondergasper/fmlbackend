# Backend Architecture — Farmers Market API

Overview
- FastAPI application (`main.py`) that mounts routers: `auth`, `products`, `orders`, `vendors`, `wallet`, `analytics`, `uploads`, `websockets`, `disputes`, `admin`.
- Supabase is used as the primary Postgres + Auth + Storage provider via `database.supabase` (public) and `database.supabase_admin` (service role) clients.
- Celery is used for background tasks (email notifications etc.).
- APScheduler runs scheduled jobs (weekly vendor digest).
- Redis is used for rate limiting (`services.rate_limiter`) and pub/sub for WebSocket notifications.
- `services.websocket_manager` maintains WebSocket connections and broadcasts.

Core modules
- `main.py`: App bootstrapping, CORS, middleware, router registration, lifespan hooks (Redis init, scheduler).
- `database.py`: Supabase client initialisation (public and admin clients).
- `dependencies.py`: Auth helpers (`get_current_user`) and `require_role` enforcing role-based access.
- `services/email.py`: Celery tasks for sending emails. Tasks are invoked with `.delay(...)`.
- `services/rate_limiter.py`: Redis-backed sliding window rate limiter middleware.

Data flow (example: placing an order)
1. Client authenticates, calls `POST /orders` with order items.
2. `orders.place_order` verifies product availability via `supabase_admin.table('products')` and customer wallet via `supabase.table('profiles')`.
3. If valid, creates `orders` and `order_items` using `supabase_admin`, deducts wallet balance, and enqueues notification tasks (emails) via Celery.
4. Notifies vendors via `connection_manager.broadcast_vendor_alert` for real-time updates.

Environment variables (important)
- SUPABASE_URL — Supabase project URL
- SUPABASE_KEY — Supabase anon/public API key
- SUPABASE_SERVICE_ROLE_KEY — Supabase service role key for admin operations
- REDIS_URL — Redis connection URL (rate limiter + pubsub)
- ADMIN_EMAIL — platform admin contact for notifications
- CELERY_BROKER_URL / CELERY_RESULT_BACKEND — Celery configuration

Testing & Local setup
- Unit tests are in `test/`. Integration tests are controlled by `RUN_INTEGRATION=1` (see `test/conftest.py`).
- Use the included `pytest.ini` to run tests.

Deployment notes
- Run Uvicorn or Gunicorn with the FastAPI app. Ensure `SUPABASE_SERVICE_ROLE_KEY` is secured and not committed.
- Run a Celery worker for background tasks: `celery -A celery_app worker -Q default -l info` (project-specific command may vary).
- Ensure Redis and Supabase credentials are available in the environment or a secret manager.

Recommendations
- Add `.env.example` documenting env vars (I can add this file if you want).
- Add CI job to run unit tests on PRs and optionally run integration tests with secrets provided.
- Consider lazy-initializing database clients to make import-time safe for tooling.
