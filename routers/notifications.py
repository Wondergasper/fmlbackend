"""
routers/notifications.py — In-App Notifications API

Endpoints:
  GET    /notifications/          — List user's notifications + unread count
  PATCH  /notifications/{id}/read — Mark single notification as read
  PATCH  /notifications/read-all  — Mark all user notifications as read
  DELETE /notifications/{id}      — Dismiss/delete a notification
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from dependencies import get_current_user
from database import supabase_admin, supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/")
async def list_notifications(
    unread_only: bool = Query(False, description="Filter only unread notifications"),
    limit: int = Query(50, ge=1, le=100, description="Number of notifications to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    user=Depends(get_current_user),
):
    """
    List notifications for the current authenticated user.
    Returns total unread count and paginated notification objects.
    """
    user_id = str(user.id)

    # 1. Fetch unread count
    unread_count = 0
    try:
        unread_res = (
            supabase_admin.table("notifications")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("is_read", False)
            .execute()
        )
        unread_count = unread_res.count if hasattr(unread_res, "count") and unread_res.count is not None else len(unread_res.data or [])
    except Exception as exc:
        logger.warning(f"[notifications] Failed to fetch unread count for {user_id}: {exc}")

    # 2. Query notifications list
    try:
        query = (
            supabase_admin.table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if unread_only:
            query = query.eq("is_read", False)

        res = query.execute()
        items = res.data or []
    except Exception as exc:
        logger.warning(f"[notifications] Failed to fetch notifications for {user_id}: {exc}")
        items = []

    return {
        "unread_count": unread_count,
        "total": len(items),
        "notifications": items,
    }


@router.patch("/read-all")
async def mark_all_notifications_read(
    user=Depends(get_current_user),
):
    """
    Mark all unread notifications for the current user as read.
    """
    user_id = str(user.id)
    try:
        supabase_admin.table("notifications").update({"is_read": True}).eq("user_id", user_id).eq("is_read", False).execute()
        return {"message": "All notifications marked as read."}
    except Exception as exc:
        logger.warning(f"[notifications] Error marking all read for {user_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark all notifications as read.",
        )


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    user=Depends(get_current_user),
):
    """
    Mark a single notification as read.
    """
    user_id = str(user.id)
    try:
        res = (
            supabase_admin.table("notifications")
            .update({"is_read": True})
            .eq("id", notification_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Notification not found or access denied.",
            )
        return {"message": "Notification marked as read.", "id": notification_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"[notifications] Error marking read for {notification_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update notification.",
        )


@router.delete("/{notification_id}")
async def dismiss_notification(
    notification_id: str,
    user=Depends(get_current_user),
):
    """
    Delete or dismiss a notification.
    """
    user_id = str(user.id)
    try:
        res = (
            supabase_admin.table("notifications")
            .delete()
            .eq("id", notification_id)
            .eq("user_id", user_id)
            .execute()
        )
        return {"message": "Notification dismissed.", "id": notification_id}
    except Exception as exc:
        logger.warning(f"[notifications] Error dismissing {notification_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to dismiss notification.",
        )
