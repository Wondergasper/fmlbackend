"""
wallet.py — Customer wallet routes for Farmers Market API

Endpoints:
  GET  /wallet/balance    — Get the authenticated customer's wallet balance
  POST /wallet/topup      — Top up the wallet by a given amount
  GET  /wallet/history    — Get wallet transaction history
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from dependencies import get_current_user, require_role
from database import supabase, supabase_admin
from services.email import send_wallet_topup_receipt
from services.payments import (
    initialize_paystack_transaction,
    verify_paystack_transaction,
    verify_paystack_webhook_signature,
)

router = APIRouter(prefix="/wallet", tags=["wallet"])

# Limits (in kobo)
MIN_TOPUP_KOBO = 100_00      # ₦100 minimum top-up
MAX_TOPUP_KOBO = 500_000_00  # ₦500,000 maximum per transaction


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class TopupRequest(BaseModel):
    amount_kobo: int   # Amount to add (in kobo, e.g. 500000 = ₦5,000)
    reference: str     # Payment gateway transaction reference

    @field_validator("amount_kobo")
    @classmethod
    def validate_amount(cls, v):
        if v < MIN_TOPUP_KOBO:
            raise ValueError(f"Minimum top-up is ₦{MIN_TOPUP_KOBO // 100:,}")
        if v > MAX_TOPUP_KOBO:
            raise ValueError(f"Maximum top-up per transaction is ₦{MAX_TOPUP_KOBO // 100:,}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/balance")
async def get_balance(user=Depends(get_current_user)):
    """
    Return the current wallet balance for the authenticated user.
    """
    profile = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    balance_kobo = profile.data.get("wallet_balance", 0)
    return {
        "balance_kobo": balance_kobo,
        "balance_naira": balance_kobo / 100,
        "formatted": f"₦{balance_kobo / 100:,.2f}",
    }


class PaystackInitRequest(BaseModel):
    amount_kobo: int

    @field_validator("amount_kobo")
    @classmethod
    def validate_amount(cls, v):
        if v < MIN_TOPUP_KOBO:
            raise ValueError(f"Minimum top-up is ₦{MIN_TOPUP_KOBO // 100:,}")
        if v > MAX_TOPUP_KOBO:
            raise ValueError(f"Maximum top-up per transaction is ₦{MAX_TOPUP_KOBO // 100:,}")
        return v


@router.post("/topup")
async def topup_wallet(
    payload: TopupRequest,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(["customer"]))
):
    """
    Credit the customer's wallet by the specified amount.
    In production, validate the payment gateway reference before crediting.
    """
    # Fetch current balance
    profile = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    current_balance = profile.data.get("wallet_balance", 0)
    new_balance = current_balance + payload.amount_kobo

    # Update balance (admin client to bypass RLS)
    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user.id).execute()

    # Log the transaction
    tx_data = {
        "user_id": user.id,
        "type": "TopUp",
        "amount_kobo": payload.amount_kobo,
        "reference": payload.reference,
        "status": "Success",
        "description": f"Wallet top-up via payment reference {payload.reference}",
    }
    supabase_admin.table("wallet_transactions").insert(tx_data).execute()

    # ── Email notification ────────────────────────────────────────────────
    cust = (
        supabase.table("profiles")
        .select("email, full_name")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if cust.data:
        background_tasks.add_task(
            send_wallet_topup_receipt,
            cust.data.get("email", ""),
            cust.data.get("full_name", "Customer"),
            payload.amount_kobo,
            payload.reference,
            new_balance,
        )

    return {
        "message": "Wallet topped up successfully.",
        "amount_added_kobo": payload.amount_kobo,
        "new_balance_kobo": new_balance,
        "new_balance_naira": new_balance / 100,
        "formatted": f"₦{new_balance / 100:,.2f}",
    }


@router.post("/paystack/init")
async def init_paystack_topup(
    payload: PaystackInitRequest,
    user=Depends(require_role(["customer"]))
):
    """Initialize a Paystack transaction for a wallet top-up."""
    profile = (
        supabase.table("profiles")
        .select("email")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    try:
        init_data = initialize_paystack_transaction(profile.data["email"], payload.amount_kobo, user.id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return {
        "authorization_url": init_data.get("authorization_url"),
        "access_code": init_data.get("access_code"),
        "reference": init_data.get("reference"),
    }


@router.post("/paystack/verify")
async def verify_paystack_topup(payload: TopupRequest, background_tasks: BackgroundTasks, user=Depends(require_role(["customer"]))):
    """Verify Paystack transaction and credit wallet on success."""
    try:
        txn = verify_paystack_transaction(payload.reference)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    if txn.get("status") != "success":
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Payment not successful.")

    metadata = txn.get("metadata") or {}
    if metadata.get("purpose") != "wallet_topup" or metadata.get("user_id") != user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment metadata does not match user.")

    amount_kobo = txn.get("amount", 0)
    profile = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    new_balance = profile.data.get("wallet_balance", 0) + amount_kobo
    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user.id).execute()
    supabase_admin.table("wallet_transactions").insert({
        "user_id": user.id,
        "type": "TopUp",
        "amount_kobo": amount_kobo,
        "reference": payload.reference,
        "status": "Success",
        "description": "Wallet top-up via Paystack",
    }).execute()

    cust = (
        supabase.table("profiles")
        .select("email, full_name")
        .eq("id", user.id)
        .single()
        .execute()
    )
    if cust.data:
        background_tasks.add_task(
            send_wallet_topup_receipt,
            cust.data.get("email", ""),
            cust.data.get("full_name", "Customer"),
            amount_kobo,
            payload.reference,
            new_balance,
        )

    return {
        "message": "Payment verified and wallet updated.",
        "new_balance_kobo": new_balance,
        "new_balance_naira": new_balance / 100,
    }


@router.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    """Handle Paystack webhook callbacks for transaction verification."""
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    if not verify_paystack_webhook_signature(signature, raw_body):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    event = await request.json()
    if event.get("event") != "charge.success":
        return {"received": True}

    data = event.get("data", {})
    reference = data.get("reference")
    metadata = data.get("metadata", {})
    if metadata.get("purpose") != "wallet_topup":
        return {"received": True}

    user_id = metadata.get("user_id")
    amount_kobo = data.get("amount", 0)
    if not user_id or amount_kobo <= 0:
        return {"received": True}

    profile = (
        supabase.table("profiles")
        .select("wallet_balance")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if profile.data:
        new_balance = profile.data.get("wallet_balance", 0) + amount_kobo
        supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user_id).execute()
        supabase_admin.table("wallet_transactions").insert({
            "user_id": user_id,
            "type": "TopUp",
            "amount_kobo": amount_kobo,
            "reference": reference,
            "status": "Success",
            "description": "Wallet top-up via Paystack webhook",
        }).execute()

    return {"received": True}


@router.get("/history")
async def get_wallet_history(user=Depends(get_current_user)):
    """
    Return the transaction history for the authenticated user's wallet.
    """
    res = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data or []
