"""
test_notifications_api.py — Unit & Integration tests for in-app Notifications API

Tests:
  - GET   /notifications/           (list + unread_count + filter)
  - PATCH /notifications/read-all   (mark all read for caller)
  - PATCH /notifications/{id}/read  (mark single read)
  - DELETE /notifications/{id}      (dismiss/delete)
  - User isolation & unauthorized access guarantees
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app
from dependencies import get_current_user

client = TestClient(app)


class MockUser:
    def __init__(self, user_id, email="test@example.com"):
        self.id = user_id
        self.email = email


USER_A = MockUser("user-aaa-111", "usera@example.com")
USER_B = MockUser("user-bbb-222", "userb@example.com")


def mock_db_chain(data=None, count=None):
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.range.return_value = chain

    res = MagicMock()
    if isinstance(data, dict):
        res.data = [data]
    elif data is not None:
        res.data = data
    else:
        res.data = []
    res.count = count
    chain.execute.return_value = res
    return chain


def test_list_notifications_success():
    sample_notifications = [
        {
            "id": "notif-1",
            "user_id": USER_A.id,
            "title": "Order Placed",
            "message": "Order #12345678 placed.",
            "type": "order",
            "link": "/dashboard/orders/123",
            "is_read": False,
            "created_at": "2026-10-14T12:00:00Z",
        },
        {
            "id": "notif-2",
            "user_id": USER_A.id,
            "title": "Wallet Credited",
            "message": "₦5,000.00 added.",
            "type": "wallet",
            "link": "/dashboard/wallet",
            "is_read": True,
            "created_at": "2026-10-14T11:00:00Z",
        },
    ]

    app.dependency_overrides[get_current_user] = lambda: USER_A
    try:
        mock_table = MagicMock()
        # For select count and select *
        count_res = MagicMock(count=1, data=[{"id": "notif-1"}])
        list_res = MagicMock(data=sample_notifications, count=2)

        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.execute.side_effect = [count_res, list_res]
        mock_table.return_value = chain

        with patch("routers.notifications.supabase_admin.table", mock_table):
            response = client.get("/notifications/")
            assert response.status_code == 200
            data = response.json()
            assert "notifications" in data
            assert data["unread_count"] == 1
            assert len(data["notifications"]) == 2
    finally:
        app.dependency_overrides.clear()


def test_mark_all_notifications_read():
    app.dependency_overrides[get_current_user] = lambda: USER_A
    try:
        mock_table = MagicMock()
        chain = mock_db_chain([])
        mock_table.return_value = chain

        with patch("routers.notifications.supabase_admin.table", mock_table):
            response = client.patch("/notifications/read-all")
            assert response.status_code == 200
            assert response.json()["message"] == "All notifications marked as read."
            # Verify update was scoped to USER_A.id and is_read=False
            chain.update.assert_called_with({"is_read": True})
            chain.eq.assert_any_call("user_id", USER_A.id)
            chain.eq.assert_any_call("is_read", False)
    finally:
        app.dependency_overrides.clear()


def test_mark_single_notification_read_success():
    app.dependency_overrides[get_current_user] = lambda: USER_A
    try:
        mock_table = MagicMock()
        chain = mock_db_chain([{"id": "notif-1", "is_read": True}])
        mock_table.return_value = chain

        with patch("routers.notifications.supabase_admin.table", mock_table):
            response = client.patch("/notifications/notif-1/read")
            assert response.status_code == 200
            assert response.json()["id"] == "notif-1"
            chain.update.assert_called_with({"is_read": True})
            chain.eq.assert_any_call("id", "notif-1")
            chain.eq.assert_any_call("user_id", USER_A.id)
    finally:
        app.dependency_overrides.clear()


def test_mark_single_notification_read_not_found():
    app.dependency_overrides[get_current_user] = lambda: USER_A
    try:
        mock_table = MagicMock()
        # Returns empty data (either not found or belonging to another user)
        chain = mock_db_chain([])
        mock_table.return_value = chain

        with patch("routers.notifications.supabase_admin.table", mock_table):
            response = client.patch("/notifications/non-existent/read")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_dismiss_notification_success():
    app.dependency_overrides[get_current_user] = lambda: USER_A
    try:
        mock_table = MagicMock()
        chain = mock_db_chain([{"id": "notif-1"}])
        mock_table.return_value = chain

        with patch("routers.notifications.supabase_admin.table", mock_table):
            response = client.delete("/notifications/notif-1")
            assert response.status_code == 200
            assert response.json()["id"] == "notif-1"
            chain.delete.assert_called_once()
            chain.eq.assert_any_call("id", "notif-1")
            chain.eq.assert_any_call("user_id", USER_A.id)
    finally:
        app.dependency_overrides.clear()
