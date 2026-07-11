"""Auth service — reusable auth logic for registration and login.

This service layer abstracts authentication logic so it can be reused
across routers and unit-tested independently.
"""

from database import supabase, supabase_admin
from services.email import send_welcome_customer, send_welcome_vendor, send_admin_new_vendor


def register_user(email: str, password: str, full_name: str, role: str = "customer", phone: str | None = None) -> dict:
    """Register a new user with Supabase Auth + create profile/wallet."""
    auth_res = supabase.auth.sign_up({"email": email, "password": password})
    user = auth_res.user
    if not user:
        raise ValueError("Failed to create user")

    user_id = user.id

    profile_data = {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "role": role,
        "phone": phone or "",
        "wallet_balance": 0,
        "status": "Active" if role == "customer" else "Pending Approval",
        "email_verified": False,
    }
    supabase_admin.table("profiles").insert(profile_data).execute()

    if role == "vendor":
        from database import supabase_admin
        supabase_admin.table("profiles").update({"status": "Pending Approval"}).eq("id", user_id).execute()
        send_admin_new_vendor.delay(email, full_name)

    if role == "customer":
        send_welcome_customer.delay(email, full_name)
    elif role == "vendor":
        send_welcome_vendor.delay(email, full_name)

    return {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "role": role,
        "roles": [role],
    }


def get_user_profile(user_id: str) -> dict | None:
    """Fetch a user's profile from Supabase."""
    res = supabase.table("profiles").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else None


def get_user_role(user_id: str) -> str:
    """Get the user's role from the profiles table."""
    res = supabase.table("profiles").select("role").eq("id", user_id).execute()
    return res.data[0].get("role", "customer") if res.data else "customer"
