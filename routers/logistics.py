"""
logistics.py — Logistics & Dispatch routes for Farmers Market API

Endpoints:
  GET    /logistics/hubs          — List all hubs
  POST   /logistics/hubs          — Add a new hub
  DELETE /logistics/hubs          — Remove a hub
  GET    /logistics/riders        — List all riders
  POST   /logistics/riders        — Add a new rider
  PATCH  /logistics/riders/{id}   — Update rider details/status
  DELETE /logistics/riders/{id}   — Remove a rider
  POST   /logistics/riders/{id}/assign — Assign an order to a rider
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from typing import Optional, List
import math
from datetime import datetime, timezone
from dependencies import require_role, get_current_user
from database import supabase_admin, supabase
from services.websocket_manager import connection_manager

router = APIRouter(prefix="/logistics", tags=["logistics"])

def calculate_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0  # Earth radius in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (
        math.sin(dLat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dLon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 2)

class ProofOfDeliveryRequest(BaseModel):
    image_url: str
    recipient_name: str
    notes: Optional[str] = None

class DutyStatusRequest(BaseModel):
    is_online: bool

class RiderKYCRequest(BaseModel):
    license_url: str
    vehicle_photo_url: str
    face_photo_url: str
    vehicle_type: Optional[str] = "motorcycle"
    vehicle_plate: Optional[str] = None
    emergency_contact: Optional[str] = None

class RiderKYCVerifyRequest(BaseModel):
    status: str  # "Verified" | "Rejected"
    reason: Optional[str] = None

# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class HubCreate(BaseModel):
    name: str
    capacity: Optional[int] = 0
    eta: Optional[str] = "N/A"
    zone: Optional[str] = "Unassigned"

class HubDelete(BaseModel):
    name: str

class RiderCreate(BaseModel):
    name: str
    phone: str
    hub_name: Optional[str] = None
    status: Optional[str] = "Active"

class RiderUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    hub_name: Optional[str] = None
    status: Optional[str] = None

class AssignOrderRequest(BaseModel):
    order_id: str

# ---------------------------------------------------------------------------
# Hub Management
# ---------------------------------------------------------------------------

@router.get("/hubs")
async def list_hubs(user=Depends(require_role(["admin", "logist"]))):
    """List all hubs"""
    res = supabase_admin.table("hubs").select("*").order("name", asc=True).execute()
    return res.data or []

@router.post("/hubs", status_code=status.HTTP_201_CREATED)
async def add_hub(
    payload: HubCreate,
    user=Depends(require_role(["admin", "logist"]))
):
    """Add a new hub"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Hub name is required.")

    existing = supabase_admin.table("hubs").select("id").eq("name", name).execute()
    if existing.data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Hub already exists.")

    res = supabase_admin.table("hubs").insert(payload.model_dump()).execute()
    return {"message": f"Hub '{name}' added successfully.", "data": res.data[0] if res.data else None}

@router.delete("/hubs", status_code=status.HTTP_200_OK)
async def remove_hub(
    payload: HubDelete,
    user=Depends(require_role(["admin", "logist"]))
):
    """Remove a hub by name"""
    name = payload.name.strip()
    res = supabase_admin.table("hubs").delete().eq("name", name).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hub not found.")
    return {"message": f"Hub '{name}' removed."}

# ---------------------------------------------------------------------------
# Rider Management
# ---------------------------------------------------------------------------

@router.get("/riders")
async def list_riders(user=Depends(require_role(["admin", "logist"]))):
    """List all riders from riders table and registered courier profiles"""
    riders_map = {}
    
    # 1. Fetch registered courier profiles
    try:
        prof_res = (
            supabase_admin.table("profiles")
            .select("*")
            .in_("role", ["courier", "driver", "rider"])
            .execute()
        )
        for p in (prof_res.data or []):
            riders_map[p["id"]] = {
                "id": p["id"],
                "name": p.get("full_name") or p.get("email") or "Courier Rider",
                "phone": p.get("phone") or "",
                "hub": p.get("hub") or "Lagos Island",
                "hub_name": p.get("hub") or "Lagos Island",
                "vehicle": p.get("vehicle_type") or "Motorcycle",
                "status": "Active",
                "verification_status": p.get("verification_status") or "Not Submitted",
                "license_url": p.get("license_url"),
                "vehicle_photo_url": p.get("vehicle_photo_url"),
                "face_photo_url": p.get("face_photo_url"),
                "vehicle_plate": p.get("vehicle_plate"),
                "emergency_contact": p.get("emergency_contact"),
            }
    except Exception:
        pass

    # 2. Fetch riders table and overlay
    try:
        res = supabase_admin.table("riders").select("*").order("name", asc=True).execute()
        for r in (res.data or []):
            rid = r.get("id")
            if rid in riders_map:
                riders_map[rid].update(r)
            else:
                riders_map[rid] = r
    except Exception:
        pass

    return list(riders_map.values())

