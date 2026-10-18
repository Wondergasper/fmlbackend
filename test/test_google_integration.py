"""
test_google_integration.py — Comprehensive tests for Google OAuth2 and Geolocation Tracking

Coverage:
  Google Auth:
    1.  Valid Google id_token → new user created (201/200, session returned)
    2.  Valid Google id_token → existing user found (200, no profile re-creation)
    3.  Invalid Google id_token → 401
    4.  Invalid role in body → 400
    5.  No Supabase session returned → 401

  Geolocation Service (unit tests, no HTTP):
    6.  geocode_address returns (lat, lng) on success
    7.  geocode_address returns None when API key is missing
    8.  geocode_address returns None when Google API status != OK
    9.  get_distance_and_eta returns (text, minutes) on success
   10.  get_distance_and_eta returns (None, None) when key is missing
   11.  detect_location_by_ip returns dict on success
   12.  detect_location_by_ip returns None for private IP
   13.  detect_location_by_ip returns None on API failure

  Detect-location HTTP endpoint:
   14.  Public IP resolved → 200 with detected=True
   15.  Private IP (testclient localhost) → 200 with detected=False

  Driver assignment endpoint:
   16.  Admin assigns a driver → 200
   17.  Driver self-assigns → 200
   18.  Driver cannot assign another user → 403
   19.  Customer cannot assign a driver → 403
   20.  Assigning a non-driver profile → 400
   21.  Assigning to non-existent order → 404

  Driver location update endpoint:
   22.  Assigned driver posts valid coordinates → 200, DB updated
   23.  Out-of-range coordinates → 422
   24.  Unassigned driver cannot post → 403
   25.  Customer cannot post location → 403

  Tracking endpoint:
   26.  Customer can view their own order tracking → 200
   27.  Admin can view any tracking → 200
   28.  Unrelated user cannot view tracking → 403
"""

import io
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

from main import app
from dependencies import get_current_user

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(uid: str):
    u = MagicMock()
    u.id = uid
    return u


def _chain(data):
    """Build a mock supabase chain that returns `data` from .execute()."""
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


def _auth_res(uid: str = "user-001", email: str = "test@g.com", meta: dict = None):
    """Mock a Supabase AuthResponse with user + session."""
    res = MagicMock()
    res.user.id = uid
    res.user.email = email
    res.user.user_metadata = meta or {"full_name": "Test User", "picture": "https://photo"}
    res.session.access_token = "mock-access-token"
    return res


# ===========================================================================
# 1-5: Google Auth endpoint
# ===========================================================================

