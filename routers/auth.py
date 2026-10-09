"""
auth.py — Authentication routes for Farmers Market API

Endpoints:
  POST /auth/register      — Register a new user (customer or vendor)
  POST /auth/login         — Login and receive a JWT access token
  GET  /auth/me            — Get the current authenticated user's profile
  POST /auth/logout        — Invalidate the current session
  POST /auth/send-otp      — Send a 4-digit OTP code to the user's email
  POST /auth/verify-otp    — Verify a 4-digit OTP code and mark email as verified
  POST /auth/forgot-password — Send password-reset OTP
  POST /auth/reset-password  — Reset password using OTP
"""

import os
import logging
import random
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
# Fix #1 — single source of truth: all routes use middleware.auth
from middleware.auth import get_current_user
from database import supabase, supabase_admin
from config import settings
from services import rate_limiter as _rate_limiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTP brute-force protection (Redis-backed; fails open if Redis unavailable)
# ---------------------------------------------------------------------------
OTP_MAX_ATTEMPTS = 5
OTP_ATTEMPT_WINDOW_SECONDS = 15 * 60  # 15 minutes


def _otp_redis_client():
    return _rate_limiter.redis_client


def _otp_attempt_key(email: str, purpose: str) -> str:
    return f"otp_attempts:{purpose}:{email}"


def _check_otp_attempts(email: str, purpose: str) -> None:
    """Raise 429 if this email has exceeded the OTP attempt limit."""
    client = _otp_redis_client()
    if client is None:
        return
    try:
        val = client.get(_otp_attempt_key(email, purpose))
        if val and int(val) >= OTP_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many incorrect attempts. Request a new code and try again later.",
            )
    except HTTPException:
        raise
    except Exception:
        pass


def _register_otp_failure(email: str, purpose: str) -> None:
    client = _otp_redis_client()
    if client is None:
        return
    try:
        key = _otp_attempt_key(email, purpose)
        client.incr(key)
        client.expire(key, OTP_ATTEMPT_WINDOW_SECONDS)
    except Exception:
        pass


def _clear_otp_attempts(email: str, purpose: str) -> None:
    client = _otp_redis_client()
    if client is None:
        return
    try:
        client.delete(_otp_attempt_key(email, purpose))
    except Exception:
        pass
