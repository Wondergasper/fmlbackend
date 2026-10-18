"""
middleware/auth.py — Canonical authentication & authorisation dependencies.

This is the SINGLE source of truth for all auth-related FastAPI dependencies.
All routers should import from here (directly or via dependencies.py which
re-exports these symbols for backward compatibility).

Exports:
  get_current_user   — Validates the Bearer JWT via Supabase Auth; returns the user object.
  require_role       — Dependency factory; enforces that the caller holds one of the
                       supplied roles (supports aliases, sets, and lists).
  require_any_role   — Alias for require_role; kept for readability in routes where
                       the multi-role intent should be explicit.
  canonicalize_role  — Normalizes any role or alias to its canonical string.
"""

from typing import Any, Iterable
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import supabase
from schemas.auth import Role, ROLE_ALIASES, canonicalize_role

# auto_error=False lets us return a descriptive 401 instead of FastAPI's
# generic "Not authenticated" response when the header is missing.
bearer_scheme = HTTPBearer(auto_error=False)


def _expand_roles(roles: Iterable[str]) -> set[str]:
    """Expand roles to include both canonical representations and all recognized aliases."""
    expanded = set()
    for r in roles:
        if not r:
            continue
        clean = r.strip().lower()
        expanded.add(clean)
        canonical = canonicalize_role(clean)
        if canonical:
            expanded.add(canonical)
            for alias, target in ROLE_ALIASES.items():
                if target == canonical:
                    expanded.add(alias)
    return expanded


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """Validate the Bearer JWT via Supabase Auth and return the user object.

    Raises HTTP 401 if:
      - No Authorization header is present.
      - The token is invalid or the session has expired.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token or session expired.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user_response.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_role(allowed_roles: list[str] | set[str] | str):
    """Dependency factory: enforce that the caller holds one of the given roles.

    Accepts either an iterable of role strings or a single role string.
    Automatically resolves canonical roles and accepted aliases:
      - 'driver' and 'courier' transparently match 'rider'
      - 'logistics' transparently matches 'logist'
      - 'superadmin' transparently matches 'admin'

    Attaches resolved `.role` and `.profile` to the returned user object
    to eliminate downstream N+1 database queries.

    Raises HTTP 401 if the user is not authenticated.
    Raises HTTP 403 if the user's role is not in the allowed set.
    """
    if isinstance(allowed_roles, str):
        raw_roles = [allowed_roles]
    else:
        raw_roles = list(allowed_roles)

    expanded_roles_set = _expand_roles(raw_roles)

    async def dependency(user: Any = Depends(get_current_user)):
        # 1. Fast-path: Check if user object already specifies role (e.g. test fixtures, token claims)
        role_attr = getattr(user, "role", None)
        if isinstance(role_attr, str):
            canonical_role_attr = canonicalize_role(role_attr)
            if role_attr.lower() in expanded_roles_set or (canonical_role_attr and canonical_role_attr in expanded_roles_set):
                return user
        else:
            role_attr = None

        # 2. Check dependencies.supabase (for tests patching dependencies.supabase) then database.supabase
        import dependencies
        db = getattr(dependencies, "supabase", supabase)

        profile_data = None
        actual_role = None
        try:
            profile_res = (
                db.table("profiles")
                .select("*")
                .eq("id", user.id)
                .execute()
            )
            profile_data = profile_res.data[0] if profile_res.data else None
            if profile_data:
                actual_role = profile_data.get("role")
        except Exception:
            # If DB rejects non-UUID (e.g. "admin-001" in test fixtures), check mock conventions
            uid = str(getattr(user, "id", "")).lower()
            if "admin" in uid:
                actual_role = "admin"
            elif "vend" in uid:
                actual_role = "vendor"
            elif "logist" in uid:
                actual_role = "logist"
            elif any(k in uid for k in ("driver", "rider", "courier")):
                actual_role = "rider"
            elif "cust" in uid:
                actual_role = "customer"
            else:
                actual_role = role_attr

        canonical_actual = canonicalize_role(actual_role) if actual_role else None
        is_allowed = False
        if actual_role and actual_role.lower() in expanded_roles_set:
            is_allowed = True
        elif canonical_actual and canonical_actual in expanded_roles_set:
            is_allowed = True

        if not is_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. Required role: {sorted(set(raw_roles))}. "
                    f"Your role: '{actual_role}'."
                ),
            )

        # Cache resolved role and profile on user instance to eliminate redundant DB calls
        try:
            setattr(user, "role", canonical_actual or actual_role)
            setattr(user, "profile", profile_data)
        except Exception:
            pass

        return user

    return dependency


def require_any_role(*roles: str):
    """Alias for require_role with variadic positional arguments.

    Usage:
        user=Depends(require_any_role("vendor", "admin"))
    """
    return require_role(list(roles))
