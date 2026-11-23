"""Product publication and catalog access."""
import logging
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ConfigDict
from dependencies import require_role
from database import supabase, supabase_admin
from services.email import send_product_submitted, send_product_approved, send_product_rejected
from services.notification_service import create_notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["products"])
NonnegativeInt = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    price: Annotated[int, Field(strict=True, gt=0, le=9007199254740991)]
    stock: NonnegativeInt
    origin: str = Field(min_length=1, max_length=200)
    description: str | None = None
    image_url: str | None = None
    unit: str | None = None
    is_organic: bool | None = None
    harvest_date: str | None = None


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    price: Annotated[int, Field(strict=True, gt=0, le=9007199254740991)] | None = None
    stock: NonnegativeInt | None = None
    origin: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    image_url: str | None = None
    unit: str | None = None
    is_organic: bool | None = None
    harvest_date: str | None = None


class ProductStatusUpdate(BaseModel):
    status: Literal["Approved", "Pending Approval", "Rejected"]
    reason: str | None = None


class ProductImageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str


def _first_row(res):
    data = res.data if res else None
    return data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None


def catalog_query():
    # !inner removes rows whose vendor does not match the active filter.
    return (supabase_admin.table("products")
            .select("*, vendor:profiles!vendor_id!inner(status, farm_name, full_name, location)")
            .eq("status", "Approved").eq("vendor.status", "Active").gt("stock", 0))


@router.get("/")
async def get_products(category: str | None = None, search: str | None = None,
                       min_price: int | None = None, max_price: int | None = None,
                       in_stock: bool | None = None):
    query = catalog_query()
    if category:
        query = query.eq("category", category)
    if search:
        query = query.ilike("name", f"%{search}%")
    if min_price is not None:
        query = query.gte("price", min_price)
    if max_price is not None:
        query = query.lte("price", max_price)
    return query.order("created_at", desc=True).execute().data or []


@router.get("/me")
async def get_my_products(user=Depends(require_role(["vendor"]))):
    return (supabase_admin.table("products").select("*").eq("vendor_id", user.id)
            .order("created_at", desc=True).execute().data or [])


class ProductBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=100)


class CartItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    quantity: Annotated[int, Field(strict=True, gt=0)]


class CartValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CartItem] = Field(min_length=1, max_length=100)


@router.post("/validate-cart")
async def validate_cart(payload: CartValidation):
    from collections import Counter
    quantities = Counter()
    for item in payload.items:
        quantities[item.product_id] += item.quantity
    available = {row["id"]: row for row in catalog_query().in_("id", list(quantities)).execute().data or []}
    unavailable = [pid for pid, qty in quantities.items() if pid not in available or available[pid]["stock"] < qty]
    return {"valid": not unavailable, "unavailable_product_ids": unavailable,
            "items": [row for pid, row in available.items() if pid not in unavailable]}


@router.post("/batch")
async def batch_products(payload: ProductBatch):
    return catalog_query().in_("id", payload.ids).execute().data or []


@router.post("/", status_code=status.HTTP_201_CREATED)
async def list_product(product: ProductCreate, user=Depends(require_role(["vendor"]))):
    data = product.model_dump(exclude_none=True)
    data.update(vendor_id=user.id, status="Pending Approval")
    row = _first_row(supabase_admin.table("products").insert(data).execute())
    if not row:
        raise HTTPException(503, "Product could not be saved.")
    try:
        profile = getattr(user, "profile", None) or {}
        send_product_submitted.delay(profile.get("email", ""), profile.get("full_name", "Vendor"), product.name, row["id"])
    except Exception:
        logger.warning("Product submission notification could not be queued")
    return {"message": "Product listing created successfully.", "data": row}


@router.get("/{product_id}")
async def get_product(product_id: str):
    row = _first_row(catalog_query().eq("id", product_id).execute())
    if not row:
        raise HTTPException(404, "Product not found.")
    return row


def _authorize_product(product_id, user):
    row = _first_row(supabase_admin.table("products").select("*").eq("id", product_id).execute())
    if not row:
        raise HTTPException(404, "Product not found.")
    # Recheck DB even when invoked outside FastAPI dependency resolution.
    profile = _first_row(supabase_admin.table("profiles").select("role").eq("id", user.id).execute())
    from schemas.auth import canonicalize_role
    role = canonicalize_role(profile.get("role")) if profile else None
    if role not in {"vendor", "admin"} or (role == "vendor" and row["vendor_id"] != user.id):
        raise HTTPException(403, "You cannot modify this product.")
    return row, role


async def _edit_product(product_id, updates, user):
    existing, role = _authorize_product(product_id, user)
    if not updates:
        raise HTTPException(400, "No fields provided to update.")
    if role == "vendor":
        updates["status"] = "Pending Approval"
    query = supabase_admin.table("products").update(updates).eq("id", product_id)
    if role == "vendor":
        query = query.eq("vendor_id", user.id)
    row = _first_row(query.execute())
    if not row:
        raise HTTPException(409, "Product changed or was removed; reload and retry.")
    return {"message": "Product updated successfully.", "data": row}


@router.patch("/{product_id}")
async def update_product(product_id: str, payload: ProductUpdate,
                         user=Depends(require_role(["vendor", "admin"]))):
    return await _edit_product(product_id, payload.model_dump(exclude_none=True), user)


@router.patch("/{product_id}/image")
async def update_product_image(product_id: str, payload: ProductImageUpdate,
                               user=Depends(require_role(["vendor", "admin"]))):
    result = await _edit_product(product_id, {"image_url": payload.image_url}, user)
    return {**result, "image_url": payload.image_url}


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: str, user=Depends(require_role(["vendor", "admin"]))):
    _, role = _authorize_product(product_id, user)
    query = supabase_admin.table("products").delete().eq("id", product_id)
    if role == "vendor":
        query = query.eq("vendor_id", user.id)
    query.execute()


@router.patch("/{product_id}/status")
async def update_product_status(product_id: str, payload: ProductStatusUpdate,
                                user=Depends(require_role(["admin"]))):
    updates = {"status": payload.status, "status_reason": payload.reason}
    query = supabase_admin.table("products").update(updates).eq("id", product_id)
    if payload.status == "Approved":
        # Single conditional mutation: zero-stock publication never changes state.
        query = query.gt("stock", 0)
    row = _first_row(query.execute())
    if not row:
        raise HTTPException(409, "Product missing or has no stock available for publication.")
    try:
        vendor = _first_row(supabase_admin.table("profiles").select("email, full_name").eq("id", row["vendor_id"]).execute())
        if vendor:
            args = (vendor.get("email", ""), vendor.get("full_name", "Vendor"), row["name"], product_id)
            if payload.status == "Approved":
                send_product_approved.delay(*args, row["price"], row["stock"])
            elif payload.status == "Rejected":
                send_product_rejected.delay(*args, payload.reason)
        await create_notification(user_id=row["vendor_id"], title=f"Product {payload.status}",
                                  message=f"Your product '{row['name']}' is {payload.status}.",
                                  type="product", link="/farmer/dashboard",
                                  metadata={"product_id": product_id, "status": payload.status})
    except Exception:
        logger.warning("Product publication notification failed")
    return {"message": f"Product status set to '{payload.status}'.", "product_id": product_id}
