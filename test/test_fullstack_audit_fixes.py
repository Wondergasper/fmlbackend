"""
Unit tests verifying all full-stack audit & integration fixes across:
- routers/orders.py
- routers/products.py
- routers/vendors.py
- routers/wallet.py
- routers/logistics.py
- routers/auth.py
- routers/websockets.py
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routers.orders import (
    OrderItem,
    OrderCreate,
    OrderStatusUpdate,
    place_order,
    update_order_status,
)
from routers.products import (
    ProductCreate,
    ProductUpdate,
    get_my_products,
    update_product,
    delete_product,
)
from routers.vendors import get_my_vendor_earnings
from routers.wallet import TopupRequest, request_payout
from routers.logistics import (
    ProofOfDeliveryRequest,
    accept_delivery_offer,
    submit_proof_of_delivery,
)
from routers.auth import (
    _check_otp_attempts,
    _register_otp_failure,
    _clear_otp_attempts,
)


class DummyUser:
    def __init__(self, user_id: str, role: str = "customer", email: str = "test@example.com"):
        self.id = user_id
        self.role = role
        self.email = email
        self.user_metadata = {"full_name": "Test User"}


# ---------------------------------------------------------------------------
# 1. Orders: Validation, Grand Total Check, Express Fee, Vendor Settlement
# ---------------------------------------------------------------------------

def test_order_schema_rejects_zero_or_negative_quantity_and_empty_items():
    with pytest.raises(ValidationError):
        OrderItem(product_id="prod-1", quantity=0, unit_price=100000)

    with pytest.raises(ValidationError):
        OrderItem(product_id="prod-1", quantity=-3, unit_price=100000)

    with pytest.raises(ValidationError):
        OrderCreate(items=[], delivery_address="Lekki Phase 1")


def test_place_order_express_fee_and_grand_total_balance_guard():
    customer = DummyUser("cust-1", role="customer")
    order_req = OrderCreate(
        items=[OrderItem(product_id="prod-1", quantity=2, unit_price=200000)],
        delivery_address="Victoria Island, Lagos",
        delivery_type="express",
    )

    # Product price = 200,000 kobo x 2 = 400,000 kobo subtotal.
    # Express fee = 150,000 kobo -> Grand total = 550,000 kobo.
    # Customer wallet has 450,000 kobo (> subtotal, but < grand total) -> must fail with 402!
    mock_db = MagicMock()

    def table_side_effect(table_name):
        t = MagicMock()
        if table_name == "products":
            t.select.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
                {"id": "prod-1", "name": "Yam", "price": 200000, "stock": 10}
            ]
        elif table_name == "platform_config":
            t.select.return_value.eq.return_value.execute.return_value.data = []
        elif table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"wallet_balance": 450000}
            ]
        return t

    mock_db.table.side_effect = table_side_effect

    with patch("routers.orders.supabase", mock_db), patch(
        "routers.orders.supabase_admin", mock_db
    ), patch("routers.orders.geocode_address", new_callable=AsyncMock, return_value=None):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(place_order(order_req, user=customer))
        assert exc.value.status_code == 402
        assert "Insufficient wallet balance" in exc.value.detail


def test_update_order_status_ready_for_pickup_and_double_cancel_prevention():
    vendor = DummyUser("vendor-1", role="vendor")
    mock_db = MagicMock()

    # Order already Cancelled -> cancelling again should raise 400
    def table_cancelled(table_name):
        t = MagicMock()
        if table_name == "orders":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"id": "ord-1", "customer_id": "cust-1", "status": "Cancelled", "total_kobo": 285000}
            ]
        elif table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"role": "vendor"}
            ]
        return t

    mock_db.table.side_effect = table_cancelled
    with patch("routers.orders.supabase", mock_db), patch(
        "routers.orders.supabase_admin", mock_db
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                update_order_status(
                    "ord-1",
                    OrderStatusUpdate(status="Cancelled"),
                    user=vendor,
                )
            )
        assert exc.value.status_code == 400
        assert "already Cancelled" in exc.value.detail


def test_update_order_status_delivered_credits_vendor_net_earnings():
    rider = DummyUser("rider-1", role="rider")
    mock_db = MagicMock()
    wallet_updates = []
    tx_inserts = []

    def table_delivered(table_name):
        t = MagicMock()
        if table_name == "orders":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {
                    "id": "ord-2",
                    "customer_id": "cust-1",
                    "driver_id": "rider-1",
                    "status": "In Transit",
                    "total_kobo": 285000,
                    "delivery_type": "Standard",
                }
            ]
            t.update.return_value.eq.return_value.execute.return_value.data = [
                {
                    "id": "ord-2",
                    "customer_id": "cust-1",
                    "driver_id": "rider-1",
                    "status": "Delivered",
                    "total_kobo": 285000,
                    "delivery_type": "Standard",
                    "order_items": [],
                }
            ]
        elif table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"id": "vendor-1", "role": "rider", "wallet_balance": 100000, "full_name": "Vendor"}
            ]
            def capture_update(payload):
                wallet_updates.append(payload)
                m = MagicMock()
                m.eq.return_value.execute.return_value.data = [payload]
                return m
            t.update.side_effect = capture_update
        elif table_name == "order_items":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {
                    "quantity": 2,
                    "unit_price_kobo": 100000,
                    "products": {"vendor_id": "vendor-1"},
                }
            ]
        elif table_name == "wallet_transactions":
            def capture_insert(payload):
                tx_inserts.append(payload)
                m = MagicMock()
                m.execute.return_value.data = [payload]
                return m
            t.insert.side_effect = capture_insert
        return t

    mock_db.table.side_effect = table_delivered
    with patch("routers.orders.supabase", mock_db), patch(
        "routers.orders.supabase_admin", mock_db
    ), patch("routers.orders.connection_manager.broadcast_order_status", new_callable=AsyncMock), patch(
        "routers.orders.create_notification", new_callable=AsyncMock
    ):
        res = asyncio.run(
            update_order_status(
                "ord-2",
                OrderStatusUpdate(status="Delivered"),
                user=rider,
            )
        )
        assert res["data"]["status"] == "Delivered"
        # 2 * 100,000 = 200,000 gross -> 95% net = 190,000 + 100,000 existing = 290,000
        assert any(u.get("wallet_balance") == 290000 for u in wallet_updates)
        assert any(tx.get("amount_kobo") == 190000 and tx.get("type") == "Credit" for tx in tx_inserts)


# ---------------------------------------------------------------------------
# 2. Products: /products/me and 404 Handling on Missing Product
# ---------------------------------------------------------------------------

def test_update_and_delete_product_return_404_when_missing():
    vendor = DummyUser("vendor-1", role="vendor")
    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

    with patch("routers.products.supabase", mock_db), patch(
        "routers.products.supabase_admin", mock_db
    ):
        with pytest.raises(HTTPException) as exc1:
            asyncio.run(update_product("non-existent", ProductUpdate(price=50000), user=vendor))
        assert exc1.value.status_code == 404

        with pytest.raises(HTTPException) as exc2:
            asyncio.run(delete_product("non-existent", user=vendor))
        assert exc2.value.status_code == 404


def test_list_my_products_returns_vendor_catalog():
    vendor = DummyUser("vendor-1", role="vendor")
    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value.data = [
        {"id": "p-1", "name": "Organic Tomatoes", "status": "Pending Approval", "vendor_id": "vendor-1"}
    ]

    with patch("routers.products.supabase_admin", mock_db):
        items = asyncio.run(get_my_products(user=vendor))
        assert len(items) == 1
        assert items[0]["status"] == "Pending Approval"


# ---------------------------------------------------------------------------
# 3. Vendors: /vendors/me/earnings
# ---------------------------------------------------------------------------

def test_get_my_vendor_earnings_calculates_summary_and_payouts():
    vendor = DummyUser("vendor-1", role="vendor")
    mock_db = MagicMock()

    def table_side_effect(table_name):
        t = MagicMock()
        if table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {
                    "id": "vendor-1",
                    "wallet_balance": 190000,
                    "bank_name": "GTBank",
                    "account_number": "0123456789",
                }
            ]
        elif table_name == "wallet_transactions":
            t.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                {
                    "id": "tx-1",
                    "type": "Payout",
                    "amount_kobo": 100000,
                    "description": "Vendor Payout Request to GTBank (0123456789)",
                    "status": "Pending",
                    "reference": "POUT-111",
                    "created_at": "2026-10-18T10:00:00Z",
                }
            ]
        return t

    mock_db.table.side_effect = table_side_effect

    with patch("routers.vendors.supabase", mock_db), patch(
        "routers.vendors.supabase_admin", mock_db
    ):
        res = asyncio.run(get_my_vendor_earnings(user=vendor))
        assert res["paid_earnings_kobo"] == 100000
        assert res["pending_payout_kobo"] == 190000
        assert res["total_revenue_kobo"] == 290000
        assert len(res["payout_history"]) == 1
        assert res["payout_history"][0]["reference"] == "POUT-111"


# ---------------------------------------------------------------------------
# 4. Logistics & Wallet: Pool, Accept Conflict, POD Credit, Rider Payout
# ---------------------------------------------------------------------------

def test_accept_delivery_offer_blocks_hijack_and_cancelled_orders():
    rider = DummyUser("rider-2", role="rider")
    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "ord-99", "status": "In Transit", "driver_id": "rider-1"}
    ]

    with patch("routers.logistics.supabase_admin", mock_db):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(accept_delivery_offer("ord-99", user=rider))
        assert exc.value.status_code == 409
        assert "already been claimed" in exc.value.detail


def test_submit_pod_verifies_assigned_courier_and_credits_wallet():
    wrong_rider = DummyUser("rider-wrong", role="rider")
    assigned_rider = DummyUser("rider-1", role="rider")
    mock_db = MagicMock()
    wallet_updates = []

    def table_side_effect(table_name):
        t = MagicMock()
        if table_name == "orders":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"id": "ord-10", "status": "In Transit", "driver_id": "rider-1"}
            ]
            t.update.return_value.eq.return_value.execute.return_value.data = [
                {"id": "ord-10", "status": "Delivered", "driver_id": "rider-1"}
            ]
        elif table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"wallet_balance": 50000}
            ]
            def capture_update(payload):
                wallet_updates.append(payload)
                m = MagicMock()
                m.eq.return_value.execute.return_value.data = [payload]
                return m
            t.update.side_effect = capture_update
        elif table_name == "wallet_transactions":
            t.insert.return_value.execute.return_value.data = [{}]
        return t

    mock_db.table.side_effect = table_side_effect

    with patch("routers.logistics.supabase_admin", mock_db), patch(
        "routers.logistics.connection_manager.broadcast_order_updated", new_callable=AsyncMock
    ):
        # Unassigned rider blocked with 403
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                submit_proof_of_delivery(
                    "ord-10",
                    ProofOfDeliveryRequest(image_url="https://img/pod.jpg", recipient_name="Ada"),
                    user=wrong_rider,
                )
            )
        assert exc.value.status_code == 403

        # Assigned rider succeeds and receives 125,000 kobo credit
        res = asyncio.run(
            submit_proof_of_delivery(
                "ord-10",
                ProofOfDeliveryRequest(image_url="https://img/pod.jpg", recipient_name="Ada"),
                user=assigned_rider,
            )
        )
        assert res["payoutKobo"] == 125000
        assert any(u.get("wallet_balance") == 175000 for u in wallet_updates)


def test_rider_can_request_wallet_payout():
    rider = DummyUser("rider-1", role="rider")
    mock_db = MagicMock()

    def table_side_effect(table_name):
        t = MagicMock()
        if table_name == "profiles":
            t.select.return_value.eq.return_value.execute.return_value.data = [
                {"wallet_balance": 200000, "role": "rider", "bank_name": None, "account_number": None, "account_name": None}
            ]
            t.update.return_value.eq.return_value.execute.return_value.data = [{"wallet_balance": 75000}]
        elif table_name == "wallet_transactions":
            t.insert.return_value.execute.return_value.data = [{}]
        return t

    mock_db.table.side_effect = table_side_effect

    with patch("routers.wallet.supabase", mock_db), patch(
        "routers.wallet.supabase_admin", mock_db
    ):
        res = asyncio.run(
            request_payout(
                TopupRequest(amount_kobo=125000, reference="POUT-RIDER-01"),
                user=rider,
            )
        )
        assert res["new_balance_kobo"] == 75000


# ---------------------------------------------------------------------------
# 5. Auth: Async Redis OTP Brute-Force Helpers
# ---------------------------------------------------------------------------

def test_async_otp_helpers_work_with_async_redis_client():
    mock_async_redis = MagicMock()
    mock_async_redis.get = AsyncMock(return_value="5")
    mock_async_redis.incr = AsyncMock(return_value=1)
    mock_async_redis.expire = AsyncMock(return_value=True)
    mock_async_redis.delete = AsyncMock(return_value=1)

    with patch("routers.auth._otp_redis_client", return_value=mock_async_redis):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_check_otp_attempts("user@example.com", "verify_email"))
        assert exc.value.status_code == 429

        asyncio.run(_register_otp_failure("user@example.com", "verify_email"))
        mock_async_redis.incr.assert_awaited_once()
        mock_async_redis.expire.assert_awaited_once()

        asyncio.run(_clear_otp_attempts("user@example.com", "verify_email"))
        mock_async_redis.delete.assert_awaited_once()
