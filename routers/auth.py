"""
auth.py — Authentication routes for Farmers Market API

Endpoints:
  POST /auth/register  — Register a new user (customer or vendor)
  POST /auth/login     — Login and receive a JWT access token
  GET  /auth/me        — Get the current authenticated user's profile
  POST /auth/logout    — Invalidate the current session
  POST /auth/send-otp  — Send a 4-digit OTP code to the user's email
  POST /auth/verify-otp — Verify a 4-digit OTP code and mark email as verified
"""

import os
import random
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from dependencies import get_current_user
from database import supabase, supabase_admin
from services.email import (
    send_otp_email,
    send_welcome_customer,
    send_welcome_vendor,
    send_admin_new_vendor,
)

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@farmconnect.ng")

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: str = "customer"   # "customer" | "vendor"
    phone: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    id_token: str
    role: str = "customer"  # Used only when the account is brand-new


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest):
    """
    Register a new customer or vendor account.
    Creates the Supabase Auth user, then inserts a profile row.
    """
    allowed_roles = {"customer", "vendor"}
    if payload.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {allowed_roles}"
        )

    # 1. Create the Supabase Auth user
    try:
        auth_res = supabase.auth.sign_up({
            "email": payload.email,
            "password": payload.password,
        })
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Registration failed: {str(e)}"
        )

    if not auth_res.user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not create account. Email may already be registered."
        )

    user_id = auth_res.user.id

    # 2. Insert a profile row via the admin client (bypasses RLS)
    profile_data = {
        "id": user_id,
        "email": payload.email,
        "full_name": payload.full_name,
        "role": payload.role,
        "phone": payload.phone,
        "wallet_balance": 0,
    }
    try:
        supabase_admin.table("profiles").insert(profile_data).execute()
    except Exception as e:
        # Auth user created but profile failed — log and continue
        print(f"[WARN] Profile insert failed for {user_id}: {e}")

    # ── Email notifications ────────────────────────────────────────────────
    if payload.role == "customer":
        send_welcome_customer.delay(payload.email, payload.full_name)
    elif payload.role == "vendor":
        send_welcome_vendor.delay(payload.email, payload.full_name)
        send_admin_new_vendor.delay(
            ADMIN_EMAIL,
            payload.full_name,
            payload.email,
            user_id,
        )

    return {
        "message": "Account created successfully. Please verify your email.",
        "user_id": user_id,
        "email": payload.email,
        "role": payload.role,
    }


