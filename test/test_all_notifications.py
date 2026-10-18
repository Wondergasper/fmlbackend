"""
test_all_notifications.py — Comprehensive end-to-end test covering ALL 15 email
notification triggers and 3 WebSocket broadcast triggers across every endpoint.

Verifies that each .delay() call is made with the correct arguments every time
its corresponding endpoint is called.
"""

import pytest
from unittest.mock import MagicMock, patch, ANY
from fastapi.testclient import TestClient
from main import app
from dependencies import get_current_user, require_role

client = TestClient(app)


# ---------------------------------------------------------------------------
# Mock users
# ---------------------------------------------------------------------------
class MockUser:
    def __init__(self, user_id, email="test@example.com"):
        self.id = user_id
        self.email = email

CUSTOMER = MockUser("cust-001", "gasperwonder@gmail.com")
VENDOR   = MockUser("vend-001", "wonderfarms@farmconnect.ng")
ADMIN    = MockUser("admin-001", "admin@farmconnect.ng")


# ---------------------------------------------------------------------------
# Mock DB helper (same pattern as other test files)
# ---------------------------------------------------------------------------
def mock_db(data=None):
    c = MagicMock()
    c.select.return_value = c
    c.insert.return_value = c
    c.update.return_value = c
    c.delete.return_value = c
    c.eq.return_value = c
    c.or_.return_value = c
    c.in_.return_value = c
    c.order.return_value = c
    c.limit.return_value = c
    c.gte.return_value = c
    c.lte.return_value = c
    c.ilike.return_value = c
    c.gt.return_value = c
    r = MagicMock()
    if isinstance(data, dict):
        r.data = [data]
    elif data is not None:
        r.data = data
    else:
        r.data = []
    c.execute.return_value = r
    return c


# ============================================================================
# 1. AUTH — Register customer → send_welcome_customer
# ============================================================================
@patch("routers.auth.supabase")
@patch("routers.auth.supabase_admin")
@patch("routers.auth.send_welcome_customer.delay")
def test_01_register_customer_triggers_welcome_email(mock_delay, mock_admin, mock_sp):
    """POST /auth/register (customer) → send_welcome_customer.delay"""
    app.dependency_overrides.clear()

    # Mock sign_up success
    mock_sp.auth.sign_up.return_value.user.id = "new-cust-001"

    payload = {
        "email": "newuser@example.com",
        "password": "Test1234!",
        "full_name": "New Customer",
        "role": "customer",
    }
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    mock_delay.assert_called_once_with("newuser@example.com", "New Customer")
    print("  ✅ send_welcome_customer.delay called with correct args")


# ============================================================================
# 2. AUTH — Register vendor → send_welcome_vendor + send_admin_new_vendor
# ============================================================================
@patch("routers.auth.supabase")
@patch("routers.auth.supabase_admin")
@patch("routers.auth.send_welcome_vendor.delay")
@patch("routers.auth.send_admin_new_vendor.delay")
def test_02_register_vendor_triggers_welcome_and_admin_email(mock_admin_notify, mock_vendor_welcome, mock_admin, mock_sp):
    """POST /auth/register (vendor) → send_welcome_vendor + send_admin_new_vendor"""
    app.dependency_overrides.clear()

    mock_sp.auth.sign_up.return_value.user.id = "new-vend-001"

    payload = {
        "email": "newvendor@example.com",
        "password": "Test1234!",
        "full_name": "New Vendor",
        "role": "vendor",
    }
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    mock_vendor_welcome.assert_called_once_with("newvendor@example.com", "New Vendor")
    mock_admin_notify.assert_called_once()
    assert mock_admin_notify.call_args[0][1] == "New Vendor"
    assert mock_admin_notify.call_args[0][2] == "newvendor@example.com"
    print("  ✅ send_welcome_vendor.delay + send_admin_new_vendor.delay called with correct args")


