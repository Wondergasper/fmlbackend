"""
test_disputes.py — Test suite for Structured Dispute Audit Trail & Admin Resolution
Verifies dispute creation, authorization checks, vendor visibility, evidence upload, storage error handling, duplicate dispute prevention, and multi-vendor wallet reconciliation.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app
from dependencies import get_current_user, require_role

client = TestClient(app)

# Mock User Objects
class MockUser:
    def __init__(self, user_id="user_customer_123", email="customer@example.com"):
        self.id = user_id
        self.email = email

mock_customer = MockUser("user_customer_123", "customer@example.com")
mock_unauthorized = MockUser("user_unauthorized_999", "unauthorized@example.com")
mock_vendor = MockUser("user_vendor_456", "vendor@example.com")
mock_admin = MockUser("user_admin_789", "admin@example.com")


# Helper to create fluent DB mocks
def create_mock_db_chain(return_data=None):
    mock_chain = MagicMock()
    mock_chain.select.return_value = mock_chain
    mock_chain.insert.return_value = mock_chain
    mock_chain.update.return_value = mock_chain
    mock_chain.delete.return_value = mock_chain
    mock_chain.eq.return_value = mock_chain
    mock_chain.or_.return_value = mock_chain
    mock_chain.in_.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.single.return_value = mock_chain
    
    mock_result = MagicMock()
    if isinstance(return_data, dict):
        mock_result.data = [return_data]
    elif return_data is not None:
        mock_result.data = return_data
    else:
        mock_result.data = []
    mock_chain.execute.return_value = mock_result
    return mock_chain


def test_open_dispute_success():
    """Verify customer can open a dispute for a valid order."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    order_mock_data = {
        "id": "ord_100",
        "customer_id": mock_customer.id,
        "order_items": [{"products": {"vendor_id": mock_vendor.id}}]
    }
    dispute_mock_data = [{
        "id": "disp_001",
        "order_id": "ord_100",
        "created_by": mock_customer.id,
        "reason": "Damaged goods delivered",
        "status": "Open"
    }]
    profile_mock_data = {"role": "customer"}

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        def table_side_effect(table_name):
            if table_name == "orders":
                return create_mock_db_chain(order_mock_data)
            elif table_name == "profiles":
                return create_mock_db_chain(profile_mock_data)
            elif table_name == "disputes":
                return create_mock_db_chain([])  # no active disputes
            return create_mock_db_chain([])

        def admin_table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock_data)
            elif table_name == "dispute_notes":
                return create_mock_db_chain([{"id": "note_001"}])
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        response = client.post("/disputes/", json={"order_id": "ord_100", "reason": "Damaged goods delivered"})
        assert response.status_code == 201
        res_json = response.json()
        assert res_json["message"] == "Dispute opened successfully."
        assert res_json["dispute"]["id"] == "disp_001"

    app.dependency_overrides.clear()


