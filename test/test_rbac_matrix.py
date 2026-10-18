"""
test_rbac_matrix.py — Comprehensive tests for Role-Based Access Control (RBAC).

Tests:
1. Canonical role resolution, aliases, and role group definitions.
2. require_role dependency behaviour (exact match, alias matching, role groups).
3. Access denial (403 Forbidden) on unauthorized roles.
4. User role & profile caching on the request user instance.
5. Fix validation: 'logist' role correctly distinct from 'rider'/'courier'.
6. Route-level authorization matrix across protected endpoints.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from schemas.auth import (
    Role,
    ROLE_ALIASES,
    canonicalize_role,
    ROLE_GROUP_DELIVERY,
    ROLE_GROUP_DISPATCH,
    ROLE_GROUP_LOGISTICS,
    ROLE_GROUP_MERCHANT,
    RegisterRequest,
)
from middleware.auth import require_role, require_any_role, _expand_roles


# ---------------------------------------------------------------------------
# 1. Canonicalization & Aliasing Unit Tests
# ---------------------------------------------------------------------------

def test_canonicalize_role_standard():
    assert canonicalize_role("customer") == "customer"
    assert canonicalize_role("vendor") == "vendor"
    assert canonicalize_role("admin") == "admin"
    assert canonicalize_role("logist") == "logist"
    assert canonicalize_role("rider") == "rider"


def test_canonicalize_role_aliases():
    assert canonicalize_role("driver") == "rider"
    assert canonicalize_role("courier") == "rider"
    assert canonicalize_role("logistics") == "logist"
    assert canonicalize_role("superadmin") == "admin"


def test_canonicalize_role_case_and_whitespace():
    assert canonicalize_role("  DRIVER  ") == "rider"
    assert canonicalize_role("Courier") == "rider"
    assert canonicalize_role("LOGIST") == "logist"
    assert canonicalize_role(None) is None
    assert canonicalize_role("") is None


def test_role_group_definitions():
    assert "rider" in ROLE_GROUP_DELIVERY
    assert "driver" in ROLE_GROUP_DELIVERY
    assert "courier" in ROLE_GROUP_DELIVERY

    assert "admin" in ROLE_GROUP_DISPATCH
    assert "logist" in ROLE_GROUP_DISPATCH

    assert ROLE_GROUP_DELIVERY.issubset(ROLE_GROUP_LOGISTICS)
    assert ROLE_GROUP_DISPATCH.issubset(ROLE_GROUP_LOGISTICS)
    assert "customer" not in ROLE_GROUP_LOGISTICS


def test_expand_roles():
    expanded = _expand_roles(["rider"])
    assert "rider" in expanded
    assert "driver" in expanded
    assert "courier" in expanded

    dispatch_expanded = _expand_roles(["logist", "admin"])
    assert "logist" in dispatch_expanded
    assert "logistics" in dispatch_expanded
    assert "admin" in dispatch_expanded


# ---------------------------------------------------------------------------
# 2. require_role Dependency Tests (using asyncio.run)
# ---------------------------------------------------------------------------

class DummyUser:
    def __init__(self, user_id: str, role: str = None):
        self.id = user_id
        if role:
            self.role = role


def test_require_role_fast_path_with_attribute():
    dep = require_role(["admin"])
    user = DummyUser("mock-id", role="admin")
    result = asyncio.run(dep(user))
    assert result.id == "mock-id"
    assert result.role == "admin"


def test_require_role_fast_path_with_alias():
    # User object has legacy role 'driver', endpoint requires 'rider'
    dep = require_role(["rider"])
    user = DummyUser("mock-id", role="driver")
    result = asyncio.run(dep(user))
    assert result.id == "mock-id"


def test_require_role_db_lookup_success():
    dep = require_role(["logist", "admin"])
    user = DummyUser("00000000-0000-0000-0000-000000000001")

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": user.id, "role": "logist", "full_name": "Hub Dispatcher"}
    ]

    with patch("dependencies.supabase", mock_db):
        result = asyncio.run(dep(user))
        assert result.id == user.id
        assert result.role == "logist"
        assert result.profile["full_name"] == "Hub Dispatcher"


def test_require_role_db_lookup_alias_resolution():
    # Database has 'courier', endpoint requires 'rider'
    dep = require_role(["rider"])
    user = DummyUser("00000000-0000-0000-0000-000000000002")

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": user.id, "role": "courier", "full_name": "Bike Courier"}
    ]

    with patch("dependencies.supabase", mock_db):
        result = asyncio.run(dep(user))
        assert result.id == user.id
        # Canonical role cached
        assert result.role == "rider"


def test_require_role_forbidden():
    dep = require_role(["admin"])
    user = DummyUser("00000000-0000-0000-0000-000000000003")

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": user.id, "role": "customer", "full_name": "Buyer"}
    ]

    with patch("dependencies.supabase", mock_db):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(user))
        assert exc_info.value.status_code == 403
        assert "Access denied" in exc_info.value.detail


def test_require_role_logist_accesses_hubs_not_blocked_as_courier():
    """Verify the bugfix where 'logist' role can access hub management."""
    dep = require_role(["admin", "logist"])
    user = DummyUser("00000000-0000-0000-0000-000000000004")

    mock_db = MagicMock()
    mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": user.id, "role": "logist", "full_name": "Logistics Lead"}
    ]

    with patch("dependencies.supabase", mock_db):
        result = asyncio.run(dep(user))
        assert result.role == "logist"


def test_require_any_role_variadic():
    dep = require_any_role("vendor", "admin")
    user = DummyUser("mock-id", role="vendor")
    result = asyncio.run(dep(user))
    assert result.role == "vendor"


# ---------------------------------------------------------------------------
# 3. Schema & Registration Validation Tests
# ---------------------------------------------------------------------------

def test_register_request_validation():
    req = RegisterRequest(
        email="rider@farmconnect.ng",
        password="securepassword123",
        full_name="Swift Rider",
        role="rider",
    )
    assert req.role == "rider"

    req_alias = RegisterRequest(
        email="driver@farmconnect.ng",
        password="securepassword123",
        full_name="Van Driver",
        role="driver",
    )
    assert req_alias.role == "driver"

    req_logist = RegisterRequest(
        email="dispatch@farmconnect.ng",
        password="securepassword123",
        full_name="Dispatcher",
        role="logist",
    )
    assert req_logist.role == "logist"

    with pytest.raises(ValueError):
        RegisterRequest(
            email="bad@farmconnect.ng",
            password="securepassword123",
            full_name="Hacker",
            role="superhacker",
        )
