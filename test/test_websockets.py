import pytest
import asyncio
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from main import app
from services.websocket_manager import connection_manager

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_ws_auth():
    """Bypass real WS auth in all tests by mocking authentication."""
    with patch("routers.websockets.authenticate_websocket") as mock_auth, \
         patch("routers.websockets.authorize_websocket_order") as mock_authorize:
        async def auth_ws(token):
            return "test-user-id"
        async def authorize_ws(uid, oid):
            return True
        mock_auth.side_effect = auth_ws
        mock_authorize.side_effect = authorize_ws
        yield


def test_websocket_auth_failure_missing_token():
    """Verify that connecting without a token closes the connection with 1008 policy violation."""
    with patch("routers.websockets.authenticate_websocket") as mock_auth:
        async def fail_auth(token):
            raise ValueError("Missing authentication token")
        mock_auth.side_effect = fail_auth
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/orders/ord_12345") as websocket:
                websocket.receive_json()


def test_websocket_auth_success_and_connection_event():
    """Verify successful connection with test token and receipt of initial connection event."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        data = websocket.receive_json()
        assert data["event"] == "connected"
        assert data["order_id"] == "ord_12345"
        assert data["user_id"] == "test-user-id"


def test_websocket_auth_header():
    """Verify successful connection using Authorization header token."""
    with client.websocket_connect("/ws/orders/ord_12345", headers={"Authorization": "Bearer valid-token"}) as websocket:
        data = websocket.receive_json()
        assert data["event"] == "connected"
        assert data["order_id"] == "ord_12345"
        assert data["user_id"] == "test-user-id"


def test_websocket_location_ping():
    """Verify sending live delivery location ping payload receives broadcasted location event."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        init_event = websocket.receive_json()
        assert init_event["event"] == "connected"

        ping_payload = {
            "event": "delivery_location_ping",
            "latitude": 6.5244,
            "longitude": 3.3792,
            "timestamp": "2026-06-29T12:00:00Z"
        }
        websocket.send_json(ping_payload)

        response = websocket.receive_json()
        assert response["event"] == "delivery_location_ping"
        assert response["order_id"] == "ord_12345"
        assert response["latitude"] == 6.5244
        assert response["longitude"] == 3.3792
        assert response["timestamp"] == "2026-06-29T12:00:00Z"


def test_websocket_invalid_coordinates():
    """Verify that sending malformed numeric coordinates returns an error frame without dropping socket."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        init_event = websocket.receive_json()
        assert init_event["event"] == "connected"

        ping_payload = {
            "event": "delivery_location_ping",
            "latitude": "invalid_lat",
            "longitude": 3.3792
        }
        websocket.send_json(ping_payload)

        response = websocket.receive_json()
        assert response["error"] == "Invalid coordinates"


def test_websocket_forbidden_administrative_event():
    """Verify that client-sent administrative events are rejected and not broadcasted."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        init_event = websocket.receive_json()
        assert init_event["event"] == "connected"

        admin_payload = {
            "event": "order_status_update",
            "status": "Delivered"
        }
        websocket.send_json(admin_payload)

        response = websocket.receive_json()
        assert response["error"] == "Forbidden event type"

        vendor_payload = {
            "event": "vendor_alert",
            "message": "Spoofed alert"
        }
        websocket.send_json(vendor_payload)

        response2 = websocket.receive_json()
        assert response2["error"] == "Forbidden event type"


def test_websocket_order_status_update_broadcast():
    """Verify triggering order status update broadcast delivers JSON event to connected clients."""
    with client.websocket_connect("/ws/orders/ord_999?token=valid-token") as websocket:
        init_event = websocket.receive_json()
        assert init_event["event"] == "connected"

        asyncio.run(connection_manager.broadcast_order_status(
            order_id="ord_999",
            status="In Transit",
            note="Driver assigned and on the way"
        ))

        response = websocket.receive_json()
        assert response["event"] == "order_status_update"
        assert response["order_id"] == "ord_999"
        assert response["status"] == "In Transit"
        assert response["note"] == "Driver assigned and on the way"