@router.post("/login")
async def login(payload: LoginRequest):
    """
    Authenticate a user with email/password.
    Returns the Supabase session object containing the access_token.
    """
    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Login failed: {str(e)}"
        )

    if not auth_res.session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials."
        )

    # Fetch role from the profiles table (handle missing profile gracefully)
    profile_res = (
        supabase.table("profiles")
        .select("role, full_name, wallet_balance")
        .eq("id", auth_res.user.id)
        .execute()
    )
    profile_data = profile_res.data[0] if profile_res.data else None

    if not profile_data:
        # Auto-create profile for existing Auth users without one
        try:
            supabase_admin.table("profiles").insert({
                "id": auth_res.user.id,
                "email": auth_res.user.email,
                "full_name": auth_res.user.email.split("@")[0],
                "role": "customer",
                "wallet_balance": 0,
            }).execute()
            profile_data = {"role": "customer", "full_name": auth_res.user.email.split("@")[0]}
        except Exception as exc:
            print(f"[WARN] Auto-profile creation failed for {auth_res.user.id}: {exc}")

    return {
        "access_token": auth_res.session.access_token,
        "token_type": "bearer",
        "user": {
            "id": auth_res.user.id,
            "email": auth_res.user.email,
            "role": profile_data.get("role") if profile_data else None,
            "full_name": profile_data.get("full_name") if profile_data else None,
        }
    }


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.
    Requires a valid Bearer token in the Authorization header.
    """
    profile_res = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user.id)
        .execute()
    )
    profile_data = profile_res.data[0] if profile_res.data else None

    if not profile_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found."
        )

    return profile_data


# ---------------------------------------------------------------------------
# OTP Models
# ---------------------------------------------------------------------------

class SendOtpRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp_code: str


# ---------------------------------------------------------------------------
# OTP Endpoints
# ---------------------------------------------------------------------------

@router.post("/send-otp", status_code=status.HTTP_200_OK)
async def send_otp(payload: SendOtpRequest):
    """
    Generate a 4-digit OTP, store it on the user's profile, and send it via email.
    OTP expires after 10 minutes.
    """
    profile_res = (
        supabase_admin.table("profiles")
        .select("id, full_name")
        .eq("email", payload.email)
        .execute()
    )
    profile = profile_res.data[0] if profile_res.data else None
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email."
        )

    otp_code = f"{random.randint(0, 9999):04d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    supabase_admin.table("profiles").update({
        "otp_code": otp_code,
        "otp_expires_at": expires_at.isoformat(),
    }).eq("id", profile["id"]).execute()

    send_otp_email.delay(payload.email, profile["full_name"], otp_code)

    return {"message": "OTP sent to your email.", "email": payload.email}


@router.post("/verify-otp", status_code=status.HTTP_200_OK)
async def verify_otp(payload: VerifyOtpRequest):
    """
    Verify a 4-digit OTP code. Marks the email as verified on success.
    """
    profile_res = (
        supabase_admin.table("profiles")
        .select("id, full_name, otp_code, otp_expires_at, email_verified")
        .eq("email", payload.email)
        .execute()
    )
    profile = profile_res.data[0] if profile_res.data else None
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email."
        )

    if profile.get("email_verified"):
        return {"message": "Email already verified.", "email": payload.email, "verified": True}

    stored_code = profile.get("otp_code")
    if not stored_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OTP has been sent. Please request a new code."
        )

    if stored_code != payload.otp_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect verification code."
        )

    expires_at = profile.get("otp_expires_at")
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OTP has expired. Please request a new code."
            )

    supabase_admin.table("profiles").update({
        "email_verified": True,
        "otp_code": None,
        "otp_expires_at": None,
    }).eq("id", profile["id"]).execute()

    return {"message": "Email verified successfully.", "email": payload.email, "verified": True}


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    """
    Sign the current user out (invalidates the session on Supabase).
    """
    try:
        supabase.auth.sign_out()
    except Exception:
        pass  # Treat as a success regardless
    return {"message": "Logged out successfully."}


# ---------------------------------------------------------------------------
# Google OAuth2 Sign-In
# ---------------------------------------------------------------------------

@router.post("/google")
async def google_login(payload: GoogleLoginRequest):
    """
    Authenticate (or register) a user via Google Sign-In.

    **How to use from the frontend:**
    1. Trigger Google's Identity Services SDK or OAuth2 flow.
    2. Receive the `credential` (ID Token JWT) from Google.
    3. POST it here as `{ "id_token": "<jwt>", "role": "customer" }`.

    Supabase validates the JWT against Google's public keys, creates the
    `auth.users` entry if it is the first login, and returns a full session
    identical in shape to `POST /auth/login`.

    **Prerequisites (one-time setup):**
    - Enable the Google provider in your Supabase Auth dashboard.
    - Add your `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in Supabase.
    """
    allowed_roles = {"customer", "vendor"}
    if payload.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {allowed_roles}",
        )

    # 1. Exchange Google ID token for a Supabase session
    try:
        auth_res = supabase.auth.sign_in_with_id_token({
            "provider": "google",
            "token": payload.id_token,
        })
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google Sign-In failed: {exc}",
        )

    if not auth_res.session or not auth_res.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google authentication did not return a valid session.",
        )

    user = auth_res.user
    user_id = user.id

    # 2. Check for an existing profile
    profile_res = (
        supabase_admin.table("profiles")
        .select("role, full_name, wallet_balance")
        .eq("id", user_id)
        .execute()
    )
    profile_data = profile_res.data[0] if profile_res.data else None
    is_new_user = profile_data is None

    # 3. Auto-create profile for first-time Google sign-ins
    if is_new_user:
        meta = user.user_metadata or {}
        full_name = (
            meta.get("full_name")
            or meta.get("name")
            or (user.email.split("@")[0] if user.email else "User")
        )
        avatar_url = meta.get("avatar_url") or meta.get("picture")

        new_profile: dict = {
            "id":             user_id,
            "email":          user.email or "",
            "full_name":      full_name,
            "role":           payload.role,
            "wallet_balance": 0,
        }
        if avatar_url:
            new_profile["avatar_url"] = avatar_url

        try:
            supabase_admin.table("profiles").insert(new_profile).execute()
            profile_data = {"role": payload.role, "full_name": full_name, "wallet_balance": 0}
        except Exception as exc:
            # Auth session is still valid — log and continue
            print(f"[WARN] Google profile auto-create failed for {user_id}: {exc}")
            profile_data = {"role": payload.role, "full_name": "", "wallet_balance": 0}
            full_name = ""

        # Send welcome notifications for new accounts (best-effort)
        try:
            if payload.role == "customer":
                send_welcome_customer.delay(user.email, profile_data["full_name"])
            elif payload.role == "vendor":
                send_welcome_vendor.delay(user.email, profile_data["full_name"])
                send_admin_new_vendor.delay(
                    ADMIN_EMAIL,
                    profile_data["full_name"],
                    user.email,
                    user_id,
                )
        except Exception:
            pass

    return {
        "access_token": auth_res.session.access_token,
        "token_type":   "bearer",
        "is_new_user":  is_new_user,
        "user": {
            "id":        user_id,
            "email":     user.email,
            "role":      profile_data.get("role"),
            "full_name": profile_data.get("full_name"),
        },
    }