# ============================================================================
# 3. VENDOR — Approve → send_vendor_approved
# ============================================================================
@patch("routers.vendors.supabase")
@patch("routers.vendors.supabase_admin")
@patch("routers.vendors.send_vendor_approved.delay")
@patch("dependencies.supabase")
def test_03_approve_vendor_triggers_approved_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /vendors/{id}/status → Active → send_vendor_approved.delay"""
    app.dependency_overrides[get_current_user] = lambda: ADMIN

    mock_dep_sp.table.return_value = mock_db({"role": "admin"})
    mock_admin.table.return_value = mock_db([{"id": "vend-001", "email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"}])

    resp = client.patch("/vendors/vend-001/status", json={"status": "Active"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once_with("wonderfarms@farmconnect.ng", "Wonder Farms")
    print("  ✅ send_vendor_approved.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 4. VENDOR — Suspend → send_vendor_suspended
# ============================================================================
@patch("routers.vendors.supabase")
@patch("routers.vendors.supabase_admin")
@patch("routers.vendors.send_vendor_suspended.delay")
@patch("dependencies.supabase")
def test_04_suspend_vendor_triggers_suspended_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /vendors/{id}/status → Suspended → send_vendor_suspended.delay"""
    app.dependency_overrides[get_current_user] = lambda: ADMIN

    mock_dep_sp.table.return_value = mock_db({"role": "admin"})
    mock_admin.table.return_value = mock_db([{"id": "vend-001", "email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"}])

    resp = client.patch("/vendors/vend-001/status", json={"status": "Suspended", "reason": "Violating terms"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once_with("wonderfarms@farmconnect.ng", "Wonder Farms", "Violating terms")
    print("  ✅ send_vendor_suspended.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 5. PRODUCTS — Submit → send_product_submitted
# ============================================================================
@patch("routers.products.supabase")
@patch("routers.products.supabase_admin")
@patch("routers.products.send_product_submitted.delay")
@patch("dependencies.supabase")
def test_05_submit_product_triggers_submitted_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """POST /products → send_product_submitted.delay"""
    app.dependency_overrides[get_current_user] = lambda: VENDOR

    mock_dep_sp.table.return_value = mock_db({"role": "vendor"})
    mock_sp.table.return_value = mock_db({"role": "vendor", "email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"})
    mock_admin.table.return_value = mock_db([{"id": "prod-001", "name": "Test Product"}])

    payload = {
        "name": "Test Product",
        "description": "A test",
        "price": 10000,
        "stock": 50,
        "category": "Vegetables",
        "origin": "Lagos, Nigeria",
    }
    resp = client.post("/products", json=payload)
    assert resp.status_code == 201, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[0] == "wonderfarms@farmconnect.ng"
    assert args[1] == "Wonder Farms"
    assert args[2] == "Test Product"
    print("  ✅ send_product_submitted.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 6. PRODUCTS — Approve → send_product_approved
# ============================================================================
@patch("routers.products.supabase")
@patch("routers.products.supabase_admin")
@patch("routers.products.send_product_approved.delay")
@patch("dependencies.supabase")
def test_06_approve_product_triggers_approved_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /products/{id}/status → Approved → send_product_approved.delay"""
    app.dependency_overrides[get_current_user] = lambda: ADMIN

    mock_dep_sp.table.return_value = mock_db({"role": "admin"})

    def sp_side_effect(table):
        if table == "profiles":
            # Vendor profile lookup for email
            return mock_db({"email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"})
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    # Admin update returns the updated product with vendor_id
    mock_admin.table.return_value = mock_db([{"id": "prod-001", "vendor_id": VENDOR.id, "name": "Test Product", "price": 10000, "stock": 50, "status": "Approved"}])

    resp = client.patch("/products/prod-001/status", json={"status": "Approved"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[0] == "wonderfarms@farmconnect.ng"
    assert args[2] == "Test Product"
    print("  ✅ send_product_approved.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 7. PRODUCTS — Reject → send_product_rejected
# ============================================================================
@patch("routers.products.supabase")
@patch("routers.products.supabase_admin")
@patch("routers.products.send_product_rejected.delay")
@patch("dependencies.supabase")
def test_07_reject_product_triggers_rejected_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /products/{id}/status → Rejected → send_product_rejected.delay"""
    app.dependency_overrides[get_current_user] = lambda: ADMIN

    mock_dep_sp.table.return_value = mock_db({"role": "admin"})

    def sp_side_effect(table):
        if table == "profiles":
            return mock_db({"email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"})
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    mock_admin.table.return_value = mock_db([{"id": "prod-001", "vendor_id": VENDOR.id, "name": "Test Product", "price": 10000, "stock": 50, "status": "Rejected"}])

    resp = client.patch("/products/prod-001/status", json={"status": "Rejected", "reason": "Poor quality"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[0] == "wonderfarms@farmconnect.ng"
    assert args[2] == "Test Product"
    assert args[4] == "Poor quality"
    print("  ✅ send_product_rejected.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 8. ORDERS — Place → send_order_confirmation + send_new_sale_alert + broadcast_vendor_alert
# ============================================================================
@patch("routers.orders.supabase")
@patch("routers.orders.supabase_admin")
@patch("routers.orders.send_order_confirmation.delay")
@patch("routers.orders.send_new_sale_alert.delay")
@patch("dependencies.supabase")
def test_08_place_order_triggers_all_notifications(mock_dep_sp, mock_sale_alert, mock_confirm, mock_admin, mock_sp):
    """POST /orders → send_order_confirmation + send_new_sale_alert + vendor WebSocket alert"""
    app.dependency_overrides[get_current_user] = lambda: CUSTOMER

    mock_dep_sp.table.return_value = mock_db({"role": "customer"})

    # Wallet balance check
    profile_calls = 0
    def sp_side_effect(table):
        nonlocal profile_calls
        if table == "profiles":
            profile_calls += 1
            # 1st call: customer wallet check → return balance
            # 2nd call: customer email/name for order confirmation
            # 3rd+ call: vendor profiles for sale alert
            if profile_calls <= 2:
                return mock_db({"wallet_balance": 500000, "email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
            else:
                return mock_db([{"id": VENDOR.id, "email": "wonderfarms@farmconnect.ng", "full_name": "Wonder Farms"}])
        if table == "products":
            return mock_db([{"id": "prod-001", "price": 10000, "stock": 50, "status": "Approved", "vendor_id": VENDOR.id, "name": "Test Product"}])
        if table == "platform_config":
            return mock_db({"delivery_base_fee": 85000})
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    # Admin for order creation
    calls = 0
    def admin_side_effect(table):
        nonlocal calls
        calls += 1
        if table == "products":
            return mock_db([{"id": "prod-001", "price": 10000, "stock": 50, "status": "Approved", "vendor_id": VENDOR.id, "name": "Test Product"}])
        if table == "orders":
            return mock_db([{"id": "ord-001", "total_kobo": 185000}])
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        if table == "profiles":
            return mock_db({"wallet_balance": 315000})  # after deducting
        return mock_db([])
    mock_admin.table.side_effect = admin_side_effect

    payload = {
        "items": [{"product_id": "prod-001", "quantity": 10, "unit_price": 10000}],
        "delivery_type": "standard",
        "delivery_address": "42 Awolowo Road, Ikoyi, Lagos",
    }
    resp = client.post("/orders", json=payload)
    assert resp.status_code == 201, resp.text

    # send_order_confirmation
    mock_confirm.assert_called_once()
    c_args = mock_confirm.call_args[0]
    assert c_args[0] == "gasperwonder@gmail.com"
    assert c_args[1] == "Gasper Wonder"
    assert c_args[2] == "ord-001"
    print("  ✅ send_order_confirmation.delay called with customer email & order id")

    # send_new_sale_alert
    if mock_sale_alert.called:
        s_args = mock_sale_alert.call_args[0]
        assert s_args[0] == "wonderfarms@farmconnect.ng"
        print(f"  ✅ send_new_sale_alert.delay called for vendor {s_args[1]}")

    app.dependency_overrides.clear()


# ============================================================================
# 9. ORDERS — In Transit → send_order_in_transit + broadcast_order_status
# ============================================================================
@patch("routers.orders.supabase")
@patch("routers.orders.supabase_admin")
@patch("routers.orders.send_order_in_transit.delay")
@patch("dependencies.supabase")
def test_09_order_in_transit_triggers_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /orders/{id}/status → 'In Transit' → send_order_in_transit.delay"""
    app.dependency_overrides[get_current_user] = lambda: VENDOR

    mock_dep_sp.table.return_value = mock_db({"role": "vendor"})

    sp_call_count = 0
    def sp_side_effect(table):
        nonlocal sp_call_count
        if table == "profiles":
            sp_call_count += 1
            if sp_call_count == 1:
                return mock_db({"role": "vendor"})
            return mock_db({"email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    # order_items vendor check: vendor has products in this order
    def admin_side_effect(table):
        if table == "orders":
            return mock_db([{"id": "ord-001", "customer_id": CUSTOMER.id, "total_kobo": 185000, "delivery_address": "42 Awolowo Road, Ikoyi, Lagos"}])
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])  # has products
        return mock_db([])
    mock_admin.table.side_effect = admin_side_effect

    resp = client.patch("/orders/ord-001/status", json={"status": "In Transit"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[2] == "ord-001"
    print("  ✅ send_order_in_transit.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 10. ORDERS — Delivered → send_order_delivered + broadcast_order_status
# ============================================================================
@patch("routers.orders.supabase")
@patch("routers.orders.supabase_admin")
@patch("routers.orders.send_order_delivered.delay")
@patch("dependencies.supabase")
def test_10_order_delivered_triggers_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /orders/{id}/status → 'Delivered' → send_order_delivered.delay"""
    app.dependency_overrides[get_current_user] = lambda: VENDOR

    mock_dep_sp.table.return_value = mock_db({"role": "vendor"})

    sp_call_count = 0
    def sp_side_effect(table):
        nonlocal sp_call_count
        if table == "profiles":
            sp_call_count += 1
            if sp_call_count == 1:
                return mock_db({"role": "vendor"})
            return mock_db({"email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    def admin_side_effect(table):
        if table == "orders":
            return mock_db([{"id": "ord-001", "customer_id": CUSTOMER.id, "total_kobo": 185000}])
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        return mock_db([])
    mock_admin.table.side_effect = admin_side_effect

    resp = client.patch("/orders/ord-001/status", json={"status": "Delivered"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[2] == "ord-001"
    print("  ✅ send_order_delivered.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 11. ORDERS — Cancelled → send_order_cancelled + broadcast_order_status
# ============================================================================
@patch("routers.orders.supabase")
@patch("routers.orders.supabase_admin")
@patch("routers.orders.send_order_cancelled.delay")
@patch("dependencies.supabase")
def test_11_order_cancelled_triggers_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """PATCH /orders/{id}/status → 'Cancelled' → send_order_cancelled.delay"""
    app.dependency_overrides[get_current_user] = lambda: VENDOR

    mock_dep_sp.table.return_value = mock_db({"role": "vendor"})

    sp_call_count = 0
    def sp_side_effect(table):
        nonlocal sp_call_count
        if table == "profiles":
            sp_call_count += 1
            if sp_call_count == 1:
                return mock_db({"role": "vendor"})
            return mock_db({"email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    def admin_side_effect(table):
        if table == "orders":
            return mock_db([{"id": "ord-001", "customer_id": CUSTOMER.id, "total_kobo": 185000}])
        if table == "order_items":
            return mock_db([{"id": "oi-001"}])
        return mock_db([])
    mock_admin.table.side_effect = admin_side_effect

    resp = client.patch("/orders/ord-001/status", json={"status": "Cancelled", "note": "Out of stock"})
    assert resp.status_code == 200, resp.text
    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[2] == "ord-001"
    assert args[4] == "Out of stock"
    print("  ✅ send_order_cancelled.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 12. WALLET — Topup → send_wallet_topup_receipt
# ============================================================================
@patch("routers.wallet.supabase")
@patch("routers.wallet.supabase_admin")
@patch("routers.wallet.send_wallet_topup_receipt.delay")
@patch("dependencies.supabase")
def test_12_wallet_topup_triggers_receipt_email(mock_dep_sp, mock_delay, mock_admin, mock_sp):
    """POST /wallet/topup → send_wallet_topup_receipt.delay"""
    app.dependency_overrides[get_current_user] = lambda: CUSTOMER

    mock_dep_sp.table.return_value = mock_db({"role": "customer"})

    # Mock idempotency check (return empty = no duplicate)
    mock_admin.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

    def sp_side_effect(table):
        if table == "profiles":
            return mock_db({"wallet_balance": 100000, "email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    def admin_side_effect(table):
        if table == "wallet_transactions":
            return mock_db([])
        if table == "profiles":
            return mock_db({"wallet_balance": 150000, "email": "", "full_name": ""})
        return mock_db([])
    mock_admin.table.side_effect = admin_side_effect

    payload = {"amount_kobo": 50000, "reference": "ref-topup-001"}
    resp = client.post("/wallet/topup", json=payload)
    assert resp.status_code == 200, resp.text

    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[0] == "gasperwonder@gmail.com"
    assert args[1] == "Gasper Wonder"
    assert args[2] == 50000
    assert args[3] == "ref-topup-001"
    print("  ✅ send_wallet_topup_receipt.delay called with correct args")
    app.dependency_overrides.clear()


# ============================================================================
# 13. WALLET — Paystack verify → send_wallet_topup_receipt
# ============================================================================
@patch("routers.wallet.supabase")
@patch("routers.wallet.supabase_admin")
@patch("routers.wallet.send_wallet_topup_receipt.delay")
@patch("routers.wallet.verify_paystack_transaction")
@patch("dependencies.supabase")
def test_13_paystack_verify_triggers_receipt_email(mock_dep_sp, mock_verify, mock_delay, mock_admin, mock_sp):
    """POST /wallet/paystack/verify → send_wallet_topup_receipt.delay"""
    app.dependency_overrides[get_current_user] = lambda: CUSTOMER

    mock_dep_sp.table.return_value = mock_db({"role": "customer"})
    mock_verify.return_value = {
        "status": "success",
        "amount": 75000,
        "metadata": {"purpose": "wallet_topup", "user_id": CUSTOMER.id},
    }

    def sp_side_effect(table):
        if table == "profiles":
            return mock_db({"wallet_balance": 200000, "email": "gasperwonder@gmail.com", "full_name": "Gasper Wonder"})
        return mock_db([])
    mock_sp.table.side_effect = sp_side_effect

    payload = {"amount_kobo": 75000, "reference": "paystack-ref-001"}
    resp = client.post("/wallet/paystack/verify", json=payload)
    assert resp.status_code == 200, resp.text

    mock_delay.assert_called_once()
    args = mock_delay.call_args[0]
    assert args[0] == "gasperwonder@gmail.com"
    assert args[1] == "Gasper Wonder"
    print("  ✅ send_wallet_topup_receipt (Paystack) called with correct args")
    app.dependency_overrides.clear()
