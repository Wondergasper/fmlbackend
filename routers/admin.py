"""
admin.py — Admin platform configuration routes for Farmers Market API

Endpoints:
  GET  /admin/config    — Get current platform configuration
  PATCH /admin/config   — Update platform configuration (fees, bonuses, gateway mode)
  GET  /admin/categories    — List all product categories
  POST /admin/categories    — Add a new product category
  DELETE /admin/categories  — Remove a product category
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from dependencies import require_role, get_current_user
from database import supabase, supabase_admin

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class PlatformConfigUpdate(BaseModel):
    platform_fees: Optional[int] = None
    delivery_base_fee: Optional[int] = None
    delivery_express_fee: Optional[int] = None
    gateway_mode: Optional[str] = None
    signup_bonus: Optional[int] = None
    farmer_rewards_rate: Optional[int] = None


class CategoryCreate(BaseModel):
    name: str


class CategoryDelete(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Platform Config
# ---------------------------------------------------------------------------

CONFIG_ID = "platform-config"

@router.get("/config")
async def get_platform_config(user=Depends(require_role(["admin"]))):
    """
    Return the current platform configuration.
    """
    res = supabase_admin.table("platform_config").select("*").eq("id", CONFIG_ID).execute()
    if not res.data:
        return {
            "platform_fees": 5,
            "delivery_base_fee": 85000,
            "delivery_express_fee": 150000,
            "gateway_mode": "Sandbox",
            "signup_bonus": 50000,
            "farmer_rewards_rate": 2,
        }
    return res.data[0]


@router.patch("/config")
async def save_platform_config(
    payload: PlatformConfigUpdate,
    user=Depends(require_role(["admin"]))
):
    """
    Persist platform-wide configuration (fees, bonuses, gateway mode).
    Called by the admin settings tab.
    """
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update.")

    res = supabase_admin.table("platform_config").upsert(
        {"id": CONFIG_ID, **update_data},
        on_conflict="id"
    ).execute()

    return {"message": "Platform configuration saved successfully.", "data": res.data[0] if res.data else update_data}


# ---------------------------------------------------------------------------
# Category Management
# ---------------------------------------------------------------------------

@router.get("/categories")
async def list_categories(user=Depends(get_current_user)):
    """
    List all product categories. Available to all authenticated users.
    """
    res = supabase.table("categories").select("*").order("name", asc=True).execute()
    return [c["name"] for c in (res.data or [])]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def add_category(
    payload: CategoryCreate,
    user=Depends(require_role(["admin"]))
):
    """
    Add a new product category.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Category name is required.")

    existing = supabase.table("categories").select("id").eq("name", name).execute()
    if existing.data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category already exists.")

    res = supabase_admin.table("categories").insert({"name": name}).execute()
    return {"message": f"Category '{name}' added.", "data": res.data[0] if res.data else {"name": name}}


@router.delete("/categories", status_code=status.HTTP_200_OK)
async def remove_category(
    payload: CategoryDelete,
    user=Depends(require_role(["admin"]))
):
    """
    Remove a product category.
    """
    name = payload.name.strip()
    res = supabase_admin.table("categories").delete().eq("name", name).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found.")
    return {"message": f"Category '{name}' removed."}