class TestGoogleAuth(unittest.TestCase):

    # -----------------------------------------------------------------------
    # 1. Valid token — NEW user (no existing profile)
    # -----------------------------------------------------------------------
    def test_01_valid_token_new_user_returns_session(self):
        app.dependency_overrides.clear()

        with patch("routers.auth.supabase") as mock_sp, \
             patch("routers.auth.supabase_admin") as mock_admin:

            mock_sp.auth.sign_in_with_id_token.return_value = _auth_res()
            # Profile lookup returns empty → new user
            profile_lookup = _chain([])
            mock_admin.table.return_value = profile_lookup

            # We also need the insert to succeed
            insert_chain = _chain([{"role": "customer", "full_name": "Test User"}])
            mock_admin.table.return_value = insert_chain
            # Make profile select return empty, then insert success
            call_count = 0

            def table_side(name):
                nonlocal call_count
                call_count += 1
                if name == "profiles":
                    c = _chain([])
                    c.insert.return_value = c
                    c.execute.return_value.data = [] if call_count == 1 else [{"id": "x"}]
                    return c
                return _chain([])

            mock_admin.table.side_effect = table_side

            resp = client.post("/auth/google", json={"id_token": "valid-google-token"})

        self.assertIn(resp.status_code, [200, 201], resp.text)
        body = resp.json()
        self.assertIn("access_token", body)
        self.assertEqual(body["token_type"], "bearer")
        self.assertIn("user", body)

    # -----------------------------------------------------------------------
    # 2. Valid token — EXISTING user
    # -----------------------------------------------------------------------
    def test_02_valid_token_existing_user_no_re_create(self):
        app.dependency_overrides.clear()

        with patch("routers.auth.supabase") as mock_sp, \
             patch("routers.auth.supabase_admin") as mock_admin:

            mock_sp.auth.sign_in_with_id_token.return_value = _auth_res()

            # Profile exists
            mock_admin.table.return_value = _chain(
                [{"role": "customer", "full_name": "Test User", "wallet_balance": 500}]
            )

            resp = client.post("/auth/google", json={"id_token": "existing-token"})

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["is_new_user"], False)

    # -----------------------------------------------------------------------
    # 3. Invalid Google token → 401
    # -----------------------------------------------------------------------
    def test_03_invalid_google_token_returns_401(self):
        app.dependency_overrides.clear()

        with patch("routers.auth.supabase") as mock_sp:
            mock_sp.auth.sign_in_with_id_token.side_effect = Exception("JWT verification failed")
            resp = client.post("/auth/google", json={"id_token": "bad-token"})

        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertIn("Google Sign-In failed", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 4. Invalid role → 400
    # -----------------------------------------------------------------------
    def test_04_invalid_role_returns_400(self):
        resp = client.post("/auth/google", json={"id_token": "tok", "role": "admin"})
        self.assertEqual(resp.status_code, 400, resp.text)

    # -----------------------------------------------------------------------
    # 5. No session in auth response → 401
    # -----------------------------------------------------------------------
    def test_05_no_session_returns_401(self):
        app.dependency_overrides.clear()

        with patch("routers.auth.supabase") as mock_sp:
            no_session = MagicMock()
            no_session.session = None
            no_session.user = None
            mock_sp.auth.sign_in_with_id_token.return_value = no_session
            resp = client.post("/auth/google", json={"id_token": "empty-response"})

        self.assertEqual(resp.status_code, 401, resp.text)


# ===========================================================================
# 6-13: google_maps service unit tests
# ===========================================================================

class TestGoogleMapsService(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # 6. geocode_address success
    # -----------------------------------------------------------------------
    async def test_06_geocode_returns_lat_lng_on_success(self):
        from services.google_maps import geocode_address

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "OK",
            "results": [{"geometry": {"location": {"lat": 6.4541, "lng": 3.3947}}}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("services.google_maps.GOOGLE_MAPS_API_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await geocode_address("Lagos, Nigeria")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 6.4541)
        self.assertAlmostEqual(result[1], 3.3947)

    # -----------------------------------------------------------------------
    # 7. geocode_address — no API key
    # -----------------------------------------------------------------------
    async def test_07_geocode_returns_none_without_api_key(self):
        from services.google_maps import geocode_address

        with patch("services.google_maps.GOOGLE_MAPS_API_KEY", ""):
            result = await geocode_address("Lagos, Nigeria")

        self.assertIsNone(result)

    # -----------------------------------------------------------------------
    # 8. geocode_address — API status != OK
    # -----------------------------------------------------------------------
    async def test_08_geocode_returns_none_when_api_status_not_ok(self):
        from services.google_maps import geocode_address

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ZERO_RESULTS", "results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("services.google_maps.GOOGLE_MAPS_API_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await geocode_address("Nowhere-land XYZ")

        self.assertIsNone(result)

    # -----------------------------------------------------------------------
    # 9. get_distance_and_eta — success
    # -----------------------------------------------------------------------
    async def test_09_distance_eta_returns_values_on_success(self):
        from services.google_maps import get_distance_and_eta

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "OK",
            "rows": [{"elements": [{"status": "OK",
                                     "distance": {"text": "4.2 km"},
                                     "duration": {"value": 720}}]}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("services.google_maps.GOOGLE_MAPS_API_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            text, mins = await get_distance_and_eta(6.45, 3.39, 6.50, 3.35)

        self.assertEqual(text, "4.2 km")
        self.assertEqual(mins, 12)  # 720 seconds / 60

    # -----------------------------------------------------------------------
    # 10. get_distance_and_eta — no API key
    # -----------------------------------------------------------------------
    async def test_10_distance_eta_returns_none_without_api_key(self):
        from services.google_maps import get_distance_and_eta

        with patch("services.google_maps.GOOGLE_MAPS_API_KEY", ""):
            text, mins = await get_distance_and_eta(6.45, 3.39, 6.50, 3.35)

        self.assertIsNone(text)
        self.assertIsNone(mins)

    # -----------------------------------------------------------------------
    # 11. detect_location_by_ip — success
    # -----------------------------------------------------------------------
    async def test_11_detect_location_returns_dict_on_success(self):
        from services.google_maps import detect_location_by_ip

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "success",
            "lat": 6.4541, "lon": 3.3947,
            "city": "Lagos", "regionName": "Lagos State",
            "country": "Nigeria", "countryCode": "NG",
            "timezone": "Africa/Lagos", "isp": "Airtel Nigeria",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await detect_location_by_ip("197.210.64.1")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["latitude"], 6.4541)
        self.assertEqual(result["city"], "Lagos")
        self.assertEqual(result["country_code"], "NG")

    # -----------------------------------------------------------------------
    # 12. detect_location_by_ip — private IP skipped
    # -----------------------------------------------------------------------
    async def test_12_private_ip_returns_none(self):
        from services.google_maps import detect_location_by_ip

        result = await detect_location_by_ip("127.0.0.1")
        self.assertIsNone(result)

        result2 = await detect_location_by_ip("192.168.1.1")
        self.assertIsNone(result2)

    # -----------------------------------------------------------------------
    # 13. detect_location_by_ip — API failure → None
    # -----------------------------------------------------------------------
    async def test_13_api_failure_returns_none(self):
        from services.google_maps import detect_location_by_ip

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.side_effect = Exception("Connection refused")
            mock_client_cls.return_value = mock_client

            result = await detect_location_by_ip("197.210.64.1")

        self.assertIsNone(result)


# ===========================================================================
# 14-15: /orders/detect-location HTTP endpoint
# ===========================================================================

class TestDetectLocationEndpoint(unittest.TestCase):

    # -----------------------------------------------------------------------
    # 14. Public IP resolved
    # -----------------------------------------------------------------------
    def test_14_public_ip_returns_detected_true(self):
        user = _user("cust-001")
        app.dependency_overrides[get_current_user] = lambda: user

        with patch("routers.orders.detect_location_by_ip", new_callable=AsyncMock) as mock_det:
            mock_det.return_value = {
                "latitude": 6.45, "longitude": 3.39,
                "city": "Lagos", "region": "Lagos State",
                "country": "Nigeria", "country_code": "NG",
                "timezone": "Africa/Lagos", "isp": "Airtel",
            }
            resp = client.get("/orders/detect-location")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["detected"])
        self.assertIn("latitude", body)
        self.assertEqual(body["city"], "Lagos")

    # -----------------------------------------------------------------------
    # 15. Localhost / private IP → detected=False (graceful)
    # -----------------------------------------------------------------------
    def test_15_private_ip_returns_detected_false(self):
        user = _user("cust-001")
        app.dependency_overrides[get_current_user] = lambda: user

        with patch("routers.orders.detect_location_by_ip", new_callable=AsyncMock) as mock_det:
            mock_det.return_value = None  # private IP can't be resolved
            resp = client.get("/orders/detect-location")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["detected"])
        self.assertIn("message", body)


# ===========================================================================
# 16-21: Driver assignment endpoint
# ===========================================================================

class TestAssignDriver(unittest.TestCase):

    def _mock_dep_supabase(self, role: str):
        c = MagicMock()
        c.table.return_value = _chain([{"role": role}])
        return c

    # -----------------------------------------------------------------------
    # 16. Admin assigns a driver
    # -----------------------------------------------------------------------
    def test_16_admin_assigns_driver(self):
        admin = _user("admin-001")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "admin"}])
            mock_sp.table.return_value = _chain([{"role": "admin"}])

            def table_side(name):
                if name == "profiles":
                    return _chain([{"id": "driver-001", "full_name": "Ali Driver", "role": "driver"}])
                if name == "orders":
                    c = _chain([{"id": "order-001", "status": "Processing"}])
                    c.update.return_value = c
                    return c
                return _chain([])
            mock_admin.table.side_effect = table_side

            with patch("routers.orders.connection_manager") as mock_ws:
                mock_ws.broadcast_order_status = AsyncMock()
                resp = client.patch(
                    "/orders/order-001/assign-driver",
                    json={"driver_id": "driver-001"}
                )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("driver_id", resp.json())

    # -----------------------------------------------------------------------
    # 17. Driver self-assigns
    # -----------------------------------------------------------------------
    def test_17_driver_self_assigns(self):
        driver = _user("driver-001")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])

            def table_side(name):
                if name == "profiles":
                    return _chain([{"id": "driver-001", "full_name": "Ali", "role": "driver"}])
                if name == "orders":
                    c = _chain([{"id": "order-001", "status": "Processing"}])
                    c.update.return_value = c
                    return c
                return _chain([])
            mock_admin.table.side_effect = table_side

            with patch("routers.orders.connection_manager") as mock_ws:
                mock_ws.broadcast_order_status = AsyncMock()
                resp = client.patch(
                    "/orders/order-001/assign-driver",
                    json={"driver_id": "driver-001"}
                )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)

    # -----------------------------------------------------------------------
    # 18. Driver cannot assign another user
    # -----------------------------------------------------------------------
    def test_18_driver_cannot_assign_another(self):
        driver = _user("driver-001")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])

            resp = client.patch(
                "/orders/order-001/assign-driver",
                json={"driver_id": "driver-999"}  # different driver
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)

    # -----------------------------------------------------------------------
    # 19. Customer cannot assign a driver
    # -----------------------------------------------------------------------
    def test_19_customer_cannot_assign_driver(self):
        customer = _user("cust-001")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("routers.orders.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])

            resp = client.patch(
                "/orders/order-001/assign-driver",
                json={"driver_id": "driver-001"}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)

    # -----------------------------------------------------------------------
    # 20. Assigning a non-driver profile returns 400
    # -----------------------------------------------------------------------
    def test_20_non_driver_profile_returns_400(self):
        admin = _user("admin-001")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "admin"}])
            mock_sp.table.return_value = _chain([{"role": "admin"}])
            # driver lookup returns a customer role
            mock_admin.table.return_value = _chain([{"id": "cust-999", "full_name": "Bob", "role": "customer"}])

            resp = client.patch(
                "/orders/order-001/assign-driver",
                json={"driver_id": "cust-999"}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 400, resp.text)

    # -----------------------------------------------------------------------
    # 21. Assigning to non-existent order returns 404
    # -----------------------------------------------------------------------
    def test_21_nonexistent_order_returns_404(self):
        admin = _user("admin-001")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "admin"}])
            mock_sp.table.return_value = _chain([{"role": "admin"}])

            call_n = 0
            def table_side(name):
                nonlocal call_n
                call_n += 1
                if name == "profiles":
                    return _chain([{"id": "driver-001", "full_name": "Ali", "role": "driver"}])
                # orders returns empty → 404
                return _chain([])
            mock_admin.table.side_effect = table_side

            resp = client.patch(
                "/orders/ghost-order/assign-driver",
                json={"driver_id": "driver-001"}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 404, resp.text)


