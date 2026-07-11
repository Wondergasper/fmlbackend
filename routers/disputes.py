"""
disputes.py — Structured Dispute Audit Trail routes for Farmers Market API

Endpoints:
  POST  /disputes/                   — Open a dispute for an order
  GET   /disputes/                   — List disputes (role-filtered)
  GET   /disputes/{dispute_id}        — Get dispute details with notes & evidence timeline
  POST  /disputes/{dispute_id}/notes  — Add a note to a dispute
  POST  /disputes/{dispute_id}/evidence — Add evidence photo/doc reference or upload file
  PATCH /disputes/{dispute_id}/resolve — Resolve dispute (admin only with notes & outcome, triggers wallet reconciliation)
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, status
from pydantic import BaseModel
from dependencies import get_current_user, require_role
from database import supabase, supabase_admin
from routers.uploads import _validate_image_bytes, _MAGIC

# Extend allowed types for dispute evidence to include PDF
_EVIDENCE_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    **_MAGIC,
    "application/pdf": [(0, b"%PDF")],
}

router = APIRouter(prefix="/disputes", tags=["disputes"])

DISPUTE_BUCKET = os.getenv("DISPUTE_STORAGE_BUCKET", "dispute-evidence")


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class DisputeCreate(BaseModel):
    order_id: str
    reason: str

class DisputeNoteCreate(BaseModel):
    note_text: Optional[str] = None
    note: Optional[str] = None

class DisputeEvidenceCreate(BaseModel):
    file_path: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = "image"
    description: Optional[str] = None

class DisputeResolve(BaseModel):
    resolution_outcome: str  # 'Refund Buyer' | 'Release to Vendor' | 'Split' | 'Closed' | 'Dismissed'
    resolution_notes: str


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def get_order_associated_vendor_ids(order_data: dict) -> set:
    """Extract all vendor IDs associated with items in an order."""
    vendor_ids = set()
    items = order_data.get("order_items") or []
    for item in items:
        if isinstance(item, dict):
            if item.get("vendor_id"):
                vendor_ids.add(item.get("vendor_id"))
            prod = item.get("products")
            if isinstance(prod, dict) and prod.get("vendor_id"):
                vendor_ids.add(prod.get("vendor_id"))
    return vendor_ids


def calculate_vendor_payouts(order_data: dict) -> dict:
    """
    Calculate payout amounts in kobo for each vendor involved in an order.
    """
    items = order_data.get("order_items") or []
    vendor_amounts = {}
    total_calculated = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        v_id = item.get("vendor_id")
        prod = item.get("products")
        if isinstance(prod, dict) and prod.get("vendor_id"):
            v_id = prod.get("vendor_id")

        if not v_id:
            continue

        qty = item.get("quantity", 1)
        price = item.get("unit_price_kobo") or item.get("unit_price") or item.get("price_kobo") or 0
        item_total = qty * price

        vendor_amounts[v_id] = vendor_amounts.get(v_id, 0) + item_total
        total_calculated += item_total

    total_kobo = order_data.get("total_kobo", 0)
    if not vendor_amounts:
        return {}

    if len(vendor_amounts) == 1:
        single_vendor = list(vendor_amounts.keys())[0]
        vendor_amounts[single_vendor] = total_kobo
    elif total_calculated == 0 and total_kobo > 0:
        share = total_kobo // len(vendor_amounts)
        for v_id in vendor_amounts:
            vendor_amounts[v_id] = share

    return vendor_amounts


async def verify_dispute_access(dispute: dict, user_id: str) -> str:
    """
    Verify that user_id has permission to access or modify a dispute.
    Returns the user's role if authorized, otherwise raises HTTP 403.
    Authorized users: dispute creator, order customer, associated vendor, or admin.
    """
    profile_res = supabase.table("profiles").select("role").eq("id", user_id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    user_role = profile_data.get("role") if profile_data else "customer"

    if user_role == "admin":
        return user_role

    # Check if user is dispute creator or opener
    if dispute.get("created_by") == user_id or dispute.get("opened_by") == user_id:
        return user_role

    # Fetch order details to check customer_id and associated vendors
    order_id = dispute.get("order_id")
    if order_id:
        order_data = dispute.get("orders")
        if not isinstance(order_data, dict) or not order_data.get("id"):
            order_res = supabase.table("orders").select("*, order_items(*, products(vendor_id))").eq("id", order_id).execute()
            order_data = order_res.data[0] if (order_res and order_res.data) else {}

        if isinstance(order_data, dict):
            if order_data.get("customer_id") == user_id:
                return user_role
            vendor_ids = get_order_associated_vendor_ids(order_data)
            if user_id in vendor_ids:
                return user_role

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: You are not authorized to access this dispute."
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/", status_code=status.HTTP_201_CREATED)
async def open_dispute(
    payload: DisputeCreate,
    user=Depends(get_current_user)
):
    """
    Open a dispute for a specified order (buyers or sellers).
    """
    # Verify order exists and fetch items/vendor information
    order_res = supabase.table("orders").select("*, order_items(*, products(vendor_id))").eq("id", payload.order_id).execute()
    if not order_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    order_data = order_res.data[0]

    # Check user role
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    user_role = profile_data.get("role") if profile_data else "customer"

    # Verify creating user is associated with the order (customer_id == user.id or vendor or admin)
    vendor_ids = get_order_associated_vendor_ids(order_data)
    is_customer = (order_data.get("customer_id") == user.id)
    is_vendor = (user.id in vendor_ids)
    is_admin = (user_role == "admin")

    if not (is_customer or is_vendor or is_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: You are not associated with this order."
        )

    # Check if an active dispute ('Open' or 'Under Review') already exists for this order
    existing_disp = supabase.table("disputes").select("id, status").eq("order_id", payload.order_id).in_("status", ["Open", "Under Review"]).execute()
    if existing_disp.data and len(existing_disp.data) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An active dispute already exists for this order."
        )

    dispute_data = {
        "order_id": payload.order_id,
        "created_by": user.id,
        "opened_by": user.id,
        "reason": payload.reason,
        "status": "Open"
    }
    res = supabase_admin.table("disputes").insert(dispute_data).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create dispute.")

    dispute = res.data[0]

    # Automatically create initial system note
    note_data = {
        "dispute_id": dispute["id"],
        "user_id": user.id,
        "sender_id": user.id,
        "user_role": user_role,
        "sender_role": user_role,
        "note_text": f"Dispute opened: {payload.reason}",
        "note": f"Dispute opened: {payload.reason}"
    }
    supabase_admin.table("dispute_notes").insert(note_data).execute()

    return {"message": "Dispute opened successfully.", "dispute": dispute}


@router.get("/")
async def list_disputes(user=Depends(get_current_user)):
    """
    List disputes relevant to caller's role.
    """
    profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    role = profile_data.get("role") if profile_data else "customer"

    if role == "admin":
        res = supabase.table("disputes").select("*, orders(*)").order("created_at", desc=True).execute()
    elif role == "vendor":
        # Allow vendors to view disputes for orders involving their products
        try:
            orders_res = (
                supabase.table("orders")
                .select("id, order_items!inner(products!inner(vendor_id))")
                .eq("order_items.products.vendor_id", user.id)
                .limit(100)
                .execute()
            )
            vendor_order_ids = [o["id"] for o in (orders_res.data or []) if o.get("id")]
        except Exception:
            vendor_order_ids = []

        if vendor_order_ids:
            res = supabase.table("disputes").select("*, orders(*)").in_("order_id", vendor_order_ids).order("created_at", desc=True).execute()
        else:
            res = supabase.table("disputes").select("*, orders(*)").or_(f"created_by.eq.{user.id},opened_by.eq.{user.id}").order("created_at", desc=True).execute()
    else:
        res = supabase.table("disputes").select("*, orders(*)").or_(f"created_by.eq.{user.id},opened_by.eq.{user.id}").order("created_at", desc=True).execute()

    return res.data or []


@router.get("/{dispute_id}")
async def get_dispute(dispute_id: str, user=Depends(get_current_user)):
    """
    Fetch comprehensive dispute record with notes and evidence.
    """
    dispute_res = supabase.table("disputes").select("*, orders(*, order_items(*, products(*)))").eq("id", dispute_id).execute()
    if not dispute_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found.")

    dispute = dispute_res.data[0]

    # Enforce strict authorization check
    await verify_dispute_access(dispute, user.id)

    # Fetch notes history in chronological order
    notes_res = supabase.table("dispute_notes").select("*, profiles(full_name, role)").eq("dispute_id", dispute_id).order("created_at", asc=True).execute()

    # Fetch evidence records
    evidence_res = supabase.table("dispute_evidence").select("*, profiles(full_name)").eq("dispute_id", dispute_id).order("created_at", asc=True).execute()

    dispute["notes"] = notes_res.data or []
    dispute["evidence"] = evidence_res.data or []

    return dispute


@router.post("/{dispute_id}/notes", status_code=status.HTTP_201_CREATED)
async def add_dispute_note(
    dispute_id: str,
    payload: DisputeNoteCreate,
    user=Depends(get_current_user)
):
    """
    Add a note or comment to an open dispute audit trail.
    """
    dispute_res = supabase.table("disputes").select("*, orders(*, order_items(*, products(*)))").eq("id", dispute_id).execute()
    if not dispute_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found.")

    dispute = dispute_res.data[0]

    # Enforce strict authorization check
    user_role = await verify_dispute_access(dispute, user.id)

    note_text = payload.note_text or payload.note
    if not note_text or not note_text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note text is required.")

    note_data = {
        "dispute_id": dispute_id,
        "user_id": user.id,
        "sender_id": user.id,
        "user_role": user_role,
        "sender_role": user_role,
        "note_text": note_text.strip(),
        "note": note_text.strip()
    }
    res = supabase_admin.table("dispute_notes").insert(note_data).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to add note.")

    return {"message": "Note added to dispute audit trail.", "note": res.data[0]}


@router.post("/{dispute_id}/evidence", status_code=status.HTTP_201_CREATED)
async def add_dispute_evidence(
    dispute_id: str,
    request: Request,
    file: Optional[UploadFile] = File(None),
    description: Optional[str] = Form(None),
    user=Depends(get_current_user)
):
    """
    Attach photo or document evidence to a dispute (via multipart upload or JSON payload).
    """
    dispute_res = supabase.table("disputes").select("*, orders(*, order_items(*, products(*)))").eq("id", dispute_id).execute()
    if not dispute_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found.")

    dispute = dispute_res.data[0]

    # Enforce strict authorization check
    await verify_dispute_access(dispute, user.id)

    storage_path = ""
    public_url = ""
    file_type = "image"
    desc = description

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = await request.json()
        public_url = body.get("file_url") or body.get("url") or ""
        storage_path = body.get("file_path") or body.get("path") or public_url
        file_type = body.get("file_type") or "image"
        if not desc:
            desc = body.get("description")
    elif file is not None:
        file_bytes = await file.read()
        ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
        storage_path = f"disputes/{dispute_id}/{uuid.uuid4().hex}.{ext}"
        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        public_url = f"{supabase_url}/storage/v1/object/public/{DISPUTE_BUCKET}/{storage_path}"

        # Validate file content via magic bytes before storing
        declared_mime = file.content_type or "image/jpeg"
        evidence_sigs = _EVIDENCE_MAGIC.get(declared_mime)
        if not evidence_sigs:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Unsupported file type '{declared_mime}'. Allowed: JPEG, PNG, WebP, PDF.",
            )
        for offset, signature in evidence_sigs:
            end = offset + len(signature)
            if len(file_bytes) < end or file_bytes[offset:end] != signature:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=(
                        f"File content does not match declared type '{declared_mime}'. "
                        "Please upload a valid image or PDF document."
                    ),
                )

        try:
            supabase_admin.storage.from_(DISPUTE_BUCKET).upload(
                path=storage_path,
                file=file_bytes,
                file_options={"content-type": file.content_type or "application/octet-stream", "upsert": "true"}
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Storage upload failed: {str(exc)}"
            )
    else:
        try:
            form = await request.form()
            public_url = str(form.get("file_url") or form.get("url") or "")
            storage_path = str(form.get("file_path") or form.get("path") or public_url)
            if not desc:
                desc = str(form.get("description") or "")
        except Exception:
            pass

    if not public_url and not storage_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence file, file_url, or file_path is required."
        )

    evidence_data = {
        "dispute_id": dispute_id,
        "uploaded_by": user.id,
        "file_path": storage_path,
        "file_url": public_url,
        "file_type": file_type,
        "description": desc
    }
    res = supabase_admin.table("dispute_evidence").insert(evidence_data).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to record evidence.")

    return {"message": "Evidence attached to dispute.", "evidence": res.data[0]}


@router.patch("/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: str,
    payload: DisputeResolve,
    user=Depends(require_role(["admin"]))
):
    """
    Admin endpoint to log resolution notes and select outcome before closing a dispute.
    Triggers automated wallet reconciliation based on resolution_outcome.
    """
    notes_clean = payload.resolution_notes.strip() if payload.resolution_notes else ""
    if not notes_clean or not payload.resolution_outcome:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Resolution notes and outcome are strictly required to resolve a dispute."
        )

    valid_outcomes = {"Refund Buyer", "Release to Vendor", "Split", "Closed", "Dismissed"}
    if payload.resolution_outcome not in valid_outcomes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid outcome. Must be one of: {valid_outcomes}"
        )

    disp_res = supabase.table("disputes").select("*").eq("id", dispute_id).execute()
    if not disp_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found.")

    dispute_rec = disp_res.data[0]
    if dispute_rec.get("status") in ["Resolved", "Closed"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dispute is already resolved.")

    # ── Financial Reconciliation Integration ──────────────────────────────────────
    order_id = dispute_rec.get("order_id")
    if order_id and payload.resolution_outcome in ["Refund Buyer", "Release to Vendor", "Split"]:
        try:
            order_res = supabase.table("orders").select("*, order_items(*, products(vendor_id))").eq("id", order_id).execute()
            if not order_res or not order_res.data:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Associated order not found for financial reconciliation.")

            order_data = order_res.data[0]
            customer_id = order_data.get("customer_id")
            total_kobo = order_data.get("total_kobo", 0)
            outcome = payload.resolution_outcome

            vendor_payouts = calculate_vendor_payouts(order_data)

            if outcome == "Refund Buyer":
                if not customer_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer ID missing on order.")
                p_res = supabase.table("profiles").select("wallet_balance").eq("id", customer_id).execute()
                p_data = p_res.data[0] if p_res.data else None
                c_bal = p_data.get("wallet_balance", 0) if p_data else 0
                new_bal = c_bal + total_kobo
                upd = supabase_admin.table("profiles").update({"wallet_balance": new_bal}).eq("id", customer_id).execute()
                if not upd.data:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update customer wallet balance.")
                tx = supabase_admin.table("wallet_transactions").insert({
                    "user_id": customer_id,
                    "type": "Refund",
                    "amount_kobo": total_kobo,
                    "reference": f"dispute_refund_{dispute_id}",
                    "status": "Success",
                    "description": f"Dispute refund for order {order_id}: {notes_clean}"
                }).execute()
                if not tx.data:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to log customer refund transaction.")

            elif outcome == "Release to Vendor":
                if not vendor_payouts:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No vendor associated with order items.")
                for v_id, amt in vendor_payouts.items():
                    p_res = supabase.table("profiles").select("wallet_balance").eq("id", v_id).execute()
                    p_data = p_res.data[0] if p_res.data else None
                    v_bal = p_data.get("wallet_balance", 0) if p_data else 0
                    new_bal = v_bal + amt
                    upd = supabase_admin.table("profiles").update({"wallet_balance": new_bal}).eq("id", v_id).execute()
                    if not upd.data:
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update vendor {v_id} wallet balance.")
                    tx = supabase_admin.table("wallet_transactions").insert({
                        "user_id": v_id,
                        "type": "Credit",
                        "amount_kobo": amt,
                        "reference": f"dispute_payout_{dispute_id}_{v_id}",
                        "status": "Success",
                        "description": f"Dispute payout for order {order_id}: {notes_clean}"
                    }).execute()
                    if not tx.data:
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to log vendor {v_id} payout transaction.")

            elif outcome == "Split":
                if not customer_id or not vendor_payouts:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing customer or vendor information for split.")
                cust_split = total_kobo // 2
                vendor_total_split = total_kobo - cust_split

                p_cust = supabase.table("profiles").select("wallet_balance").eq("id", customer_id).execute()
                p_cust_data = p_cust.data[0] if p_cust.data else None
                c_bal = p_cust_data.get("wallet_balance", 0) if p_cust_data else 0
                upd_c = supabase_admin.table("profiles").update({"wallet_balance": c_bal + cust_split}).eq("id", customer_id).execute()
                if not upd_c.data:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update customer split wallet balance.")
                tx_c = supabase_admin.table("wallet_transactions").insert({
                    "user_id": customer_id,
                    "type": "Refund",
                    "amount_kobo": cust_split,
                    "reference": f"dispute_split_{dispute_id}",
                    "status": "Success",
                    "description": f"Dispute split refund for order {order_id}"
                }).execute()
                if not tx_c.data:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to log customer split transaction.")

                total_vendor_item_amt = sum(vendor_payouts.values()) or 1
                for v_id, orig_amt in vendor_payouts.items():
                    v_split = (orig_amt * vendor_total_split) // total_vendor_item_amt
                    p_vend = supabase.table("profiles").select("wallet_balance").eq("id", v_id).execute()
                    p_vend_data = p_vend.data[0] if p_vend.data else None
                    v_bal = p_vend_data.get("wallet_balance", 0) if p_vend_data else 0
                    upd_v = supabase_admin.table("profiles").update({"wallet_balance": v_bal + v_split}).eq("id", v_id).execute()
                    if not upd_v.data:
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update vendor {v_id} split wallet balance.")
                    tx_v = supabase_admin.table("wallet_transactions").insert({
                        "user_id": v_id,
                        "type": "Credit",
                        "amount_kobo": v_split,
                        "reference": f"dispute_split_{dispute_id}_{v_id}",
                        "status": "Success",
                        "description": f"Dispute split payout for order {order_id}"
                    }).execute()
                    if not tx_v.data:
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to log vendor {v_id} split transaction.")

        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Financial reconciliation failed: {str(exc)}"
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    update_data = {
        "status": "Resolved",
        "resolution_outcome": payload.resolution_outcome,
        "resolution_notes": notes_clean,
        "resolved_by": user.id,
        "resolved_at": now_iso,
        "updated_at": now_iso
    }

    res = supabase_admin.table("disputes").update(update_data).eq("id", dispute_id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update dispute status.")

    dispute_rec = res.data[0]

    note_data = {
        "dispute_id": dispute_id,
        "user_id": user.id,
        "sender_id": user.id,
        "user_role": "admin",
        "sender_role": "admin",
        "note_text": f"[RESOLVED - Outcome: {payload.resolution_outcome}] {notes_clean}",
        "note": f"[RESOLVED - Outcome: {payload.resolution_outcome}] {notes_clean}"
    }
    supabase_admin.table("dispute_notes").insert(note_data).execute()

    return {"message": f"Dispute resolved with outcome '{payload.resolution_outcome}'.", "dispute": dispute_rec}