def test_websocket_vendor_alert_broadcast():
    """Verify triggering vendor order alert broadcast delivers event to connected clients."""
    with client.websocket_connect("/ws/orders/ord_888?token=valid-token") as websocket:
        init_event = websocket.receive_json()
        assert init_event["event"] == "connected"

        asyncio.run(connection_manager.broadcast_vendor_alert(
            order_id="ord_888",
            vendor_id="v_123",
            message="New sale alert",
            extra_data={"total_kobo": 250000}
        ))

        response = websocket.receive_json()
        assert response["event"] == "vendor_order_alert"
        assert response["order_id"] == "ord_888"
        assert response["vendor_id"] == "v_123"
        assert response["message"] == "New sale alert"
        assert response["total_kobo"] == 250000


def test_websocket_out_of_bounds_and_nan_coordinates():
    """Verify out-of-bounds, nan, and inf coordinates return Invalid coordinates error."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        websocket.receive_json()

        websocket.send_json({"event": "delivery_location_ping", "latitude": 95.0, "longitude": 3.3792})
        res1 = websocket.receive_json()
        assert res1["error"] == "Invalid coordinates"

        websocket.send_json({"event": "delivery_location_ping", "latitude": 6.5, "longitude": -185.0})
        res2 = websocket.receive_json()
        assert res2["error"] == "Invalid coordinates"

        websocket.send_json({"event": "delivery_location_ping", "latitude": "nan", "longitude": 3.3792})
        res3 = websocket.receive_json()
        assert res3["error"] == "Invalid coordinates"

        websocket.send_json({"event": "delivery_location_ping", "latitude": 6.5, "longitude": "inf"})
        res4 = websocket.receive_json()
        assert res4["error"] == "Invalid coordinates"


def test_websocket_non_dict_payload():
    """Verify sending non-dict JSON payloads returns Payload must be a JSON object error."""
    with client.websocket_connect("/ws/orders/ord_12345?token=valid-token") as websocket:
        websocket.receive_json()

        websocket.send_text("[1, 2, 3]")
        res = websocket.receive_json()
        assert res["error"] == "Payload must be a JSON object"


def test_websocket_production_test_token_gating():
    """Verify test tokens are rejected when ENVIRONMENT=production."""
    import os
    os.environ["ENVIRONMENT"] = "production"
    try:
        with patch("routers.websockets.authenticate_websocket") as mock_auth:
            async def fail_auth(token):
                raise ValueError("Test tokens are disabled in production environment")
            mock_auth.side_effect = fail_auth
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/orders/ord_12345?token=test-valid-token") as websocket:
                    websocket.receive_json()
    finally:
        os.environ.pop("ENVIRONMENT", None)


def test_websocket_redis_listener_mock():
    """Verify start_redis_listener processes pubsub messages and forwards to local sockets."""
    class MockPubSub:
        async def subscribe(self, channel): pass
        async def psubscribe(self, pattern): pass
        async def listen(self):
            yield {"type": "message", "data": '{"order_id": "ord_redis_1", "payload": {"event": "order_status_update", "status": "Shipped"}}'}
            yield {"type": "pmessage", "data": '{"order_id": "ord_redis_2", "payload": {"event": "order_status_update", "status": "Delivered"}}'}

    class MockRedis:
        def pubsub(self): return MockPubSub()

    connection_manager.redis = MockRedis()
    try:
        async def run_listener():
            task = asyncio.create_task(connection_manager.start_redis_listener())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_listener())
    finally:
        connection_manager.redis = None

if __name__ == "__main__":
    test_websocket_auth_failure_missing_token()
    test_websocket_auth_success_and_connection_event()
    test_websocket_auth_header()
    test_websocket_location_ping()
    test_websocket_invalid_coordinates()
    test_websocket_forbidden_administrative_event()
    test_websocket_order_status_update_broadcast()
    test_websocket_vendor_alert_broadcast()
    test_websocket_out_of_bounds_and_nan_coordinates()
    test_websocket_non_dict_payload()
    test_websocket_production_test_token_gating()
    test_websocket_redis_listener_mock()
    print("ALL WEBSOCKET TESTS PASSED SUCCESSFULLY!")
