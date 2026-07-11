from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import supabase, supabase_admin

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Validates the JWT token against Supabase Auth"""
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token or session expired"
            )
        return user_response.user
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )

def require_role(allowed_roles: list[str]):
    """Enforces role-based permissions at the endpoint level"""
    async def dependency(user = Depends(get_current_user)):
        # Retrieve the user profile from database to verify the role
        profile_res = supabase.table("profiles").select("role").eq("id", user.id).execute()
        profile_data = profile_res.data[0] if profile_res.data else None
        if not profile_data or profile_data.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Insufficient permissions for this resource"
            )
        return user
    return dependency