@router.post("/riders", status_code=status.HTTP_201_CREATED)
async def add_rider(
    payload: RiderCreate,
    user=Depends(require_role(["admin", "logist"]))
):
    """Add a new rider"""
    if not payload.name.strip() or not payload.phone.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name and phone are required.")

    res = supabase_admin.table("riders").insert(payload.model_dump()).execute()
    return {"message": f"Rider '{payload.name}' added successfully.", "data": res.data[0] if res.data else None}

@router.patch("/riders/{rider_id}", status_code=status.HTTP_200_OK)
async def update_rider(
    rider_id: str,
    payload: RiderUpdate,
    user=Depends(require_role(["admin", "logist"]))
):
    """Update a rider's details or status"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update.")

    res = supabase_admin.table("riders").update(update_data).eq("id", rider_id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rider not found.")
    
    return {"message": "Rider updated successfully.", "data": res.data[0]}

@router.delete("/riders/{rider_id}", status_code=status.HTTP_200_OK)
async def remove_rider(
    rider_id: str,
    user=Depends(require_role(["admin", "logist"]))
):
    """Remove a rider"""
    res = supabase_admin.table("riders").delete().eq("id", rider_id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rider not found.")
    return {"message": "Rider removed successfully."}

@router.post("/riders/{rider_id}/assign", status_code=status.HTTP_200_OK)
async def assign_order(
    rider_id: str,
    payload: AssignOrderRequest,
    user=Depends(require_role(["admin", "logist"]))
):
    """
    Assign an order to a rider.
    1. Updates the rider's assigned_order_id and status.
    2. Updates the order's status to In Transit.
    """
    # Verify rider exists
    rider_res = supabase_admin.table("riders").select("id, status").eq("id", rider_id).execute()
    if not rider_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rider not found.")
    
    # Verify order exists
    order_res = supabase_admin.table("orders").select("id, status").eq("id", payload.order_id).execute()
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    # Update Rider
    supabase_admin.table("riders").update({
        "assigned_order_id": payload.order_id,
        "status": "In Transit"
    }).eq("id", rider_id).execute()

    # Update Order Status
    supabase_admin.table("orders").update({
        "status": "In Transit"
    }).eq("id", payload.order_id).execute()

    return {"message": f"Order {payload.order_id} assigned to rider {rider_id} successfully."}


# ---------------------------------------------------------------------------
# Delivery Pool & Courier Operations
# ---------------------------------------------------------------------------

@router.get("/pool")
async def get_delivery_pool(
    lat: Optional[float] = Query(None, description="Rider latitude"),
    lng: Optional[float] = Query(None, description="Rider longitude"),
    radius_km: float = Query(25.0, description="Search radius in kilometers"),
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Fetch open delivery offers available for couriers to claim.
    Returns orders ready for pickup or processing with optional radius filter.
    """
    try:
        res = (
            supabase_admin.table("orders")
            .select("*, customer:profiles!customer_id(full_name, phone, delivery_address), order_items(*, products(*))")
            .in_("status", ["Processing", "Ready for Pickup", "In Transit"])
            .is_("driver_id", "null")
            .order("created_at", desc=True)
            .execute()
        )
        orders = res.data or []
    except Exception:
        res = (
            supabase_admin.table("orders")
            .select("*, customer:profiles!customer_id(full_name, phone, delivery_address), order_items(*, products(*))")
            .in_("status", ["Processing", "Ready for Pickup"])
            .order("created_at", desc=True)
            .execute()
        )
        orders = res.data or []

    offers = []
    for o in orders:
        o_lat = o.get("latitude") or 6.5244
        o_lng = o.get("longitude") or 3.3792
        dist = 0.0
        if lat is not None and lng is not None:
            dist = calculate_distance_km(lat, lng, float(o_lat), float(o_lng))
            if dist > radius_km:
                continue

        cust = o.get("customer") or {}
        items = o.get("order_items") or []
        items_count = sum(i.get("quantity", 1) for i in items)

        offers.append({
            "id": f"task-{o['id'][:8]}",
            "orderId": o["id"],
            "order_id": o["id"],
            "farmName": "Farm Depot",
            "farmAddress": "Lagos Agro Distribution Hub",
            "farmPhone": "+2348003276435",
            "farmLat": 6.5244,
            "farmLng": 3.3792,
            "customerName": cust.get("full_name") or "Customer",
            "customerAddress": o.get("delivery_address") or cust.get("delivery_address") or "Lagos",
            "customerPhone": cust.get("phone") or "+2348000000000",
            "customerLat": float(o_lat),
            "customerLng": float(o_lng),
            "itemsCount": items_count,
            "payoutKobo": 125000,
            "distanceKm": dist,
            "status": "available",
            "deliveryType": o.get("delivery_type", "standard"),
            "createdAt": o.get("created_at")
        })

    return offers


