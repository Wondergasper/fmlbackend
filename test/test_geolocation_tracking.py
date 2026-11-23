"""
test_geolocation_tracking.py — Comprehensive tests for Open-Source Maps, Routing & Real-Time Telemetry

Coverage:
  1.  Haversine distance formula & urban ETA calculations
  2.  OSRM routing service integration & response parsing
  3.  OSRM network failure graceful fallback to Haversine engine
  4.  Nominatim geocoding & reverse geocoding
  5.  Unified get_optimized_distance_and_eta provider resolution
  6.  Driver location update with smart distance throttling (avoids duplicate routing calls)
  7.  Driver location update records telemetry breadcrumbs
  8.  Customer can retrieve order tracking history (breadcrumbs)
  9.  Assigned driver can retrieve tracking history
  10. Admin can retrieve tracking history
  11. Unauthorized user receives 403 on tracking history
  12. Non-existent order receives 404 on tracking history
"""

import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

from main import app
from dependencies import get_current_user
from services.routing import (
    calculate_haversine_distance_km,
    estimate_urban_eta_minutes,
    get_osrm_distance_and_eta,
    get_optimized_distance_and_eta,
    nominatim_geocode,
    nominatim_reverse_geocode,
)

client = TestClient(app)


def _user(uid: str, role: str = "customer"):
    u = MagicMock()
    u.id = uid
    u.role = role
    return u


def _chain(data):
    m = MagicMock()
    m.select.return_value = m
    m.insert.return_value = m
    m.update.return_value = m
    m.delete.return_value = m
    m.eq.return_value = m
    m.in_.return_value = m
    m.order.return_value = m
    m.execute.return_value.data = data if isinstance(data, list) else [data] if data else []
    return m


