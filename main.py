import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import products, auth, orders, vendors, wallet, analytics, uploads, websockets, disputes, admin
from config import settings
from services.email import send_weekly_vendor_digest
from services.rate_limiter import RateLimitMiddleware, init_redis, close_redis
from services.websocket_manager import connection_manager
from database import supabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weekly digest scheduler
# ---------------------------------------------------------------------------

def _run_weekly_digest() -> None:
    """
    Fetch all active vendors and send each one their weekly digest.
    Runs every Monday at 08:00 WAT (07:00 UTC).
    """
    from datetime import datetime, timezone, timedelta
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    week_label = f"{start.strftime('%d')}\u2013{now.strftime('%d %b %Y')}"

    try:
        vendors_res = (
            supabase.table("profiles")
            .select("id, email, full_name")
            .eq("role", "vendor")
            .eq("status", "Active")
            .execute()
        )
        for vendor in (vendors_res.data or []):
            vid   = vendor["id"]
            # Revenue & order count for the week (vendor's share only)
            orders_res = (
                supabase.table("orders")
                .select("id, order_items!inner(quantity, unit_price_kobo, products!inner(vendor_id, name, stock))")
                .eq("order_items.products.vendor_id", vid)
                .gte("created_at", start.isoformat())
                .execute()
            )
            orders_data = orders_res.data or []
            revenue = 0
            units_sold = 0
            for order in orders_data:
                for item in (order.get("order_items") or []):
                    qty = item.get("quantity", 0)
                    price = item.get("unit_price_kobo", 0)
                    revenue += qty * price
                    units_sold += qty
            n_orders = len(orders_data)

            # Low-stock products
            low_res = (
                supabase.table("products")
                .select("name, stock")
                .eq("vendor_id", vid)
                .lt("stock", 50)
                .eq("status", "Approved")
                .execute()
            )
            low_products = low_res.data or []

            stats = {
                "week_label":        week_label,
                "revenue_kobo":      revenue,
                "orders_count":      n_orders,
                "units_sold":        units_sold,
                "avg_rating":        0.0,
                "top_product_name":  "\u2014",
                "top_product_units": 0,
                "low_stock_count":   len(low_products),
                "low_stock_names":   [p["name"] for p in low_products],
            }
            send_weekly_vendor_digest.delay(
                vendor["email"], vendor["full_name"], stats
            )
    except Exception as exc:
        logger.error(f"[digest] Weekly digest failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start APScheduler on app startup; shut it down cleanly on exit."""
    scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="UTC")
        # Every Monday at 07:00 UTC (08:00 WAT)
        scheduler.add_job(_run_weekly_digest, "cron", day_of_week="mon", hour=7, minute=0)
        scheduler.start()
        logger.info("[scheduler] Weekly digest cron started (Mon 07:00 UTC).")
    except ImportError:
        logger.warning("[scheduler] apscheduler not installed — weekly digest disabled.")

    pubsub_task = None
    try:
        await init_redis()
        if hasattr(connection_manager, "start_redis_listener"):
            import asyncio
            pubsub_task = asyncio.create_task(connection_manager.start_redis_listener())
    except Exception as exc:
        logger.warning(f"[rate_limiter] Redis initialization failed: {exc}")

    yield

    if pubsub_task:
        pubsub_task.cancel()
    if scheduler:
        scheduler.shutdown(wait=False)
    await close_redis()

app = FastAPI(
    title="Farmers Market API",
    version="2.1.0",
    description="Backend API for the Farm-Connect Farmers Market platform (Nigeria)",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow the Vite dev client and production frontend
# ---------------------------------------------------------------------------
cors_origins = [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RateLimitMiddleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)       # /auth/register, /auth/login, /auth/me
app.include_router(products.router)   # /products/ (CRUD + status + image)
app.include_router(orders.router)     # /orders/ (place, list, update status)
app.include_router(vendors.router)    # /vendors/ (list, profile, status)
app.include_router(wallet.router)     # /wallet/ (balance, topup, history)
app.include_router(analytics.router)  # /analytics/ (KPI, revenue, vendor rank)
app.include_router(uploads.router)    # /uploads/ (product image upload to Supabase Storage)
app.include_router(websockets.router) # /ws/orders/{order_id} (real-time order tracking & notifications)
app.include_router(disputes.router)   # /disputes/ (structured dispute audit trail & resolution)
app.include_router(admin.router)      # /admin/config (platform config persistence) & /admin/categories (CRUD)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/", tags=["health"])
def health_check():
    return {
        "status": "healthy",
        "service": "Farmers Market API",
        "version": "2.1.0",
        "docs": "/docs",
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