# ===========================================================================
# 22-25: Driver location update endpoint
# ===========================================================================

class TestDriverLocationUpdate(unittest.TestCase):

    # -----------------------------------------------------------------------
    # 22. Assigned driver posts valid coordinates
    # -----------------------------------------------------------------------
    def test_22_assigned_driver_posts_valid_location(self):
        driver = _user("driver-001")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("routers.orders.get_distance_and_eta", new_callable=AsyncMock) as mock_eta, \
             patch("routers.orders.connection_manager") as mock_ws, \
             patch("dependencies.supabase") as mock_dep:

            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])
            mock_eta.return_value = ("3.1 km", 8)
            mock_ws.broadcast_to_order = AsyncMock()

            order_chain = _chain([{
                "id": "order-001",
                "driver_id": "driver-001",
                "latitude": 6.45,
                "longitude": 3.39,
                "status": "In Transit",
            }])
            order_chain.update.return_value = order_chain

            mock_admin.table.return_value = order_chain

            resp = client.patch(
                "/orders/order-001/location",
                json={"latitude": 6.44, "longitude": 3.38}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["latitude"], 6.44)
        self.assertEqual(body["distance_text"], "3.1 km")
        self.assertEqual(body["eta_minutes"], 8)

    # -----------------------------------------------------------------------
    # 23. Out-of-range coordinates → 422
    # -----------------------------------------------------------------------
    def test_23_out_of_range_coordinates_returns_422(self):
        driver = _user("driver-001")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])

            resp = client.patch(
                "/orders/order-001/location",
                json={"latitude": 999.0, "longitude": 3.38}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 422, resp.text)

    # -----------------------------------------------------------------------
    # 24. Unassigned driver cannot update location
    # -----------------------------------------------------------------------
    def test_24_unassigned_driver_returns_403(self):
        driver = _user("driver-002")
        app.dependency_overrides[get_current_user] = lambda: driver

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "driver"}])
            mock_sp.table.return_value = _chain([{"role": "driver"}])
            # Order is assigned to driver-001, not driver-002
            mock_admin.table.return_value = _chain([{
                "id": "order-001",
                "driver_id": "driver-001",
                "latitude": 6.45,
                "longitude": 3.39,
                "status": "In Transit",
            }])

            resp = client.patch(
                "/orders/order-001/location",
                json={"latitude": 6.44, "longitude": 3.38}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)

    # -----------------------------------------------------------------------
    # 25. Customer cannot post location
    # -----------------------------------------------------------------------
    def test_25_customer_cannot_post_location(self):
        customer = _user("cust-001")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("routers.orders.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])

            resp = client.patch(
                "/orders/order-001/location",
                json={"latitude": 6.44, "longitude": 3.38}
            )

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)