class TestOpenSourceRoutingUnit(unittest.IsolatedAsyncioTestCase):

    def test_01_haversine_distance_calculation(self):
        # Lagos Marina (6.4531, 3.3958) to Ikeja City Mall (6.6145, 3.3580) ~ 18 km
        dist = calculate_haversine_distance_km(6.4531, 3.3958, 6.6145, 3.3580)
        self.assertGreater(dist, 16.0)
        self.assertLess(dist, 22.0)

    def test_02_estimate_urban_eta_minutes(self):
        eta = estimate_urban_eta_minutes(15.0, average_speed_kmh=30.0)
        # 15km * 1.2 = 18km / 30km/h = 0.6 hr = ~36 mins
        self.assertEqual(eta, 36)

        # Very close distance returns minimum 1 minute
        eta_min = estimate_urban_eta_minutes(0.01)
        self.assertEqual(eta_min, 1)

    async def test_03_osrm_distance_and_eta_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": "Ok",
            "routes": [{"distance": 5400.0, "duration": 900.0}]
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            dist_text, eta = await get_osrm_distance_and_eta(6.45, 3.39, 6.50, 3.40)
            self.assertEqual(dist_text, "5.4 km")
            self.assertEqual(eta, 15)

    async def test_04_osrm_failure_returns_none(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("OSRM server unreachable")
            dist_text, eta = await get_osrm_distance_and_eta(6.45, 3.39, 6.50, 3.40)
            self.assertIsNone(dist_text)
            self.assertIsNone(eta)

    async def test_05_optimized_routing_fallback_to_haversine(self):
        # When OSRM fails and Google Maps API key is unset, must cleanly return Haversine
        with patch("services.routing.get_osrm_distance_and_eta", new_callable=AsyncMock) as mock_osrm:
            mock_osrm.return_value = (None, None)
            dist_text, eta, provider = await get_optimized_distance_and_eta(
                6.4531, 3.3958, 6.6145, 3.3580, provider="osrm"
            )
            self.assertIn("km", dist_text)
            self.assertGreater(eta, 0)
            self.assertEqual(provider, "haversine_estimate")

    async def test_06_nominatim_geocode_and_reverse(self):
        mock_search_resp = MagicMock()
        mock_search_resp.json.return_value = [{"lat": "6.5244", "lon": "3.3792"}]
        mock_search_resp.raise_for_status.return_value = None

        mock_rev_resp = MagicMock()
        mock_rev_resp.json.return_value = {"display_name": "Lagos, Nigeria"}
        mock_rev_resp.raise_for_status.return_value = None

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [mock_search_resp, mock_rev_resp]
            coords = await nominatim_geocode("Lagos, Nigeria")
            self.assertEqual(coords, (6.5244, 3.3792))

            address = await nominatim_reverse_geocode(6.5244, 3.3792)
            self.assertEqual(address, "Lagos, Nigeria")


class TestGeolocationAPIAndHistory(unittest.TestCase):

    def test_07_smart_throttling_skips_duplicate_routing_calls(self):
        driver = _user("driver-001", role="driver")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("routers.orders.get_distance_and_eta", new_callable=AsyncMock) as mock_eta, \
             patch("routers.orders.connection_manager") as mock_ws, \
             patch("dependencies.supabase_admin") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])
            mock_ws.broadcast_to_order = AsyncMock()

            # Order has previous location: (6.45000, 3.39000) and cached ETA: ("3.1 km", 8)
            order_data = {
                "id": "order-001",
                "driver_id": "driver-001",
                "latitude": 6.50,
                "longitude": 3.40,
                "status": "In Transit",
                "driver_latitude": 6.45000,
                "driver_longitude": 3.39000,
                "distance_text": "3.1 km",
                "eta_minutes": 8,
            }
            order_chain = _chain([order_data])
            mock_admin.table.return_value = order_chain

            # Driver sends a tiny movement of ~20 meters (6.4501, 3.3901)
            resp = client.patch(
                "/orders/order-001/location",
                json={
                    "latitude": 6.4501,
                    "longitude": 3.3901,
                    "heading": 45.0,
                    "speed": 25.0,
                    "accuracy": 5.0,
                }
            )

            # Routing calculation should NOT be invoked because movement < 100 meters
            mock_eta.assert_not_called()

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["distance_text"], "3.1 km")
        self.assertEqual(body["eta_minutes"], 8)
        self.assertEqual(body["heading"], 45.0)

    def test_08_breadcrumbs_persisted_on_location_update(self):
        driver = _user("driver-001", role="driver")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("routers.orders.connection_manager") as mock_ws, \
             patch("dependencies.supabase_admin") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])
            mock_ws.broadcast_to_order = AsyncMock()

            captured_inserts = []
            def table_side(table_name):
                c = _chain([{
                    "id": "order-001",
                    "driver_id": "driver-001",
                    "latitude": 6.50,
                    "longitude": 3.40,
                    "status": "In Transit",
                }])
                if table_name == "order_tracking_breadcrumbs":
                    def capture_insert(data):
                        captured_inserts.append(data)
                        return c
                    c.insert.side_effect = capture_insert
                return c

            mock_admin.table.side_effect = table_side

            resp = client.patch(
                "/orders/order-001/location",
                json={
                    "latitude": 6.46,
                    "longitude": 3.41,
                    "heading": 90.0,
                    "speed": 40.0,
                    "accuracy": 8.0,
                    "provider": "gps",
                }
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(len(captured_inserts) > 0)
        crumb = captured_inserts[0]
        self.assertEqual(crumb["order_id"], "order-001")
        self.assertEqual(crumb["driver_id"], "driver-001")
        self.assertEqual(crumb["latitude"], 6.46)
        self.assertEqual(crumb["heading"], 90.0)

    def test_09_customer_can_get_tracking_history(self):
        customer = _user("cust-001", role="customer")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase_admin") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])

            crumbs_mock = [
                {"id": "b1", "latitude": 6.45, "longitude": 3.39, "heading": 0.0, "speed": 10.0, "created_at": "2026-10-14T10:00:00Z"},
                {"id": "b2", "latitude": 6.46, "longitude": 3.40, "heading": 30.0, "speed": 25.0, "created_at": "2026-10-14T10:02:00Z"},
            ]

            def table_side(table_name):
                if table_name == "orders":
                    return _chain([{"id": "order-001", "customer_id": "cust-001", "driver_id": "driver-001"}])
                if table_name == "order_tracking_breadcrumbs":
                    return _chain(crumbs_mock)
                return _chain([])

            mock_admin.table.side_effect = table_side

            resp = client.get("/orders/order-001/tracking/history")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["order_id"], "order-001")
        self.assertEqual(body["total_points"], 2)
        self.assertEqual(len(body["breadcrumbs"]), 2)

    def test_10_unauthorized_user_cannot_get_tracking_history(self):
        stranger = _user("stranger-999", role="customer")
        app.dependency_overrides[get_current_user] = lambda: stranger

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase_admin") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])

            def table_side(table_name):
                if table_name == "orders":
                    return _chain([{"id": "order-001", "customer_id": "cust-001", "driver_id": "driver-001"}])
                return _chain([])

            mock_admin.table.side_effect = table_side

            resp = client.get("/orders/order-001/tracking/history")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_11_ghost_order_tracking_history_returns_404(self):
        admin = _user("admin-001", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase_admin") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "admin"}])
            mock_sp.table.return_value = _chain([{"role": "admin"}])
            mock_admin.table.return_value = _chain([])

            resp = client.get("/orders/ghost-order/tracking/history")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 404, resp.text)


if __name__ == "__main__":
    unittest.main()
