"""
auth.py — Authentication routes for Farmers Market API

Endpoints:
  POST /auth/register  — Register a new user (customer or vendor)
  POST /auth/login     — Login and receive a JWT access token
  GET  /auth/me        — Get the current authenticated user's profile
  POST /auth/logout    — Invalidate the current session
"""

import os
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from dependencies import get_current_user
from database import supabase, supabase_admin
from services.email import (
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, background_tasks: BackgroundTasks):
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
        background_tasks.add_task(
            send_welcome_customer, payload.email, payload.full_name
        )
    elif payload.role == "vendor":
        background_tasks.add_task(
            send_welcome_vendor, payload.email, payload.full_name
        )
        background_tasks.add_task(
            send_admin_new_vendor,
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

    # Fetch role from the profiles table
    profile = (
        supabase.table("profiles")
        .select("role, full_name, wallet_balance")
        .eq("id", auth_res.user.id)
        .single()
        .execute()
    )

    return {
        "access_token": auth_res.session.access_token,
        "token_type": "bearer",
        "user": {
            "id": auth_res.user.id,
            "email": auth_res.user.email,
            "role": profile.data.get("role") if profile.data else None,
            "full_name": profile.data.get("full_name") if profile.data else None,
        }
    }


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.
    Requires a valid Bearer token in the Authorization header.
    """
    profile = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user.id)
        .single()
        .execute()
    )

    if not profile.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found."
        )

    return profile.data


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
