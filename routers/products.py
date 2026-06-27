"""
products.py — Product catalog routes for Farmers Market API

Endpoints:
  GET    /products/              — Public: browse all approved products (with filtering)
  POST   /products/              — Vendor/Admin: list a new product
  GET    /products/{id}          — Public: get a single product's detail
  PATCH  /products/{id}          — Vendor (own) / Admin: update a product
  PATCH  /products/{id}/image    — Vendor (own) / Admin: update only the product image URL
  DELETE /products/{id}          — Vendor (own) / Admin: remove a product
  PATCH  /products/{id}/status   — Admin: approve or reject a product listing
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query
from pydantic import BaseModel, HttpUrl
from typing import Optional
from dependencies import require_role, get_current_user
from database import supabase, supabase_admin
from services.email import (
    send_product_submitted,
    send_product_approved,
    send_product_rejected,
)

router = APIRouter(prefix="/products", tags=["products"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class ProductCreate(BaseModel):
    name: str
    category: str
    price: int        # in kobo
    stock: int
    origin: str
    description: Optional[str] = None
    image_url: Optional[str] = None

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    price: Optional[int] = None
    stock: Optional[int] = None
    origin: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None

class ProductStatusUpdate(BaseModel):
    status: str   # "Approved" | "Pending Approval" | "Rejected"
    reason: Optional[str] = None

class ProductImageUpdate(BaseModel):
    image_url: str   # Public URL returned by POST /uploads/image


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def get_products(
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    min_price: Optional[int] = Query(None),
    max_price: Optional[int] = Query(None),
    in_stock: Optional[bool] = Query(None),
):
    """
    Public: browse approved products with optional filtering.
    """
    query = supabase.table("products").select("*").eq("status", "Approved")

    if category:
        query = query.eq("category", category)
    if search:
        query = query.ilike("name", f"%{search}%")
    if min_price is not None:
        query = query.gte("price", min_price)
    if max_price is not None:
        query = query.lte("price", max_price)
    if in_stock:
        query = query.gt("stock", 0)

    res = query.order("created_at", desc=True).execute()
    return res.data or []


@router.post("/", status_code=status.HTTP_201_CREATED)
async def list_product(
    product: ProductCreate,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["vendor", "admin"]))
):
    """Vendors or Admins upload a new product listing."""
    product_data = product.model_dump()
    product_data["vendor_id"] = user.id
    # Admin uploads are auto-approved; vendor submissions go to Pending Approval
    profile = supabase.table("profiles").select("role, email, full_name").eq("id", user.id).single().execute()
    role = profile.data.get("role") if profile.data else "vendor"
    product_data["status"] = "Approved" if role == "admin" else "Pending Approval"

    res = supabase_admin.table("products").insert(product_data).execute()
    new_product = res.data[0]

    # ── Email notification (vendors only — admins are auto-approved) ─────────
    if role == "vendor" and profile.data:
        background_tasks.add_task(
            send_product_submitted,
            profile.data.get("email", ""),
            profile.data.get("full_name", "Vendor"),
            product.name,
            new_product.get("id", ""),
        )

    return {"message": "Product listing created successfully.", "data": new_product}


@router.get("/{product_id}")
async def get_product(product_id: str):
    """Public: get a single product's full detail."""
    res = supabase.table("products").select("*").eq("id", product_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return res.data


@router.patch("/{product_id}")
async def update_product(
    product_id: str,
    payload: ProductUpdate,
    user=Depends(get_current_user)
):
    """
    Update a product. Vendors may only update their own listings.
    Admins may update any product.
    """
    # Fetch existing product
    existing = supabase.table("products").select("vendor_id").eq("id", product_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    # Role check
    profile = supabase.table("profiles").select("role").eq("id", user.id).single().execute()
    role = profile.data.get("role") if profile.data else "customer"

    if role == "vendor" and existing.data.get("vendor_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own products.")
    if role == "customer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update.")

    res = supabase_admin.table("products").update(update_data).eq("id", product_id).execute()
    return {"message": "Product updated successfully.", "data": res.data[0] if res.data else None}


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: str, user=Depends(get_current_user)):
    """
    Delete a product. Vendors may only delete their own. Admins may delete any.
    """
    existing = supabase.table("products").select("vendor_id").eq("id", product_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    profile = supabase.table("profiles").select("role").eq("id", user.id).single().execute()
    role = profile.data.get("role") if profile.data else "customer"

    if role == "vendor" and existing.data.get("vendor_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own products.")
    if role == "customer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    supabase_admin.table("products").delete().eq("id", product_id).execute()
    return  # 204 No Content


@router.patch("/{product_id}/image")
async def update_product_image(
    product_id: str,
    payload: ProductImageUpdate,
    user=Depends(get_current_user),
):
    """
    Update the image URL of a product listing.

    Typical flow:
      1. Vendor calls POST /uploads/image with the image file  → receives a public URL
      2. Vendor calls PATCH /products/{id}/image with { "image_url": "<url>" }

    Vendors may only update images for their own products.
    Admins may update any product.
    """
    existing = supabase.table("products").select("vendor_id").eq("id", product_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    profile = supabase.table("profiles").select("role").eq("id", user.id).single().execute()
    role = profile.data.get("role") if profile.data else "customer"

    if role == "vendor" and existing.data.get("vendor_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own products.")
    if role == "customer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    res = supabase_admin.table("products").update({"image_url": payload.image_url}).eq("id", product_id).execute()
    return {"message": "Product image updated successfully.", "image_url": payload.image_url}


@router.patch("/{product_id}/status")
async def update_product_status(
    product_id: str,
    payload: ProductStatusUpdate,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["admin"]))
):
    """Admin only: approve, reject, or suspend a product listing."""
    allowed = {"Approved", "Pending Approval", "Rejected"}
    if payload.status not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Status must be one of: {allowed}")

    update_data = {"status": payload.status}
    if payload.reason:
        update_data["status_reason"] = payload.reason

    res = supabase_admin.table("products").update(update_data).eq("id", product_id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    # ── Email notification to the vendor ────────────────────────────────────
    product_data = res.data[0]
    vendor_id    = product_data.get("vendor_id")
    if vendor_id:
        vendor = (
            supabase.table("profiles")
            .select("email, full_name")
            .eq("id", vendor_id)
            .single()
            .execute()
        )
        if vendor.data:
            v_email = vendor.data.get("email", "")
            v_name  = vendor.data.get("full_name", "Vendor")
            p_name  = product_data.get("name", "Your product")
            p_price = product_data.get("price", 0)
            p_stock = product_data.get("stock", 0)

            if payload.status == "Approved":
                background_tasks.add_task(
                    send_product_approved,
                    v_email, v_name, p_name, product_id, p_price, p_stock,
                )
            elif payload.status == "Rejected":
                background_tasks.add_task(
                    send_product_rejected,
                    v_email, v_name, p_name, product_id, payload.reason,
                )

    return {"message": f"Product status set to '{payload.status}'.", "product_id": product_id}
