"""
services/notification_service.py — Unified in-app notification manager

Creates persistent notification rows in database and broadcasts real-time
notification events over WebSockets.
"""

import logging
from typing import Optional, Dict, Any
from database import supabase_admin
from services.websocket_manager import connection_manager

logger = logging.getLogger(__name__)


async def create_notification(
    user_id: str,
    title: str,
    message: str,
    type: str = "system",
    link: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Persist an in-app notification to the database and broadcast it
    immediately to the user's active WebSockets.
    """
    if not user_id or not title or not message:
        logger.warning("[notifications] Missing required fields for notification.")
        return None

    record = {
        "user_id": str(user_id),
        "title": title,
        "message": message,
        "type": type,
        "link": link,
        "is_read": False,
        "metadata": metadata or {},
    }

    try:
        res = supabase_admin.table("notifications").insert(record).execute()
        created = res.data[0] if (res and res.data) else record
    except Exception as exc:
        logger.warning(f"[notifications] Failed to insert notification in DB: {exc}")
        created = record

    # Broadcast in-app notification over WebSocket (non-blocking / best-effort)
    try:
        await connection_manager.broadcast_user_notification(str(user_id), created)
    except Exception as ws_err:
        logger.warning(f"[notifications] WebSocket push error for user {user_id}: {ws_err}")

    return created
