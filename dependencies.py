"""
dependencies.py — Backward-compatible re-export shim.

All auth dependencies live in middleware.auth (single source of truth).
This module re-exports them so existing routers that import from 'dependencies'
continue to work seamlessly.
"""

from middleware.auth import get_current_user, require_role, require_any_role, canonicalize_role
from schemas.auth import Role, ROLE_GROUP_DELIVERY, ROLE_GROUP_DISPATCH, ROLE_GROUP_LOGISTICS, ROLE_GROUP_MERCHANT
from database import supabase, supabase_admin

__all__ = [
    "get_current_user",
    "require_role",
    "require_any_role",
    "canonicalize_role",
    "Role",
    "ROLE_GROUP_DELIVERY",
    "ROLE_GROUP_DISPATCH",
    "ROLE_GROUP_LOGISTICS",
    "ROLE_GROUP_MERCHANT",
    "supabase",
    "supabase_admin",
]