from services.email import (
    send_otp_email,
    send_welcome_customer,
    send_welcome_vendor,
    send_admin_new_vendor,
    send_password_reset_email,
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
    # Fix #2 — optional role the caller expects; returned role must match
    expected_role: Optional[str] = None


class GoogleLoginRequest(BaseModel):
    id_token: str
    role: str = "customer"  # Used only when the account is brand-new


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest):
    """
    Register a new customer or vendor account.
    Creates the Supabase Auth user, then inserts a profile row.
    """
    allowed_roles = {"customer", "vendor", "logist", "rider", "courier", "driver"}
    if payload.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {allowed_roles}"
        )

    # Fix #6 — enforce minimum password length
    if len(payload.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters."
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

    # 1b. Auto-confirm the email so the user can log in immediately.
    #     Supabase blocks sign_in_with_password until email_confirmed_at is set.
    #     Since our email transport is not yet fully active, we confirm server-side.
    try:
        supabase_admin.auth.admin.update_user_by_id(
            user_id,
            {"email_confirm": True}
        )
    except Exception as e:
        logger.warning(f"[auth] Could not auto-confirm email for {user_id}: {e}")

    # 2. Insert a profile row via the admin client (bypasses RLS)
    # Database check constraint allows: 'customer', 'vendor', 'admin', 'driver', 'courier'
    db_role = "courier" if payload.role in ("rider", "courier", "driver", "logist") else payload.role
    profile_data = {
        "id": user_id,
        "email": payload.email,
        "full_name": payload.full_name,
        "role": db_role,
        "phone": payload.phone,
        "wallet_balance": 0,
        "status": "Pending Approval" if payload.role == "vendor" else "Active",
    }
    try:
        supabase_admin.table("profiles").insert(profile_data).execute()
    except Exception as e:
        logger.warning(f"[auth] Profile insert failed for {user_id}: {e}")

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

    Fix #2 — If `expected_role` is provided, the actual DB role must match.
    This prevents a customer from using the vendor/admin login pages to
    gain a session that the frontend would treat as a privileged role.
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

    # No session returned usually means the password was accepted but the
    # account's email is NOT confirmed in Supabase (it does not raise).
    # In development we auto-confirm so the user isn't blocked; in production
    # we surface a clear, actionable error instead of "Invalid credentials".
    if not auth_res.session:
        user_obj = getattr(auth_res, "user", None)
        unconfirmed = bool(user_obj) and not getattr(user_obj, "email_confirmed_at", None)

        if unconfirmed and settings.environment != "production":
            try:
                supabase_admin.auth.admin.update_user_by_id(
                    user_obj.id, {"email_confirm": True}
                )
                auth_res = supabase.auth.sign_in_with_password({
                    "email": payload.email,
                    "password": payload.password,
                })
            except Exception as exc:
                logger.warning(f"[auth] Dev auto-confirm + re-login failed for {payload.email}: {exc}")

        if not auth_res.session:
            if unconfirmed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Email not verified. Check your inbox for the verification code, "
                           "or use 'Forgot password' to verify your account.",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials."
            )

    # Fetch role from the profiles table (handle missing profile gracefully)
    profile_res = (
        supabase_admin.table("profiles")
        .select("role, full_name, wallet_balance, status")
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
                "status": "Active",
                "wallet_balance": 0,
            }).execute()
            profile_data = {"role": "customer", "full_name": auth_res.user.email.split("@")[0], "status": "Active"}
        except Exception as exc:
            print(f"[WARN] Auto-profile creation failed for {auth_res.user.id}: {exc}")

    actual_role = profile_data.get("role") if profile_data else None

    # Fix #2 — enforce role match when the caller specifies an expected role
    if payload.expected_role:
        expected = payload.expected_role
        courier_roles = {"rider", "courier", "driver", "logist"}
        roles_match = (actual_role == expected) or (expected in courier_roles and actual_role in courier_roles)
        if not roles_match:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. This account does not have the '{payload.expected_role}' role."
            )

    return {
        "access_token": auth_res.session.access_token,
        "refresh_token": auth_res.session.refresh_token,
        "token_type": "bearer",
        "user": {
            "id": auth_res.user.id,
            "email": auth_res.user.email,
            "role": actual_role,
            "full_name": profile_data.get("full_name") if profile_data else None,
            "status": profile_data.get("status") if profile_data else None,
        }
    }


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.
    Requires a valid Bearer token in the Authorization header.
    """
    profile_res = (
        supabase_admin.table("profiles")
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


class ProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    delivery_address: Optional[str] = None


@router.patch("/profile", status_code=status.HTTP_200_OK)
async def update_profile(
    payload: ProfileUpdateRequest,
    user=Depends(get_current_user),
):
    """
    Update the authenticated user's own profile details.
    """
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided to update.",
        )

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = supabase_admin.table("profiles").update(update_data).eq("id", user.id).execute()
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found.",
        )

    return {"message": "Profile updated successfully.", "data": res.data[0]}


# ---------------------------------------------------------------------------
# OTP / Password-Reset Models
# ---------------------------------------------------------------------------

class SendOtpRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp_code: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp_code: str
    new_password: str


# ---------------------------------------------------------------------------
# OTP Endpoints
# ---------------------------------------------------------------------------

@router.post("/send-otp", status_code=status.HTTP_200_OK)
async def send_otp(payload: SendOtpRequest):
    """
    Generate a 4-digit OTP for email verification, store it on the user's
    profile, and send it via email. OTP expires after 10 minutes.

    Fix #7 — stores otp_purpose="email_verification" so that
    password-reset codes cannot be used here and vice versa.
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

    otp_code = f"{random.randint(0, 999999):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    supabase_admin.table("profiles").update({
        "otp_code": otp_code,
        "otp_expires_at": expires_at.isoformat(),
        "otp_purpose": "email_verification",  # Fix #7
    }).eq("id", profile["id"]).execute()

    # Best-effort email (fails open — see email.py). When no email domain is
    # verified (dev), the OTP is also returned directly so the caller can
    # proceed without relying on delivery.
    send_otp_email.delay(payload.email, profile["full_name"], otp_code)

    result = {"message": "OTP generated.", "email": payload.email}
    if settings.environment != "production":
        result["otp_code"] = otp_code
        result["note"] = "Email delivery requires a verified domain; use otp_code above for dev."
    return result


@router.post("/verify-otp", status_code=status.HTTP_200_OK)
async def verify_otp(payload: VerifyOtpRequest):
    """
    Verify a 4-digit OTP code. Marks the email as verified on success.

    Fix #7 — rejects codes that were issued for password-reset, not
    email-verification, preventing cross-flow OTP reuse.
    """
    profile_res = (
        supabase_admin.table("profiles")
        .select("id, full_name, otp_code, otp_expires_at, otp_purpose, email_verified")
        .eq("email", payload.email)
        .execute()
    )
    profile = profile_res.data[0] if profile_res.data else None
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email."
        )

    _check_otp_attempts(payload.email, "email_verification")

    if profile.get("email_verified"):
        return {"message": "Email already verified.", "email": payload.email, "verified": True}

    stored_code = profile.get("otp_code")
    if not stored_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OTP has been sent. Please request a new code."
        )

    # Fix #7 — reject if this OTP was issued for a different purpose
    otp_purpose = profile.get("otp_purpose")
    if otp_purpose and otp_purpose != "email_verification":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This code cannot be used for email verification. Please request a new code."
        )

    if stored_code != payload.otp_code:
        _register_otp_failure(payload.email, "email_verification")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect verification code."
        )

    expires_at = profile.get("otp_expires_at")
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            _register_otp_failure(payload.email, "email_verification")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OTP has expired. Please request a new code."
            )

    supabase_admin.table("profiles").update({
        "email_verified": True,
        "otp_code": None,
        "otp_expires_at": None,
        "otp_purpose": None,
    }).eq("id", profile["id"]).execute()

    _clear_otp_attempts(payload.email, "email_verification")

    # Align Supabase Auth's email confirmation with our app flag so that login
    # (which gates on Supabase email_confirmed_at) works in production too.
    try:
        supabase_admin.auth.admin.update_user_by_id(
            profile["id"], {"email_confirm": True}
        )
    except Exception as exc:
        logger.warning(f"[auth] Could not confirm Supabase email for {profile['id']}: {exc}")

    return {"message": "Email verified successfully.", "email": payload.email, "verified": True}


