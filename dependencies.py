"""
dependencies.py — Backward-compatible re-export shim.

All auth dependencies now live in middleware.auth (single source of truth).
This module re-exports them so existing routers that import from 'dependencies'
(including wallet.py, which is intentionally left unchanged) continue to work
without any modifications.

Do NOT add new logic here — add it to middleware/auth.py instead.
"""

from middleware.auth import get_current_user, require_role, require_any_role
from database import supabase, supabase_admin

__all__ = ["get_current_user", "require_role", "require_any_role", "supabase", "supabase_admin"]
