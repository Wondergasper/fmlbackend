"""
middleware/auth.py — Canonical authentication & authorisation dependencies.

This is the SINGLE source of truth for all auth-related FastAPI dependencies.
All routers should import from here (directly or via dependencies.py which
re-exports these symbols for backward compatibility).

Exports:
  get_current_user   — Validates the Bearer JWT via Supabase Auth; returns the user object.
  require_role       — Dependency factory; enforces that the caller holds one of the
                       supplied roles (accepts a list[str] or a single str).
  require_any_role   — Alias for require_role; kept for readability in routes where
                       the multi-role intent should be explicit.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import supabase

# auto_error=False lets us return a descriptive 401 instead of FastAPI's
# generic "Not authenticated" response when the header is missing.
bearer_scheme = HTTPBearer(auto_error=False)


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


def require_role(allowed_roles: list[str] | str):
    """Dependency factory: enforce that the caller holds one of the given roles.

    Accepts either a list of role strings or a single role string so that
    all call-site signatures are supported:

        user=Depends(require_role(["admin"]))           # single role via list
        user=Depends(require_role(["vendor", "admin"])) # multiple roles
        user=Depends(require_role("admin"))             # single role as str

    Raises HTTP 401 if the user is not authenticated.
    Raises HTTP 403 if the user's role is not in the allowed set.
    """
    # Normalise to a set for O(1) membership checks
    if isinstance(allowed_roles, str):
        roles_set = {allowed_roles}
    else:
        roles_set = set(allowed_roles)

    async def dependency(user=Depends(get_current_user)):
        profile_res = (
            supabase.table("profiles")
            .select("role")
            .eq("id", user.id)
            .execute()
        )
        profile_data = profile_res.data[0] if profile_res.data else None
        actual_role = profile_data.get("role") if profile_data else None

        if actual_role not in roles_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. Required role: {sorted(roles_set)}. "
                    f"Your role: '{actual_role}'."
                ),
            )
        return user

    return dependency


def require_any_role(*roles: str):
    """Alias for require_role with variadic positional arguments.

    Usage:
        user=Depends(require_any_role("vendor", "admin"))

    This is a readability alias — the underlying logic is identical to
    require_role when a list is passed.
    """
    return require_role(list(roles))