# ---------------------------------------------------------------------------
# Password Reset Endpoints
# ---------------------------------------------------------------------------

@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Initiate a password reset by sending a 6-digit OTP to the user's email.

    Always returns 200 regardless of whether the email is registered,
    to prevent email enumeration.
    """
    profile_res = (
        supabase_admin.table("profiles")
        .select("id, full_name")
        .eq("email", payload.email)
        .execute()
    )
    profile = profile_res.data[0] if profile_res.data else None

    if profile:
        otp_code = f"{random.randint(0, 999999):06d}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

        supabase_admin.table("profiles").update({
            "otp_code": otp_code,
            "otp_expires_at": expires_at.isoformat(),
            "otp_purpose": "password_reset",  # Fix #7
        }).eq("id", profile["id"]).execute()

        # Dispatch password reset email via Celery
        try:
            from services.email import send_password_reset_email
            send_password_reset_email.delay(
                payload.email,
                profile.get("full_name") or "User",
                otp_code,
            )
        except Exception as exc:
            logger.warning(f"[auth] Failed to queue password reset email: {exc}")

    # Always return the same response to avoid email enumeration
    return {"message": "If an account exists with that email, a reset code has been sent."}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(payload: ResetPasswordRequest):
    """
    Reset the user's password using a valid OTP.

    Validates the 6-digit OTP, then updates the password via the Supabase
    admin client (bypasses the need for the user's current password).
    """
    profile_res = (
        supabase_admin.table("profiles")
        .select("id, otp_code, otp_expires_at, otp_purpose")
        .eq("email", payload.email)
        .execute()
    )
    profile = profile_res.data[0] if profile_res.data else None

    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with that email."
        )

    _check_otp_attempts(payload.email, "password_reset")

    stored_code = profile.get("otp_code")
    if not stored_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No reset code has been requested. Please start the forgot-password flow."
        )

    # Fix #7 — reject if this OTP was issued for email-verification, not reset
    otp_purpose = profile.get("otp_purpose")
    if otp_purpose and otp_purpose != "password_reset":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This code cannot be used for a password reset. Please request a new reset code."
        )

    if stored_code != payload.otp_code:
        _register_otp_failure(payload.email, "password_reset")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect reset code."
        )

    expires_at = profile.get("otp_expires_at")
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            _register_otp_failure(payload.email, "password_reset")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset code has expired. Please request a new one."
            )

    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters."
        )

    # Update password via Supabase admin API
    try:
        supabase_admin.auth.admin.update_user_by_id(
            profile["id"],
            {"password": payload.new_password},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update password: {str(e)}"
        )

    # Invalidate the OTP after successful reset
    supabase_admin.table("profiles").update({
        "otp_code": None,
        "otp_expires_at": None,
        "otp_purpose": None,
    }).eq("id", profile["id"]).execute()

    _clear_otp_attempts(payload.email, "password_reset")

    return {"message": "Password updated successfully. You can now log in with your new password."}


@router.post("/refresh", tags=["auth"])
async def refresh_token(payload: RefreshRequest):
    """
    Exchange a Supabase refresh_token for a new session (access + refresh).
    Keeps the user logged in past the ~1h access-token expiry without
    forcing a full re-login.
    """
    try:
        auth_res = supabase.auth.refresh_session(payload.refresh_token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Session refresh failed: {str(e)}"
        )
    if not auth_res.session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session."
        )
    return {
        "access_token": auth_res.session.access_token,
        "refresh_token": auth_res.session.refresh_token,
        "token_type": "bearer",
    }


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    """
    Sign the current user out. Revokes all of the user's Supabase sessions so
    the stateless JWT cannot be reused after logout (otherwise it stays valid
    until it expires, ~1h).
    """
    try:
        supabase_admin.auth.admin.sign_out(user.id)
    except Exception as exc:
        logger.warning(f"[auth] logout revocation failed for {user.id}: {exc}")
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
    allowed_roles = {"customer", "vendor", "logist"}
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
