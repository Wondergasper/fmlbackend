"""Order service — order creation, stock management, wallet deduction.

Extracts business logic from the thick orders router into testable service functions.
"""

from database import supabase, supabase_admin


def verify_product_prices(items: list[dict]) -> tuple[int, dict]:
    """Verify product prices against DB and calculate total. Returns (total_kobo, db_products_map)."""
    product_ids = list(set(item["product_id"] for item in items))
    products_res = (
        supabase_admin.table("products")
        .select("id, price, stock, status, vendor_id, name")
        .eq("status", "Approved")
        .in_("id", product_ids)
        .execute()
    )
    db_products = {p["id"]: p for p in (products_res.data or [])}

    for item in items:
        db_prod = db_products.get(item["product_id"])
        if not db_prod:
            raise ValueError(f"Product {item['product_id']} is not available.")
        if db_prod["stock"] < item["quantity"]:
            raise ValueError(f"Insufficient stock for '{db_prod.get('name', item['product_id'])}'.")

    total_kobo = sum(
        db_products[item["product_id"]]["price"] * item["quantity"]
        for item in items
    )
    return total_kobo, db_products


def deduct_stock(items: list[dict], db_products: dict):
    """Deduct stock for each ordered item."""
    for item in items:
        prod_id = item["product_id"]
        prev_stock = db_products[prod_id]["stock"]
        supabase_admin.table("products").update(
            {"stock": prev_stock - item["quantity"]}
        ).eq("id", prod_id).execute()


def restore_stock(order_id: str):
    """Restore product stock when an order is cancelled."""
    items_res = supabase_admin.table("order_items").select("product_id, quantity").eq("order_id", order_id).execute()
    for oi in (items_res.data or []):
        product_id = oi.get("product_id")
        quantity = oi.get("quantity")
        if not product_id or quantity is None:
            continue
        prod_res = supabase_admin.table("products").select("stock").eq("id", product_id).execute()
        if prod_res.data:
            restored = prod_res.data[0]["stock"] + quantity
            supabase_admin.table("products").update({"stock": restored}).eq("id", product_id).execute()


def get_delivery_fee(delivery_type: str = "standard") -> int:
    """Get delivery fee from platform config (in kobo)."""
    config_res = supabase.table("platform_config").select("*").eq("id", "platform-config").execute()
    config = config_res.data[0] if config_res.data else {}
    return (
        config.get("delivery_express_fee", 150000)
        if delivery_type == "express"
        else config.get("delivery_base_fee", 85000)
    )
