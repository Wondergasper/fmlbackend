"""Payment service — wallet operations and Paystack integration.

Provides reusable functions for wallet credit/debit, balance checks,
and transaction logging. Designed for unit testing by accepting
supabase clients as parameters.
"""

from database import supabase, supabase_admin


def get_wallet_balance(user_id: str) -> int:
    """Get the current wallet balance for a user (in kobo)."""
    res = supabase.table("profiles").select("wallet_balance").eq("id", user_id).execute()
    if not res.data:
        raise ValueError(f"Profile not found for user {user_id}")
    return res.data[0].get("wallet_balance", 0)


def credit_wallet(user_id: str, amount_kobo: int, reference: str, description: str = "TopUp") -> int:
    """Credit a user's wallet and record the transaction. Returns new balance."""
    if amount_kobo <= 0:
        raise ValueError("Amount must be positive")

    # Check duplicate reference (idempotency)
    existing = supabase_admin.table("wallet_transactions").select("id").eq("reference", reference).execute()
    if existing.data:
        raise ValueError(f"Duplicate transaction reference: {reference}")

    current = get_wallet_balance(user_id)
    new_balance = current + amount_kobo

    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user_id).execute()
    supabase_admin.table("wallet_transactions").insert({
        "user_id": user_id,
        "type": description,
        "amount_kobo": amount_kobo,
        "reference": reference,
        "status": "Success",
        "description": f"Wallet credit: {description}",
    }).execute()

    return new_balance


def debit_wallet(user_id: str, amount_kobo: int, reference: str, description: str = "Payment") -> int | None:
    """Debit a user's wallet if sufficient balance exists. Returns new balance or None."""
    if amount_kobo <= 0:
        raise ValueError("Amount must be positive")

    current = get_wallet_balance(user_id)
    if current < amount_kobo:
        return None

    new_balance = current - amount_kobo
    supabase_admin.table("profiles").update({"wallet_balance": new_balance}).eq("id", user_id).execute()
    supabase_admin.table("wallet_transactions").insert({
        "user_id": user_id,
        "type": description,
        "amount_kobo": -amount_kobo,
        "reference": reference,
        "status": "Success",
        "description": f"Wallet debit: {description}",
    }).execute()

    return new_balance


def refund_wallet(user_id: str, amount_kobo: int, reference: str, description: str = "Refund") -> int:
    """Refund a user (credit wallet on cancellation). Returns new balance."""
    return credit_wallet(user_id, amount_kobo, reference, f"Refund: {description}")
