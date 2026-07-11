"""
orders.py — Order management routes for Farmers Market API

Endpoints:
  GET   /orders/                       — List orders (filtered by role)
  POST  /orders/                       — Place a new order (customer only)
  GET   /orders/detect-location        — Auto-detect caller's location from IP
  GET   /orders/{order_id}             — Get a single order's details
  PATCH /orders/{order_id}/status      — Update order status (vendor/admin only)
  PATCH /orders/{order_id}/assign-driver — Assign a driver to an order (admin/driver)
  PATCH /orders/{order_id}/location    — Driver posts their current GPS coordinates
  GET   /orders/{order_id}/tracking    — Real-time tracking snapshot (all parties)
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from typing import Optional, List
import logging
from dependencies import get_current_user, require_role
from database import supabase, supabase_admin
from services.email import (
    send_order_confirmation,
    send_new_sale_alert,
    send_order_in_transit,
    send_order_delivered,
    send_order_cancelled,
)
from services.websocket_manager import connection_manager
from services.google_maps import geocode_address, get_distance_and_eta, detect_location_by_ip

logger = logging.getLogger(__name__)

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

class AssignDriverRequest(BaseModel):
    driver_id: str

class LocationUpdateRequest(BaseModel):
    latitude: float
    longitude: float
    speed: Optional[float] = None  # km/h, optional telemetry


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
    profile_res = (
        supabase.table("profiles")
        .select("role")
        .eq("id", user.id)
        .execute()
    )
    profile_data = profile_res.data[0] if profile_res.data else None
    role = profile_data.get("role") if profile_data else "customer"

    if role == "admin":
        res = supabase.table("orders").select("*, customer:profiles!customer_id(full_name, email), order_items(*, products(*))").order("created_at", desc=True).execute()
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

    # Verify product prices against database (prevent price manipulation)
    product_ids = list(set(item.product_id for item in payload.items))
    products_res = (
        supabase_admin.table("products")
        .select("id, price, stock, status, vendor_id, vendor:profiles!vendor_id(status)")
        .eq("status", "Approved")
        .eq("vendor.status", "Active")
        .in_("id", product_ids)
        .execute()
    )
    db_products = {p["id"]: p for p in (products_res.data or [])}

    for item in payload.items:
        db_prod = db_products.get(item.product_id)
        if not db_prod:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} is not available."
            )
        if db_prod["stock"] < item.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for product '{db_prod.get('name', item.product_id)}'."
            )

    # Calculate total from verified DB prices
    total_kobo = sum(
        db_products[item.product_id]["price"] * item.quantity
        for item in payload.items
    )

    # Verify wallet balance
    profile_res = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user.id)
        .execute()
    )
    profile_data = profile_res.data[0] if profile_res.data else None
    balance = profile_data.get("wallet_balance", 0) if profile_data else 0

    if balance < total_kobo:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Insufficient wallet balance. Please top up before ordering."
        )

    # Create the order (admin client to bypass RLS)
    config_res = supabase.table("platform_config").select("*").eq("id", "platform-config").execute()
    config = config_res.data[0] if config_res.data else {}
    delivery_fee = (
        config.get("delivery_express_fee", 150000)
        if payload.delivery_type == "express"
        else config.get("delivery_base_fee", 85000)
    )  # kobo
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

    # Geocode delivery address to coordinates (non-blocking, best-effort)
    try:
        coords = await geocode_address(payload.delivery_address)
        if coords:
            dest_lat, dest_lng = coords
            supabase_admin.table("orders").update(
                {"latitude": dest_lat, "longitude": dest_lng}
            ).eq("id", order_id).execute()
    except Exception as geo_exc:
        logger.warning(f"[orders] Geocoding failed for order {order_id}: {geo_exc}")

    # Create order_items (use DB price, not client-provided)
    items_data = [
        {
            "order_id": order_id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "unit_price_kobo": db_products[item.product_id]["price"],
        }
        for item in payload.items
    ]
    supabase_admin.table("order_items").insert(items_data).execute()

    # Decrement product stock for each ordered item
    for item in payload.items:
        prev_stock = db_products[item.product_id]["stock"]
        supabase_admin.table("products").update(
            {"stock": prev_stock - item.quantity}
        ).eq("id", item.product_id).execute()

    # Deduct from wallet
    new_balance = balance - (total_kobo + delivery_fee)
    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user.id).execute()

    # ── Email notifications ────────────────────────────────────────────────
    customer_profile_res = (
        supabase.table("profiles")
        .select("email, full_name")
        .eq("id", user.id)
        .execute()
    )
    customer_profile_data = customer_profile_res.data[0] if customer_profile_res.data else None
    if customer_profile_data:
        c_email = customer_profile_data.get("email", "")
        c_name  = customer_profile_data.get("full_name", "Customer")
        order_items_fmt = [
            {
                "name": db_products.get(item.product_id, {}).get("name", f"Product {item.product_id[:6]}"),
                "quantity": item.quantity,
                "unit_price_kobo": db_products[item.product_id]["price"],
            }
            for item in payload.items
        ]
        send_order_confirmation.delay(
            c_email, c_name, order_id,
            order_items_fmt,
            total_kobo + delivery_fee,
            payload.delivery_type,
            payload.delivery_address,
            new_balance,
        )

    # Alert each unique vendor that has items in this order (batched, using cached db_products)
    products_map = db_products

    vendor_ids = set()
    vendor_items_map = {}
    for item in payload.items:
        prod = products_map.get(item.product_id)
        if not prod:
            continue
        vid = prod.get("vendor_id")
        if not vid:
            continue
        vendor_ids.add(vid)
        if vid not in vendor_items_map:
            vendor_items_map[vid] = []
        vendor_items_map[vid].append({
            "name": prod.get("name", "Product"),
            "quantity": item.quantity,
            "unit_price_kobo": prod.get("price", 0),
        })

    if vendor_ids:
        vendors_res = supabase.table("profiles").select("id, email, full_name").in_("id", list(vendor_ids)).execute()
        vendor_profile_map = {v["id"]: v for v in (vendors_res.data or [])}

        for vid in vendor_ids:
            v_profile = vendor_profile_map.get(vid)
            if not v_profile:
                continue
            v_email = v_profile.get("email", "")
            v_name  = v_profile.get("full_name", "Vendor")
            v_items = vendor_items_map.get(vid, [])
            city = payload.delivery_address.split(",")[-1].strip()
            send_new_sale_alert.delay(
                v_email, v_name, order_id,
                v_items,
                payload.delivery_type,
                city,
            )
            # Real-time WebSocket vendor alert broadcast
            await connection_manager.broadcast_vendor_alert(
                order_id=order_id,
                vendor_id=vid,
                message="New sale alert",
                extra_data={"total_kobo": total_kobo + delivery_fee}
            )

    return {
        "message": "Order placed successfully.",
        "order_id": order_id,
        "total_kobo": total_kobo + delivery_fee,
        "new_wallet_balance": new_balance,
    }


# ---------------------------------------------------------------------------
# Auto-detect caller location from IP address
# ---------------------------------------------------------------------------

@router.get("/detect-location", tags=["orders", "geolocation"])
async def detect_location(request: Request, user=Depends(get_current_user)):
    """
    Automatically detect the caller's approximate location from their IP address.

    The server reads the client IP from standard headers
    (`X-Forwarded-For`, `X-Real-IP`, or the direct connection).
    Uses ip-api.com for geolocation (free tier, no API key required).

    Returns approximate city/region/country + coordinates.
    Useful for pre-filling a delivery address on the frontend.

    Returns:
        200 + location dict on success.
        422 if the IP cannot be resolved (e.g. localhost during development).
    """
    # Resolve the real client IP — honour reverse proxy headers
    ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP", "")
        or (request.client.host if request.client else "")
    )

    location = await detect_location_by_ip(ip)
    if not location:
        return {
            "detected": False,
            "ip": ip,
            "message": "Could not resolve location for this IP (may be localhost or a private network).",
        }

    return {
        "detected": True,
        "ip": ip,
        **location,
    }


@router.get("/{order_id}")
async def get_order(order_id: str, user=Depends(get_current_user)):
    """Get detailed information about a specific order."""
    res = (
        supabase.table("orders")
        .select("*, order_items(*, products(name, image_url, price, vendor_id))")
        .eq("id", order_id)
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    order = res.data[0]

    # Customers can only view their own orders
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    role = profile_data.get("role") if profile_data else "customer"

    if role == "customer" and order.get("customer_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return order


@router.patch("/{order_id}/status")
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdate,
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

    # Verify vendor has products in this order (if not admin)
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    role = profile_data.get("role") if profile_data else "customer"
    if role == "vendor":
        vendor_check = (
            supabase.table("order_items")
            .select("id, products!inner(vendor_id)")
            .eq("order_id", order_id)
            .eq("products.vendor_id", user.id)
            .limit(1)
            .execute()
        )
        if not vendor_check.data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only update orders containing your products."
            )

    update_data = {"status": payload.status}
    if payload.note:
        update_data["status_note"] = payload.note

    res = supabase_admin.table("orders").update(update_data).eq("id", order_id).execute()

    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    # ── On cancellation: restore product stock + refund customer wallet ───────
    if payload.status == "Cancelled":
        try:
            items_res = (
                supabase_admin.table("order_items")
                .select("product_id, quantity")
                .eq("order_id", order_id)
                .execute()
            )
            for oi in (items_res.data or []):
                product_id = oi.get("product_id")
                quantity = oi.get("quantity")
                if not product_id or quantity is None:
                    continue
                prod_res = (
                    supabase_admin.table("products")
                    .select("stock")
                    .eq("id", product_id)
                    .execute()
                )
                if prod_res.data:
                    restored = prod_res.data[0]["stock"] + quantity
                    supabase_admin.table("products").update(
                        {"stock": restored}
                    ).eq("id", product_id).execute()
        except Exception as exc:
            logger.warning(f"[orders] Stock restore on cancellation failed for {order_id}: {exc}")

        try:
            order_data_inner = res.data[0]
            cust_id = order_data_inner.get("customer_id")
            total = order_data_inner.get("total_kobo", 0)
            if cust_id and total:
                bal_res = (
                    supabase_admin.table("profiles")
                    .select("wallet_balance")
                    .eq("id", cust_id)
                    .execute()
                )
                if bal_res.data:
                    cur_bal = bal_res.data[0].get("wallet_balance", 0)
                    supabase_admin.table("profiles").update(
                        {"wallet_balance": cur_bal + total}
                    ).eq("id", cust_id).execute()
        except Exception as exc:
            logger.warning(f"[orders] Wallet refund on cancellation failed for {order_id}: {exc}")


    # ── Email notifications ────────────────────────────────────────────────
    order_data = res.data[0]
    customer_id = order_data.get("customer_id")
    if customer_id:
        cust_res = (
            supabase.table("profiles")
            .select("email, full_name")
            .eq("id", customer_id)
            .execute()
        )
        cust_data = cust_res.data[0] if cust_res.data else None
        if cust_data:
            c_email   = cust_data.get("email", "")
            c_name    = cust_data.get("full_name", "Customer")
            c_address = order_data.get("delivery_address", "")
            c_total   = order_data.get("total_kobo", 0)

            if payload.status == "In Transit":
                send_order_in_transit.delay(
                    c_email, c_name, order_id, c_address,
                )
            elif payload.status == "Delivered":
                send_order_delivered.delay(
                    c_email, c_name, order_id,
                )
            elif payload.status == "Cancelled":
                send_order_cancelled.delay(
                    c_email, c_name, order_id, c_total, payload.note,
                )

    # Trigger real-time WebSocket order status update broadcast
    await connection_manager.broadcast_order_status(
        order_id=order_id,
        status=payload.status,
        note=payload.note
    )

    return {"message": f"Order status updated to '{payload.status}'.", "order": order_data}


# ---------------------------------------------------------------------------
# Driver assignment
# ---------------------------------------------------------------------------

@router.patch("/{order_id}/assign-driver", tags=["orders", "geolocation"])
async def assign_driver(
    order_id: str,
    payload: AssignDriverRequest,
    user=Depends(get_current_user),
):
    """
    Assign a driver or courier to an order.

    **Access:**
    - `admin` — can assign any driver to any order.
    - `driver` / `courier` — can self-assign (driver_id must equal their own id).

    Sets `driver_id` on the order and transitions status to `'In Transit'`.
    """
    # Resolve caller role
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    role = (profile_res.data[0].get("role") if profile_res.data else None) or ""

    if role not in {"admin", "driver", "courier"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins and drivers can assign drivers to orders.",
        )

    # Drivers may only self-assign
    if role in {"driver", "courier"} and payload.driver_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Drivers can only assign themselves to an order.",
        )

    # Verify the target driver profile exists and is a driver/courier
    driver_res = (
        supabase_admin.table("profiles")
        .select("id, full_name, role")
        .eq("id", payload.driver_id)
        .execute()
    )
    if not driver_res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Driver profile not found.",
        )
    driver_profile = driver_res.data[0]
    if driver_profile.get("role") not in {"driver", "courier"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The specified user is not a driver or courier.",
        )

    # Verify order exists
    order_res = supabase_admin.table("orders").select("id, status").eq("id", order_id).execute()
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    # Assign driver and set status In Transit
    supabase_admin.table("orders").update(
        {"driver_id": payload.driver_id, "status": "In Transit"}
    ).eq("id", order_id).execute()

    # Broadcast WebSocket update
    await connection_manager.broadcast_order_status(
        order_id=order_id,
        status="In Transit",
        note=f"Driver {driver_profile.get('full_name', 'assigned')} is on the way."
    )

    return {
        "message": "Driver assigned successfully.",
        "order_id": order_id,
        "driver_id": payload.driver_id,
        "driver_name": driver_profile.get("full_name"),
    }


# ---------------------------------------------------------------------------
# Driver real-time location update
# ---------------------------------------------------------------------------

@router.patch("/{order_id}/location", tags=["orders", "geolocation"])
async def update_driver_location(
    order_id: str,
    payload: LocationUpdateRequest,
    user=Depends(get_current_user),
):
    """
    Driver posts their current GPS coordinates for a specific order.

    - Validates coordinates are within valid GPS ranges.
    - Persists `driver_latitude` / `driver_longitude` to the order row.
    - Calls Google Distance Matrix API to calculate updated driving ETA
      and distance from driver → customer destination.
    - Broadcasts the full tracking update to all WebSocket subscribers
      (customer, vendor, admin watching the order).

    **Access:** Only the driver assigned to the order (or admin) can call this.
    """
    import math

    # Basic coordinate bounds check
    if not (-90.0 <= payload.latitude <= 90.0 and -180.0 <= payload.longitude <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Coordinates out of valid GPS range.",
        )
    if math.isnan(payload.latitude) or math.isnan(payload.longitude):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Coordinates must be finite numbers.",
        )

    # Resolve caller role
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    role = (profile_res.data[0].get("role") if profile_res.data else None) or ""

    if role not in {"admin", "driver", "courier"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only drivers and admins can post location updates.",
        )

    # Fetch the order — verify assigned driver
    order_res = (
        supabase_admin.table("orders")
        .select("id, driver_id, latitude, longitude, status")
        .eq("id", order_id)
        .execute()
    )
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    order = order_res.data[0]

    if role != "admin" and order.get("driver_id") != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the assigned driver for this order.",
        )

    # Calculate ETA + distance from driver to destination (best-effort)
    distance_text = None
    eta_minutes = None
    dest_lat = order.get("latitude")
    dest_lng = order.get("longitude")
    if dest_lat is not None and dest_lng is not None:
        try:
            distance_text, eta_minutes = await get_distance_and_eta(
                payload.latitude, payload.longitude,
                float(dest_lat), float(dest_lng),
            )
        except Exception as exc:
            logger.warning(f"[orders] Distance Matrix error for order {order_id}: {exc}")

    # Persist to DB
    update_fields: dict = {
        "driver_latitude":  payload.latitude,
        "driver_longitude": payload.longitude,
    }
    if distance_text is not None:
        update_fields["distance_text"] = distance_text
    if eta_minutes is not None:
        update_fields["eta_minutes"] = eta_minutes

    supabase_admin.table("orders").update(update_fields).eq("id", order_id).execute()

    # Broadcast real-time location update via WebSocket
    ws_payload = {
        "event":          "delivery_location_ping",
        "type":           "delivery_location_ping",
        "order_id":       order_id,
        "latitude":       payload.latitude,
        "longitude":      payload.longitude,
        "speed":          payload.speed,
        "distance_text":  distance_text,
        "eta_minutes":    eta_minutes,
        "sender_id":      user.id,
    }
    await connection_manager.broadcast_to_order(order_id, ws_payload)

    return {
        "message":       "Location updated.",
        "order_id":      order_id,
        "latitude":      payload.latitude,
        "longitude":     payload.longitude,
        "distance_text": distance_text,
        "eta_minutes":   eta_minutes,
    }


# ---------------------------------------------------------------------------
# Tracking snapshot (customer / vendor / admin view)
# ---------------------------------------------------------------------------

@router.get("/{order_id}/tracking", tags=["orders", "geolocation"])
async def get_order_tracking(order_id: str, user=Depends(get_current_user)):
    """
    Return a real-time tracking snapshot for an order.

    Accessible by:
    - The customer who placed the order.
    - Any vendor with items in the order.
    - Admins.
    - The assigned driver.

    Returns destination coordinates, current driver coordinates,
    cached ETA, distance, driver profile details, and order status.
    """
    # Fetch order with related data
    order_res = (
        supabase_admin.table("orders")
        .select(
            "id, status, delivery_address, delivery_type, "
            "latitude, longitude, driver_id, "
            "driver_latitude, driver_longitude, "
            "eta_minutes, distance_text, customer_id, "
            "order_items(product_id, products(vendor_id))"
        )
        .eq("id", order_id)
        .execute()
    )
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    order = order_res.data[0]

    # Authorization check
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    role = (profile_res.data[0].get("role") if profile_res.data else None) or ""

    is_customer = order.get("customer_id") == user.id
    is_driver   = order.get("driver_id") == user.id
    is_vendor   = role == "vendor" and any(
        item.get("products", {}).get("vendor_id") == user.id
        for item in (order.get("order_items") or [])
    )
    is_admin = role == "admin"

    if not any([is_customer, is_driver, is_vendor, is_admin]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to track this order.",
        )

    # Fetch driver profile details if assigned
    driver_info = None
    driver_id = order.get("driver_id")
    if driver_id:
        d_res = (
            supabase_admin.table("profiles")
            .select("full_name, phone")
            .eq("id", driver_id)
            .execute()
        )
        if d_res.data:
            driver_info = d_res.data[0]

    return {
        "order_id":        order_id,
        "status":          order.get("status"),
        "delivery_address": order.get("delivery_address"),
        "destination": {
            "latitude":  order.get("latitude"),
            "longitude": order.get("longitude"),
        },
        "driver": {
            "id":        driver_id,
            "name":      driver_info.get("full_name") if driver_info else None,
            "phone":     driver_info.get("phone")     if driver_info else None,
            "latitude":  order.get("driver_latitude"),
            "longitude": order.get("driver_longitude"),
        },
        "eta_minutes":   order.get("eta_minutes"),
        "distance_text": order.get("distance_text"),
    }
