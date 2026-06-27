"""
vendors.py — Vendor management routes for Farmers Market API

Endpoints:
  GET   /vendors/              — List all vendors (admin only)
  GET   /vendors/{vendor_id}   — Get a single vendor's public profile
  PATCH /vendors/{vendor_id}/status — Approve / Suspend a vendor (admin only)
  GET   /vendors/me/profile    — Get the authenticated vendor's own profile
  PATCH /vendors/me/profile    — Update authenticated vendor's own profile
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from dependencies import get_current_user, require_role
from database import supabase, supabase_admin
from services.email import send_vendor_approved, send_vendor_suspended

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class VendorStatusUpdate(BaseModel):
    status: str   # "Active" | "Suspended" | "Pending Approval"
    reason: Optional[str] = None

class VendorProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    farm_name: Optional[str] = None
    location: Optional[str] = None
    fulfillment_hub: Optional[str] = None
    order_cutoff: Optional[str] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_name: Optional[str] = None
    phone: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def list_vendors(user=Depends(require_role(["admin"]))):
    """
    Return all registered vendor profiles.
    Admin access only.
    """
    res = (
        supabase.table("profiles")
        .select("id, full_name, display_name, email, farm_name, location, status, rating, products_count, created_at")
        .eq("role", "vendor")
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


@router.get("/me/profile")
async def get_my_vendor_profile(user=Depends(require_role(["vendor"]))):
    """
    Return the full profile of the authenticated vendor.
    """
    res = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor profile not found.")
    return res.data


@router.patch("/me/profile")
async def update_my_vendor_profile(
    payload: VendorProfileUpdate,
    user=Depends(require_role(["vendor"]))
):
    """
    Allow a vendor to update their own profile details and bank info.
    """
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update.")

    res = supabase.table("profiles").update(update_data).eq("id", user.id).execute()
    return {"message": "Vendor profile updated successfully.", "data": res.data[0] if res.data else None}


@router.get("/{vendor_id}")
async def get_vendor(vendor_id: str):
    """
    Public endpoint: Get a vendor's profile and their approved products.
    """
    vendor = (
        supabase.table("profiles")
        .select("id, full_name, display_name, farm_name, location, bio, rating, status")
        .eq("id", vendor_id)
        .eq("role", "vendor")
        .single()
        .execute()
    )
    if not vendor.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found.")

    products = (
        supabase.table("products")
        .select("id, name, category, price, stock, image_url, status")
        .eq("vendor_id", vendor_id)
        .eq("status", "Approved")
        .execute()
    )

    return {
        "vendor": vendor.data,
        "products": products.data or [],
    }


@router.patch("/{vendor_id}/status")
async def update_vendor_status(
    vendor_id: str,
    payload: VendorStatusUpdate,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["admin"]))
):
    """
    Admin: Approve, suspend, or reactivate a vendor account.
    """
    allowed = {"Active", "Suspended", "Pending Approval"}
    if payload.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {allowed}"
        )

    update_data = {"status": payload.status}
    if payload.reason:
        update_data["status_reason"] = payload.reason

    res = supabase_admin.table("profiles").update(update_data).eq("id", vendor_id).eq("role", "vendor").execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found.")

    # ── Email notification ────────────────────────────────────────────────
    vendor_data = res.data[0]
    v_email = vendor_data.get("email", "")
    v_name  = vendor_data.get("full_name", "Vendor")

    if payload.status == "Active":
        background_tasks.add_task(send_vendor_approved, v_email, v_name)
    elif payload.status == "Suspended":
        background_tasks.add_task(send_vendor_suspended, v_email, v_name, payload.reason)

    return {"message": f"Vendor status updated to '{payload.status}'.", "vendor_id": vendor_id}