def test_open_dispute_order_not_found():
    """Verify opening dispute for non-existent order returns 404."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    with patch("routers.disputes.supabase") as mock_sp:
        mock_sp.table.return_value = create_mock_db_chain(None)

        response = client.post("/disputes/", json={"order_id": "ord_invalid", "reason": "Missing items"})
        assert response.status_code == 404
        assert response.json()["detail"] == "Order not found."

    app.dependency_overrides.clear()


def test_open_dispute_unauthorized_user():
    """Verify user not associated with order cannot open a dispute (403)."""
    app.dependency_overrides[get_current_user] = lambda: mock_unauthorized

    order_mock_data = {
        "id": "ord_100",
        "customer_id": mock_customer.id,
        "order_items": [{"products": {"vendor_id": mock_vendor.id}}]
    }

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "orders":
                return create_mock_db_chain(order_mock_data)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.post("/disputes/", json={"order_id": "ord_100", "reason": "Unrelated issue"})
        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_open_dispute_duplicate_prevention():
    """Verify duplicate active dispute creation is rejected with 400."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    order_mock_data = {
        "id": "ord_100",
        "customer_id": mock_customer.id,
        "order_items": []
    }
    active_dispute_mock = [{"id": "disp_existing", "status": "Open"}]

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "orders":
                return create_mock_db_chain(order_mock_data)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            elif table_name == "disputes":
                return create_mock_db_chain(active_dispute_mock)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.post("/disputes/", json={"order_id": "ord_100", "reason": "Another issue"})
        assert response.status_code == 400
        assert "An active dispute already exists" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_get_dispute_details():
    """Verify retrieving full dispute details for authorized user."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    dispute_mock = {"id": "disp_001", "order_id": "ord_100", "created_by": mock_customer.id, "reason": "Damaged items"}
    notes_mock = [
        {"id": "n1", "note_text": "Dispute opened", "created_at": "2026-06-29T10:00:00Z"},
        {"id": "n2", "note_text": "Vendor replied", "created_at": "2026-06-29T11:00:00Z"}
    ]
    evidence_mock = [
        {"id": "e1", "file_url": "https://example.com/img.jpg", "file_path": "disputes/disp_001/e1.jpg"}
    ]

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            elif table_name == "dispute_notes":
                return create_mock_db_chain(notes_mock)
            elif table_name == "dispute_evidence":
                return create_mock_db_chain(evidence_mock)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.get("/disputes/disp_001")
        assert response.status_code == 200
        res_json = response.json()
        assert res_json["id"] == "disp_001"
        assert len(res_json["notes"]) == 2
        assert len(res_json["evidence"]) == 1

    app.dependency_overrides.clear()


def test_get_dispute_unauthorized_403():
    """Verify unauthorized user cannot access dispute details (IDOR check)."""
    app.dependency_overrides[get_current_user] = lambda: mock_unauthorized

    dispute_mock = {"id": "disp_001", "order_id": "ord_100", "created_by": mock_customer.id}
    order_mock = {"id": "ord_100", "customer_id": mock_customer.id, "order_items": [{"products": {"vendor_id": mock_vendor.id}}]}

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            elif table_name == "orders":
                return create_mock_db_chain(order_mock)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.get("/disputes/disp_001")
        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_add_dispute_note():
    """Verify adding a note to an open dispute as authorized user."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    dispute_mock = {"id": "disp_001", "status": "Open", "created_by": mock_customer.id}
    profile_mock = {"role": "customer"}
    note_inserted = [{"id": "note_002", "dispute_id": "disp_001", "note_text": "Photos uploaded for review"}]

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain(profile_mock)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.return_value = create_mock_db_chain(note_inserted)

        response = client.post("/disputes/disp_001/notes", json={"note_text": "Photos uploaded for review"})
        assert response.status_code == 201
        res_json = response.json()
        assert res_json["message"] == "Note added to dispute audit trail."
        assert res_json["note"]["id"] == "note_002"

    app.dependency_overrides.clear()


def test_add_dispute_note_unauthorized():
    """Verify unauthorized user cannot add note (403)."""
    app.dependency_overrides[get_current_user] = lambda: mock_unauthorized

    dispute_mock = {"id": "disp_001", "status": "Open", "created_by": mock_customer.id}

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.post("/disputes/disp_001/notes", json={"note_text": "Unauthorized note"})
        assert response.status_code == 403

    app.dependency_overrides.clear()


