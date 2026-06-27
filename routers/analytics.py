"""
analytics.py — Admin platform analytics routes for Farmers Market API

Endpoints:
  GET /analytics/           — Platform KPI summary (admin only)
  GET /analytics/revenue    — Revenue breakdown by period
  GET /analytics/vendors    — Vendor performance ranking
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Optional
from dependencies import require_role
from database import supabase

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def get_platform_summary(user=Depends(require_role(["admin"]))):
    """
    Return platform-wide KPI summary for the admin dashboard.
    Includes total orders, revenue, active vendor count, and customer count.
    """
    # Total customers
    customers_res = supabase.table("profiles").select("id", count="exact").eq("role", "customer").execute()
    total_customers = customers_res.count or 0

    # Total vendors
    vendors_res = supabase.table("profiles").select("id", count="exact").eq("role", "vendor").execute()
    total_vendors = vendors_res.count or 0

    # Active vendors
    active_vendors_res = (
        supabase.table("profiles")
        .select("id", count="exact")
        .eq("role", "vendor")
        .eq("status", "Active")
        .execute()
    )
    active_vendors = active_vendors_res.count or 0

    # Total orders and GMV
    orders_res = supabase.table("orders").select("total_kobo, status").execute()
    orders = orders_res.data or []
    total_orders = len(orders)
    total_gmv_kobo = sum(o.get("total_kobo", 0) for o in orders)
    delivered_orders = sum(1 for o in orders if o.get("status") == "Delivered")

    # Total products listed
    products_res = supabase.table("products").select("id", count="exact").execute()
    total_products = products_res.count or 0

    # Pending approvals
    pending_products_res = (
        supabase.table("products")
        .select("id", count="exact")
        .eq("status", "Pending Approval")
        .execute()
    )
    pending_products = pending_products_res.count or 0

    pending_vendors_res = (
        supabase.table("profiles")
        .select("id", count="exact")
        .eq("role", "vendor")
        .eq("status", "Pending Approval")
        .execute()
    )
    pending_vendors = pending_vendors_res.count or 0

    return {
        "customers": {
            "total": total_customers,
        },
        "vendors": {
            "total": total_vendors,
            "active": active_vendors,
            "pending_approval": pending_vendors,
        },
        "orders": {
            "total": total_orders,
            "delivered": delivered_orders,
            "fulfilment_rate": round(delivered_orders / total_orders * 100, 1) if total_orders else 0,
        },
        "revenue": {
            "gmv_kobo": total_gmv_kobo,
            "gmv_naira": total_gmv_kobo / 100,
            "formatted": f"₦{total_gmv_kobo / 100:,.2f}",
            # Platform commission (e.g. 5%)
            "commission_kobo": int(total_gmv_kobo * 0.05),
            "commission_naira": total_gmv_kobo * 0.05 / 100,
        },
        "products": {
            "total": total_products,
            "pending_approval": pending_products,
        },
    }


@router.get("/revenue")
async def get_revenue_breakdown(
    period: Optional[str] = Query("monthly", description="'daily' | 'weekly' | 'monthly'"),
    user=Depends(require_role(["admin"]))
):
    """
    Return a breakdown of GMV grouped by the specified period.
    Delegates to a Supabase SQL function 'fn_revenue_by_period' if available,
    otherwise returns raw order data for the frontend to aggregate.
    """
    try:
        # Attempt to use a Postgres function for aggregated results
        res = supabase.rpc("fn_revenue_by_period", {"p_period": period}).execute()
        if res.data:
            return {"period": period, "data": res.data}
    except Exception:
        pass  # Fall through to raw data

    # Fallback: return all completed orders for frontend aggregation
    res = (
        supabase.table("orders")
        .select("id, total_kobo, status, created_at")
        .in_("status", ["Delivered", "In Transit", "Processing"])
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )
    return {"period": period, "data": res.data or [], "aggregated": False}


@router.get("/vendors")
async def get_vendor_performance(user=Depends(require_role(["admin"]))):
    """
    Return vendor performance ranking by revenue contributed.
    """
    res = (
        supabase.table("profiles")
        .select("id, full_name, display_name, farm_name, location, rating, status")
        .eq("role", "vendor")
        .eq("status", "Active")
        .order("rating", desc=True)
        .limit(20)
        .execute()
    )
    return res.data or []