@router.post("/orders/{order_id}/accept")
async def accept_delivery_offer(
    order_id: str,
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Courier claims an available delivery order from the pool.
    """
    order_res = supabase_admin.table("orders").select("id, status").eq("id", order_id).execute()
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    rider_id = user.id
    supabase_admin.table("orders").update({
        "status": "In Transit",
        "driver_id": rider_id
    }).eq("id", order_id).execute()

    task_payload = {
        "id": f"task-{order_id[:8]}",
        "orderId": order_id,
        "order_id": order_id,
        "assignedRiderId": rider_id,
        "status": "accepted",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    await connection_manager.broadcast_task_assigned(task_payload)
    return {"success": True, "message": "Delivery offer claimed successfully.", "task": task_payload}


@router.post("/orders/{order_id}/pod")
async def submit_proof_of_delivery(
    order_id: str,
    payload: ProofOfDeliveryRequest,
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Submit Proof of Delivery (POD) photo, recipient name, and notes, marking the order Delivered.
    """
    if not payload.image_url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Proof of delivery photo is required.")
    if not payload.recipient_name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recipient signature name is required.")

    now_iso = datetime.now(timezone.utc).isoformat()
    receipt_id = f"POD-REC-{order_id[:6].upper()}"

    update_fields = {
        "status": "Delivered",
        "proof_of_delivery_url": payload.image_url,
        "pod_recipient_name": payload.recipient_name,
        "pod_notes": payload.notes or ""
    }

    supabase_admin.table("orders").update(update_fields).eq("id", order_id).execute()

    await connection_manager.broadcast_order_updated(order_id, "Delivered", {
        "proofOfDeliveryUrl": payload.image_url,
        "recipientName": payload.recipient_name,
        "receiptId": receipt_id,
        "completedAt": now_iso
    })

    return {
        "success": True,
        "payoutKobo": 125000,
        "receiptId": receipt_id,
        "completedAt": now_iso
    }


@router.patch("/rider/duty")
async def set_rider_duty_status(
    payload: DutyStatusRequest,
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Toggle rider online/offline duty status.
    """
    status_str = "Active" if payload.is_online else "Offline"
    try:
        supabase_admin.table("riders").update({"status": status_str}).eq("id", user.id).execute()
    except Exception:
        pass

    return {
        "success": True,
        "is_online": payload.is_online,
        "status": status_str
    }


@router.post("/rider/kyc")
async def submit_rider_kyc(
    payload: RiderKYCRequest,
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Courier submits their Driver's License, Vehicle Photo, and Facial Selfie for verification.
    """
    if not payload.license_url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Driver's License image is required.")
    if not payload.vehicle_photo_url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vehicle photo is required.")
    if not payload.face_photo_url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Facial selfie image is required.")

    kyc_data = {
        "license_url": payload.license_url,
        "vehicle_photo_url": payload.vehicle_photo_url,
        "face_photo_url": payload.face_photo_url,
        "vehicle_type": payload.vehicle_type or "motorcycle",
        "vehicle_plate": payload.vehicle_plate,
        "emergency_contact": payload.emergency_contact,
        "verification_status": "Pending Verification",
        "kyc_submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        supabase_admin.table("riders").upsert({"id": user.id, **kyc_data}).execute()
    except Exception as e:
        try:
            supabase_admin.table("profiles").update(kyc_data).eq("id", user.id).execute()
        except Exception:
            pass

    return {
        "success": True,
        "message": "KYC verification documents submitted. Awaiting dispatch manager review.",
        "verification_status": "Pending Verification",
        "data": kyc_data
    }


@router.get("/rider/kyc")
async def get_rider_kyc(
    user=Depends(require_role(["driver", "courier", "rider", "admin", "logist"]))
):
    """
    Get current courier's KYC verification status and uploaded documents.
    """
    try:
        rider_res = supabase_admin.table("riders").select("*").eq("id", user.id).execute()
        if rider_res.data:
            return rider_res.data[0]
    except Exception:
        pass

    try:
        profile_res = supabase_admin.table("profiles").select("*").eq("id", user.id).execute()
        if profile_res.data:
            return profile_res.data[0]
    except Exception:
        pass

    return {
        "verification_status": "Not Submitted",
        "license_url": None,
        "vehicle_photo_url": None,
        "face_photo_url": None
    }


@router.patch("/riders/{rider_id}/verify")
async def verify_rider_kyc(
    rider_id: str,
    payload: RiderKYCVerifyRequest,
    user=Depends(require_role(["admin", "logist"]))
):
    """
    Admin or Dispatch Manager approves or rejects a courier's KYC documents.
    """
    if payload.status not in ("Verified", "Rejected"):
        raise HTTPException(status_code=400, detail="Status must be 'Verified' or 'Rejected'")

    update_payload = {
        "verification_status": payload.status,
        "verification_reason": payload.reason or "",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "status": "Active" if payload.status == "Verified" else "Suspended"
    }

    try:
        supabase_admin.table("riders").update(update_payload).eq("id", rider_id).execute()
    except Exception:
        pass
    try:
        supabase_admin.table("profiles").update(update_payload).eq("id", rider_id).execute()
    except Exception:
        pass

    return {
        "success": True,
        "rider_id": rider_id,
        "verification_status": payload.status,
        "message": f"Rider KYC has been set to {payload.status}."
    }