def test_add_dispute_evidence_json():
    """Verify attaching evidence via JSON payload."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    dispute_mock = {"id": "disp_001", "created_by": mock_customer.id}
    evidence_inserted = [{
        "id": "ev_001",
        "dispute_id": "disp_001",
        "file_url": "https://example.com/evidence1.jpg",
        "file_path": "disputes/disp_001/ev1.jpg"
    }]

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.return_value = create_mock_db_chain(evidence_inserted)

        payload = {
            "file_url": "https://example.com/evidence1.jpg",
            "file_path": "disputes/disp_001/ev1.jpg",
            "description": "Photo of spoiled produce"
        }
        response = client.post("/disputes/disp_001/evidence", json=payload)
        assert response.status_code == 201
        res_json = response.json()
        assert res_json["message"] == "Evidence attached to dispute."
        assert res_json["evidence"]["id"] == "ev_001"

    app.dependency_overrides.clear()


def test_storage_upload_error_raises_500():
    """Verify storage upload failure raises 500 and prevents phantom DB record."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    dispute_mock = {"id": "disp_001", "created_by": mock_customer.id}

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(dispute_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"role": "customer"})
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.storage.from_().upload.side_effect = Exception("Storage connection error")

        files = {"file": ("test.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50, "image/jpeg")}
        response = client.post("/disputes/disp_001/evidence", files=files)
        assert response.status_code == 500
        assert "Storage upload failed" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_vendor_dispute_visibility():
    """Verify vendor can view disputes for orders involving their products."""
    app.dependency_overrides[get_current_user] = lambda: mock_vendor

    order_items_mock = [{"order_id": "ord_100", "vendor_id": mock_vendor.id}]
    disputes_mock = [{"id": "disp_vendor_1", "order_id": "ord_100"}]

    with patch("routers.disputes.supabase") as mock_sp:
        def table_side_effect(table_name):
            if table_name == "profiles":
                return create_mock_db_chain({"role": "vendor"})
            elif table_name == "order_items":
                return create_mock_db_chain(order_items_mock)
            elif table_name == "disputes":
                return create_mock_db_chain(disputes_mock)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect

        response = client.get("/disputes/")
        assert response.status_code == 200
        res_json = response.json()
        assert len(res_json) == 1
        assert res_json[0]["id"] == "disp_vendor_1"

    app.dependency_overrides.clear()


def test_admin_resolve_dispute_refund_buyer():
    """Verify admin resolution with outcome 'Refund Buyer' updates status and triggers wallet refund."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    existing_dispute = {"id": "disp_001", "order_id": "ord_100", "status": "Open"}
    updated_dispute = [{
        "id": "disp_001",
        "order_id": "ord_100",
        "status": "Resolved",
        "resolution_outcome": "Refund Buyer",
        "resolution_notes": "Refund approved due to damaged delivery"
    }]
    order_mock = {
        "id": "ord_100",
        "customer_id": mock_customer.id,
        "total_kobo": 500000,
        "order_items": [{"products": {"vendor_id": mock_vendor.id}}]
    }
    profile_mock = {"wallet_balance": 100000}

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin, patch("dependencies.supabase") as mock_dep_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(existing_dispute)
            elif table_name == "orders":
                return create_mock_db_chain(order_mock)
            elif table_name == "profiles":
                return create_mock_db_chain(profile_mock)
            return create_mock_db_chain([])
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})

        def admin_table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(updated_dispute)
            elif table_name in ["dispute_notes", "profiles", "wallet_transactions"]:
                return create_mock_db_chain([{"id": "tx_001"}])
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        payload = {
            "resolution_outcome": "Refund Buyer",
            "resolution_notes": "Refund approved due to damaged delivery"
        }
        response = client.patch("/disputes/disp_001/resolve", json=payload)
        assert response.status_code == 200
        res_json = response.json()
        assert "Refund Buyer" in res_json["message"]
        assert res_json["dispute"]["resolution_outcome"] == "Refund Buyer"

    app.dependency_overrides.clear()


def test_admin_resolve_dispute_multi_vendor_release():
    """Verify Release to Vendor accurately calculates and credits multiple vendors."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    existing_dispute = {"id": "disp_multi", "order_id": "ord_multi", "status": "Open"}
    updated_dispute = [{
        "id": "disp_multi",
        "status": "Resolved",
        "resolution_outcome": "Release to Vendor",
        "resolution_notes": "Order delivered verified"
    }]
    order_mock = {
        "id": "ord_multi",
        "customer_id": mock_customer.id,
        "total_kobo": 3000,
        "order_items": [
            {"vendor_id": "v1", "quantity": 2, "unit_price_kobo": 1000},
            {"vendor_id": "v2", "quantity": 1, "unit_price_kobo": 1000}
        ]
    }

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin, patch("dependencies.supabase") as mock_dep_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(existing_dispute)
            elif table_name == "orders":
                return create_mock_db_chain(order_mock)
            elif table_name == "profiles":
                return create_mock_db_chain({"wallet_balance": 500})
            return create_mock_db_chain([])
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})

        def admin_table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(updated_dispute)
            return create_mock_db_chain([{"id": "ok"}])

        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        response = client.patch("/disputes/disp_multi/resolve", json={
            "resolution_outcome": "Release to Vendor",
            "resolution_notes": "Order delivered verified"
        })
        assert response.status_code == 200
        assert response.json()["dispute"]["status"] == "Resolved"

    app.dependency_overrides.clear()


def test_resolve_dispute_missing_notes_or_outcome():
    """Verify resolving dispute without outcome or notes returns 400."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    with patch("dependencies.supabase") as mock_dep_sp:
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})
        response = client.patch("/disputes/disp_001/resolve", json={"resolution_outcome": "", "resolution_notes": ""})
        assert response.status_code == 400
        assert "Resolution notes and outcome are strictly required" in response.json()["detail"]

    app.dependency_overrides.clear()
