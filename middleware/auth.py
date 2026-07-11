from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import supabase, supabase_admin

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """Validate the Bearer JWT via Supabase Auth and return the user."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a Bearer token.",
        )
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token or session expired",
            )
        return user_response.user
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
        )


def require_role(required_role: str):
    """Dependency factory: checks the user has the given role.

    Usage:
        @router.get("/admin/dashboard")
        def dashboard(user = Depends(require_role("admin"))):
            ...
    """
    def role_checker(user=Depends(get_current_user)):
        profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
        profile_data = profile_res.data[0] if profile_res.data else None
        role = profile_data.get("role") if profile_data else "customer"
        if role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. '{required_role}' role required.",
            )
        return user
    return role_checker


def require_any_role(*roles: str):
    """Dependency factory: checks the user has at least one of the given roles.

    Usage:
        @router.patch("/orders/{id}/status")
        def update_status(user = Depends(require_any_role("vendor", "admin"))):
            ...
    """
    def role_checker(user=Depends(get_current_user)):
        profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
        profile_data = profile_res.data[0] if profile_res.data else None
        role = profile_data.get("role") if profile_data else "customer"
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. One of {roles} roles required.",
            )
        return user
    return role_checker
