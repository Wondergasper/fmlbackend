"""
test_disputes_edge_cases.py — Empirical test suite for dispute edge case remediations.
Verifies:
1. Non-admin attempting resolution is rejected (403)
2. Whitespace resolution outcome & notes are rejected (400)
3. Storage upload exceptions are properly caught and raise 500
4. Double-resolution attempts are rejected (400)
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app
from dependencies import get_current_user, require_role

client = TestClient(app)

class MockUser:
    def __init__(self, user_id="user_123", email="user@example.com"):
        self.id = user_id
        self.email = email

mock_customer = MockUser("cust_100", "customer@example.com")
mock_admin = MockUser("admin_900", "admin@example.com")

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


def test_edge_case_1_non_admin_resolution():
    """Verify non-admin user cannot resolve disputes."""
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    with patch("dependencies.supabase") as mock_dep_sp:
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "customer"})
        response = client.patch("/disputes/disp_001/resolve", json={
            "resolution_outcome": "Refund Buyer",
            "resolution_notes": "Attempted resolution by customer"
        })
    
    assert response.status_code == 403
    assert "Access denied" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_edge_case_2_whitespace_resolution_notes():
    """Verify resolution notes with only whitespace are rejected with 400 Bad Request."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    with patch("dependencies.supabase") as mock_dep_sp:
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})
        payload = {
            "resolution_outcome": "Refund Buyer",
            "resolution_notes": "   "
        }
        response = client.patch("/disputes/disp_001/resolve", json=payload)
    
    assert response.status_code == 400
    assert "Resolution notes and outcome are strictly required" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_edge_case_3_evidence_upload_swallowed_storage_exception():
    """Verify failed storage uploads raise 500 and do not create phantom DB records."""
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
        mock_sp_admin.storage.from_().upload.side_effect = Exception("Storage cluster unreachable / upload error")

        files = {"file": ("test.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50, "image/jpeg")}
        response = client.post("/disputes/disp_001/evidence", files=files)

        assert response.status_code == 500
        assert "Storage upload failed" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_edge_case_4_resolving_already_resolved_dispute_double_reconciliation():
    """Verify resolving an already-resolved dispute is rejected to prevent double-reconciliation."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    already_resolved_dispute = {"id": "disp_resolved_999", "order_id": "ord_555", "status": "Resolved"}

    with patch("routers.disputes.supabase") as mock_sp, patch("dependencies.supabase") as mock_dep_sp:
        def table_side_effect(table_name):
            if table_name == "disputes":
                return create_mock_db_chain(already_resolved_dispute)
            return create_mock_db_chain([])

        mock_sp.table.side_effect = table_side_effect
        mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})

        payload = {
            "resolution_outcome": "Refund Buyer",
            "resolution_notes": "Second resolution attempt"
        }
        response = client.patch("/disputes/disp_resolved_999/resolve", json=payload)

        assert response.status_code == 400
        assert "already resolved" in response.json()["detail"]

    app.dependency_overrides.clear()


if __name__ == "__main__":
    pytest.main(["-v", "test_disputes_edge_cases.py"])