# ===========================================================================
# 26-28: Tracking endpoint
# ===========================================================================

class TestOrderTracking(unittest.TestCase):

    def _order_row(self, customer_id="cust-001", driver_id=None):
        return {
            "id": "order-001",
            "status": "In Transit",
            "delivery_address": "42 Awolowo Road, Ikoyi, Lagos",
            "delivery_type": "standard",
            "latitude": 6.45,
            "longitude": 3.39,
            "driver_id": driver_id,
            "driver_latitude": 6.44,
            "driver_longitude": 3.38,
            "eta_minutes": 8,
            "distance_text": "3.1 km",
            "customer_id": customer_id,
            "order_items": [],
        }

    # -----------------------------------------------------------------------
    # 26. Customer can view their own tracking
    # -----------------------------------------------------------------------
    def test_26_customer_views_own_tracking(self):
        customer = _user("cust-001")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])
            mock_admin.table.return_value = _chain([self._order_row()])

            resp = client.get("/orders/order-001/tracking")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("destination", body)
        self.assertIn("driver", body)
        self.assertEqual(body["eta_minutes"], 8)

    # -----------------------------------------------------------------------
    # 27. Admin can view any tracking
    # -----------------------------------------------------------------------
    def test_27_admin_views_any_tracking(self):
        admin = _user("admin-001")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "admin"}])
            mock_sp.table.return_value = _chain([{"role": "admin"}])
            mock_admin.table.return_value = _chain([self._order_row(customer_id="someone-else")])

            resp = client.get("/orders/order-001/tracking")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)

    # -----------------------------------------------------------------------
    # 28. Unrelated user cannot view tracking
    # -----------------------------------------------------------------------
    def test_28_unrelated_user_cannot_view_tracking(self):
        stranger = _user("stranger-999")
        app.dependency_overrides[get_current_user] = lambda: stranger

        with patch("routers.orders.supabase") as mock_sp, \
             patch("routers.orders.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep:
            mock_dep.table.return_value = _chain([{"role": "customer"}])
            mock_sp.table.return_value = _chain([{"role": "customer"}])
            # Order belongs to cust-001, not stranger-999
            mock_admin.table.return_value = _chain([self._order_row(customer_id="cust-001")])

            resp = client.get("/orders/order-001/tracking")

        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)


if __name__ == "__main__":
    unittest.main()
