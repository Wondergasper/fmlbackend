"""
orders.py — Order management routes for Farmers Market API

Endpoints:
  GET   /orders/             — List orders (filtered by role: customer sees own, vendor sees their items, admin sees all)
  POST  /orders/             — Place a new order (customer only)
  GET   /orders/{order_id}   — Get a single order's details
  PATCH /orders/{order_id}/status — Update order status (vendor/admin only)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List
from dependencies import get_current_user, require_role
from database import supabase, supabase_admin
from services.email import (
    send_order_confirmation,
    send_new_sale_alert,
    send_order_in_transit,
    send_order_delivered,
    send_order_cancelled,
)

router = APIRouter(prefix="/orders", tags=["orders"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class OrderItem(BaseModel):
    product_id: str
    quantity: int
    unit_price: int  # price in kobo at time of order

class OrderCreate(BaseModel):
    items: List[OrderItem]
    delivery_address: str
    delivery_type: str = "standard"   # "standard" | "express"
    notes: Optional[str] = None

class OrderStatusUpdate(BaseModel):
    status: str   # "Processing" | "In Transit" | "Delivered" | "Cancelled"
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def list_orders(user=Depends(get_current_user)):
    """
    Return orders relevant to the caller's role:
    - customer  → only their own orders
    - vendor    → orders containing their products
    - admin     → all orders
    """
    # Fetch role from profiles
    profile = (
        supabase.table("profiles")
        .select("role")
        .eq("id", user.id)
        .single()
        .execute()
    )
    role = profile.data.get("role") if profile.data else "customer"

    if role == "admin":
        res = supabase.table("orders").select("*, order_items(*, products(*))").order("created_at", desc=True).execute()
    elif role == "vendor":
        # Orders that contain this vendor's products
        res = (
            supabase.table("orders")
            .select("*, order_items!inner(*, products!inner(vendor_id))")
            .eq("order_items.products.vendor_id", user.id)
            .order("created_at", desc=True)
            .execute()
        )
    else:
        res = (
            supabase.table("orders")
            .select("*, order_items(*, products(name, image_url))")
            .eq("customer_id", user.id)
            .order("created_at", desc=True)
            .execute()
        )

    return res.data or []


@router.post("/", status_code=status.HTTP_201_CREATED)
async def place_order(
    payload: OrderCreate,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["customer"]))
):
    """
    Place a new order. Deducts the total from the customer's wallet balance
    and creates order + order_items rows atomically.
    """
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order must contain at least one item."
        )

    # Calculate total
    total_kobo = sum(item.unit_price * item.quantity for item in payload.items)

    # Verify wallet balance
    profile = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user.id)
        .single()
        .execute()
    )
    balance = profile.data.get("wallet_balance", 0) if profile.data else 0

    if balance < total_kobo:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Insufficient wallet balance. Please top up before ordering."
        )

    # Create the order (admin client to bypass RLS)
    delivery_fee = 150000 if payload.delivery_type == "express" else 85000  # kobo
    order_data = {
        "customer_id": user.id,
        "total_kobo": total_kobo + delivery_fee,
        "delivery_type": payload.delivery_type,
        "delivery_address": payload.delivery_address,
        "notes": payload.notes,
        "status": "Processing",
        "payment_status": "Paid",
    }
    order_res = supabase_admin.table("orders").insert(order_data).execute()
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create order.")

    order_id = order_res.data[0]["id"]

    # Create order_items
    items_data = [
        {
            "order_id": order_id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "unit_price_kobo": item.unit_price,
        }
        for item in payload.items
    ]
    supabase_admin.table("order_items").insert(items_data).execute()

    # Deduct from wallet
    new_balance = balance - (total_kobo + delivery_fee)
    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user.id).execute()

    # ── Email notifications ────────────────────────────────────────────────
    customer_profile = (
        supabase.table("profiles")
        .select("email, full_name")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if customer_profile.data:
        c_email = customer_profile.data.get("email", "")
        c_name  = customer_profile.data.get("full_name", "Customer")
        order_items_fmt = [
            {
                "name": f"Product {item.product_id[:6]}",
                "quantity": item.quantity,
                "unit_price_kobo": item.unit_price,
            }
            for item in payload.items
        ]
        background_tasks.add_task(
            send_order_confirmation,
            c_email, c_name, order_id,
            order_items_fmt,
            total_kobo + delivery_fee,
            payload.delivery_type,
            payload.delivery_address,
            new_balance,
        )

    # Alert each unique vendor that has items in this order
    vendor_ids = set()
    for item in payload.items:
        prod = (
            supabase.table("products")
            .select("vendor_id, name")
            .eq("id", item.product_id)
            .single()
            .execute()
        )
        if prod.data:
            vid = prod.data.get("vendor_id")
            if vid and vid not in vendor_ids:
                vendor_ids.add(vid)
                vendor_profile = (
                    supabase.table("profiles")
                    .select("email, full_name")
                    .eq("id", vid)
                    .single()
                    .execute()
                )
                if vendor_profile.data:
                    v_email = vendor_profile.data.get("email", "")
                    v_name  = vendor_profile.data.get("full_name", "Vendor")
                    vendor_items = [
                        {
                            "name": prod.data.get("name", "Product"),
                            "quantity": item.quantity,
                            "unit_price_kobo": item.unit_price,
                        }
                    ]
                    city = payload.delivery_address.split(",")[-1].strip()
                    background_tasks.add_task(
                        send_new_sale_alert,
                        v_email, v_name, order_id,
                        vendor_items,
                        payload.delivery_type,
                        city,
                    )

    return {
        "message": "Order placed successfully.",
        "order_id": order_id,
        "total_kobo": total_kobo + delivery_fee,
        "new_wallet_balance": new_balance,
    }


@router.get("/{order_id}")
async def get_order(order_id: str, user=Depends(get_current_user)):
    """Get detailed information about a specific order."""
    res = (
        supabase.table("orders")
        .select("*, order_items(*, products(name, image_url, price, vendor_id))")
        .eq("id", order_id)
        .single()
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    # Customers can only view their own orders
    profile = supabase.table("profiles").select("role").eq("id", user.id).single().execute()
    role = profile.data.get("role") if profile.data else "customer"

    if role == "customer" and res.data.get("customer_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return res.data


@router.patch("/{order_id}/status")
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdate,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["vendor", "admin"]))
):
    """
    Update the status of an order.
    Vendors can move orders through: Processing → In Transit → Delivered
    Admins can set any status including Cancelled.
    """
    allowed_statuses = {"Processing", "In Transit", "Delivered", "Cancelled"}
    if payload.status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {allowed_statuses}"
        )

    update_data = {"status": payload.status}
    if payload.note:
        update_data["status_note"] = payload.note

    res = supabase_admin.table("orders").update(update_data).eq("id", order_id).execute()

    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    # ── Email notifications ────────────────────────────────────────────────
    order_data = res.data[0]
    customer_id = order_data.get("customer_id")
    if customer_id:
        cust = (
            supabase.table("profiles")
            .select("email, full_name")
            .eq("id", customer_id)
            .single()
            .execute()
        )
        if cust.data:
            c_email   = cust.data.get("email", "")
            c_name    = cust.data.get("full_name", "Customer")
            c_address = order_data.get("delivery_address", "")
            c_total   = order_data.get("total_kobo", 0)

            if payload.status == "In Transit":
                background_tasks.add_task(
                    send_order_in_transit,
                    c_email, c_name, order_id, c_address,
                )
            elif payload.status == "Delivered":
                background_tasks.add_task(
                    send_order_delivered,
                    c_email, c_name, order_id,
                )
            elif payload.status == "Cancelled":
                background_tasks.add_task(
                    send_order_cancelled,
                    c_email, c_name, order_id, c_total, payload.note,
                )

    return {"message": f"Order status updated to '{payload.status}'.", "order": order_data}
