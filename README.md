# Farmers Market API (fmlbackend)

A lightweight FastAPI backend for the Farm-Connect Farmers Market platform.

## What this repo contains

- FastAPI application entry: `main.py`
- Supabase helpers: `database.py`
- Routers for features: `routers/` (auth, products, orders, vendors, wallet, analytics, uploads, disputes, websockets, admin)
- Background workers: `celery_app.py`, `celery_worker.py`
- Tests: `test/` (unit + integration; integration tests are opt-in)

Useful docs:

 - API Reference: [API_DOCS.md](docs/API_DOCS.md)
 - Backend Architecture: [ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Quickstart (development)

1. Create and activate a virtual environment (Windows example):

```powershell
python -m venv venv
venv\Scripts\activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Add environment variables (see list below). You can copy from `.env` if present.

4. Run the app locally:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

API docs: http://localhost:8000/docs

---

## Environment variables

Required for normal operation:

- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_KEY` — public Supabase anon key
- `SUPABASE_SERVICE_ROLE_KEY` — service role key (admin operations)
- `REDIS_URL` — Redis connection for rate limiter and pub/sub (optional for local dev)
- `ADMIN_EMAIL` — platform admin email (notifications)

Put these in a `.env` file or your environment. Tests in this repo are arranged to run without external services by default; integration tests can be enabled (see Tests section).

---

## Tests

Run unit tests:

```bash
python -m pytest -q
```

Run integration tests (explicit opt-in):

```bash
# Unix/macOS
RUN_INTEGRATION=1 python -m pytest -q

# Windows (PowerShell)
$env:RUN_INTEGRATION='1'; python -m pytest -q
```

Notes:
- `pytest.ini` and `test/conftest.py` skip integration tests by default.

---

## Background workers

Start the Celery worker (example):

```bash
# from project root
celery -A celery_app.celery_app worker --loglevel=info
```

If you need a scheduler (beat) or separate worker, see `celery_worker.py`.

---

## CI suggestions

- Run `pip install -r requirements.txt` and `python -m pytest -q` in CI.
- Provide required env vars via CI secrets for integration tests.

---

If you'd like, I can add a `.env.example` and a GitHub Actions workflow to run tests automatically.