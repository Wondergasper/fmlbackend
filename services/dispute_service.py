"""Dispute service — dispute resolution and financial reconciliation.

Extracts payout calculation logic from the disputes router for testability.
"""

from database import supabase, supabase_admin


def calculate_vendor_payouts(dispute_id: str) -> list[dict]:
    """Calculate how much each vendor should receive when resolving a dispute.

    Returns a list of {vendor_id, amount_kobo} dicts prorated by order item value.
    """
    dispute_res = supabase_admin.table("disputes").select("order_id").eq("id", dispute_id).execute()
    if not dispute_res.data:
        raise ValueError("Dispute not found")
    order_id = dispute_res.data[0]["order_id"]

    items_res = (
        supabase_admin.table("order_items")
        .select("product_id, quantity, unit_price_kobo, products!inner(vendor_id)")
        .eq("order_id", order_id)
        .execute()
    )
    items = items_res.data or []

    # Group by vendor
    vendor_totals: dict[str, int] = {}
    total_value = 0
    for item in items:
        vid = item.get("products", {}).get("vendor_id")
        line = item.get("unit_price_kobo", 0) * item.get("quantity", 1)
        total_value += line
        if vid:
            vendor_totals[vid] = vendor_totals.get(vid, 0) + line

    order_res = supabase_admin.table("orders").select("total_kobo").eq("id", order_id).execute()
    order_total = order_res.data[0]["total_kobo"] if order_res.data else total_value

    # Prorate by vendor share
    payouts = []
    for vid, vtotal in vendor_totals.items():
        if total_value > 0:
            share = int(order_total * vtotal / total_value)
            payouts.append({"vendor_id": vid, "amount_kobo": share})
    return payouts


def refund_buyer(dispute_id: str, order_id: str) -> dict:
    """Refund the full order total to the customer's wallet."""
    order_res = supabase_admin.table("orders").select("customer_id, total_kobo").eq("id", order_id).execute()
    if not order_res.data:
        raise ValueError("Order not found")
    order = order_res.data[0]
    cust_id = order["customer_id"]
    total = order["total_kobo"]

    profile_res = supabase_admin.table("profiles").select("wallet_balance").eq("id", cust_id).execute()
    cur_bal = profile_res.data[0]["wallet_balance"] if profile_res.data else 0
    supabase_admin.table("profiles").update({"wallet_balance": cur_bal + total}).eq("id", cust_id).execute()

    supabase_admin.table("wallet_transactions").insert({
        "user_id": cust_id,
        "type": "Refund",
        "amount_kobo": total,
        "reference": f"dispute_refund_{dispute_id}",
        "status": "Success",
        "description": f"Dispute resolution refund for order {order_id}",
    }).execute()

    return {"customer_id": cust_id, "amount_kobo": total}


def release_to_vendor(dispute_id: str, order_id: str) -> list[dict]:
    """Release payment to vendors prorated by their share of the order."""
    payouts = calculate_vendor_payouts(dispute_id)
    results = []
    for payout in payouts:
        vid = payout["vendor_id"]
        amount = payout["amount_kobo"]
        profile_res = supabase_admin.table("profiles").select("wallet_balance").eq("id", vid).execute()
        cur_bal = profile_res.data[0]["wallet_balance"] if profile_res.data else 0
        supabase_admin.table("profiles").update({"wallet_balance": cur_bal + amount}).eq("id", vid).execute()

        supabase_admin.table("wallet_transactions").insert({
            "user_id": vid,
            "type": "Payout",
            "amount_kobo": amount,
            "reference": f"dispute_release_{dispute_id}_{vid}",
            "status": "Success",
            "description": f"Dispute resolution release for order {order_id}",
        }).execute()
        results.append({"vendor_id": vid, "amount_kobo": amount})
    return results


def split_payment(dispute_id: str, order_id: str) -> dict:
    """Split payment 50/50 between customer and vendors."""
    order_res = supabase_admin.table("orders").select("customer_id, total_kobo").eq("id", order_id).execute()
    if not order_res.data:
        raise ValueError("Order not found")
    order = order_res.data[0]
    cust_id = order["customer_id"]
    total = order["total_kobo"]

    half = total // 2
    # Refund half to customer
    profile_res = supabase_admin.table("profiles").select("wallet_balance").eq("id", cust_id).execute()
    cur_bal = profile_res.data[0]["wallet_balance"] if profile_res.data else 0
    supabase_admin.table("profiles").update({"wallet_balance": cur_bal + half}).eq("id", cust_id).execute()

    # Release other half to vendors
    payouts = calculate_vendor_payouts(dispute_id)
    vendor_total = 0
    for payout in payouts:
        vid = payout["vendor_id"]
        amount = int(half * payout["amount_kobo"] / (sum(p["amount_kobo"] for p in payouts) or 1))
        vendor_total += amount
        profile_res = supabase_admin.table("profiles").select("wallet_balance").eq("id", vid).execute()
        cur_bal = profile_res.data[0]["wallet_balance"] if profile_res.data else 0
        supabase_admin.table("profiles").update({"wallet_balance": cur_bal + amount}).eq("id", vid).execute()

    return {"customer_refund_kobo": half, "vendor_total_kobo": vendor_total}
