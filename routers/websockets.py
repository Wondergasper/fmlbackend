"""
websockets.py — Real-Time WebSockets for Order Tracking & Notifications

Endpoints:
  WS /ws/orders/{order_id} — Live status broadcasts, location pings, and alerts.
"""

import json
import logging
import asyncio
import math
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from database import supabase, supabase_admin
from services.websocket_manager import connection_manager
from services.google_maps import get_distance_and_eta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["websockets"])


async def extract_token_from_websocket(websocket: WebSocket, token_query: str | None) -> str | None:
    """
    Extract token from query parameter or HTTP headers (Authorization, token, x-token, sec-websocket-protocol).
    """
    if token_query:
        return token_query

    auth_header = websocket.headers.get("authorization")
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return auth_header.strip()

    header_token = websocket.headers.get("token") or websocket.headers.get("x-token")
    if header_token:
        return header_token.strip()

    sec_proto = websocket.headers.get("sec-websocket-protocol")
    if sec_proto:
        parts = [p.strip() for p in sec_proto.split(",")]
        for part in parts:
            if part.lower() != "bearer":
                return part

    return None


async def authenticate_websocket(token: str | None) -> str:
    """
    Authenticate WebSocket connections using Supabase Auth JWT.
    """
    if not token:
        raise ValueError("Missing authentication token")

    try:
        user_res = await asyncio.to_thread(supabase.auth.get_user, token)
        if user_res and user_res.user:
            return user_res.user.id
        raise ValueError("Invalid token")
    except Exception as exc:
        logger.warning(f"[ws] Token verification failed: {exc}")
        raise ValueError("Authentication failed")


async def authorize_websocket_order(user_id: str, order_id: str) -> bool:
    """
    Verify if the user is authorized to connect to the order tracking stream.
    Authorized users:
    - Customer who placed the order (customer_id == user_id)
    - Vendor who has products in the order
    - Admin user (role == "admin")
    - Logistics / driver (role == "driver" or "courier")
    """
    try:
        profile_res = await asyncio.to_thread(
            lambda: supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        )
        role = profile_res.data.get("role", "customer") if profile_res.data else "customer"

        if role in ["admin", "driver", "courier"]:
            return True

        order_res = await asyncio.to_thread(
            lambda: supabase.table("orders")
            .select("customer_id, order_items(products(vendor_id))")
            .eq("id", order_id)
            .execute()
        )

        if not order_res.data:
            return False

        order_data = order_res.data[0]
        if order_data.get("customer_id") == user_id:
            return True

        if role == "vendor":
            items = order_data.get("order_items") or []
            for item in items:
                prod = item.get("products")
                if prod and prod.get("vendor_id") == user_id:
                    return True

        return False
    except Exception as exc:
        logger.warning(f"[ws] Order authorization query error: {exc}")
        return False


@router.websocket("/vendor/orders")
async def websocket_vendor_notifications(
    websocket: WebSocket,
    token: str = Query(None, description="Supabase access token for authentication")
):
    """
    WebSocket endpoint for vendor-specific notifications (new orders, alerts).
    Vendors connect once and receive real-time alerts for orders containing their products.
    """
    extracted_token = await extract_token_from_websocket(websocket, token)

    try:
        user_id = await authenticate_websocket(extracted_token)
    except ValueError as err:
        logger.warning(f"[ws] Vendor WS authentication failed: {err}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(err))
        return

    # Verify the caller is a vendor
    try:
        profile_res = await asyncio.to_thread(
            lambda: supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        )
        role = profile_res.data.get("role", "") if profile_res.data else ""
        if role != "vendor":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Only vendors can connect to this endpoint")
            return
    except Exception as exc:
        logger.warning(f"[ws] Vendor role check failed for {user_id}: {exc}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication failed")
        return

    await connection_manager.connect_vendor(user_id, websocket)

    try:
        await websocket.send_json({
            "event": "connected",
            "type": "connected",
            "vendor_id": user_id,
            "message": "Connected to vendor notification stream."
        })

        while True:
            data_text = await websocket.receive_text()
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "error": "Invalid JSON format",
                    "type": "error",
                    "message": "Invalid JSON format."
                })
                continue

            event_type = data.get("event") or data.get("type")

            if event_type in ["ping", "ping_pong"]:
                await websocket.send_json({"event": "pong", "type": "pong"})
            else:
                await websocket.send_json({
                    "error": "Unknown event type",
                    "type": "error",
                    "message": "Unknown event type."
                })

    except WebSocketDisconnect:
        logger.info(f"[ws] Vendor client disconnected cleanly for vendor {user_id}")
    except asyncio.CancelledError:
        logger.info(f"[ws] Vendor connection task cancelled for vendor {user_id}")
        raise
    except Exception as exc:
        logger.error(f"[ws] Unexpected vendor websocket error for {user_id}: {exc}")
    finally:
        connection_manager.disconnect_vendor(user_id, websocket)


@router.websocket("/orders/{order_id}")
async def websocket_order_tracking(
    websocket: WebSocket,
    order_id: str,
    token: str = Query(None, description="Supabase access token for authentication")
):
    """
    WebSocket endpoint for tracking real-time order status updates and delivery location pings.
    """
    extracted_token = await extract_token_from_websocket(websocket, token)

    try:
        user_id = await authenticate_websocket(extracted_token)
    except ValueError as err:
        logger.warning(f"[ws] Authentication failed for order {order_id}: {err}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(err))
        return

    is_authorized = await authorize_websocket_order(user_id, order_id)
    if not is_authorized:
        logger.warning(f"[ws] Unauthorized order access by user {user_id} for order {order_id}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized access to order")
        return

    await connection_manager.connect(order_id, websocket)

    try:
        await websocket.send_json({
            "event": "connected",
            "type": "connected",
            "order_id": order_id,
            "user_id": user_id,
            "message": "Connected to order tracking stream."
        })

        while True:
            data_text = await websocket.receive_text()
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "error": "Invalid JSON format",
                    "type": "error",
                    "message": "Invalid JSON format."
                })
                continue

            if not isinstance(data, dict):
                await websocket.send_json({
                    "error": "Payload must be a JSON object",
                    "type": "error",
                    "message": "Payload must be a JSON object."
                })
                continue

            event_type = data.get("event") or data.get("type")

            if event_type in ["order_status_update", "vendor_alert", "vendor_order_alert", "status_update", "order_status", "system_alert"]:
                await websocket.send_json({
                    "error": "Forbidden event type",
                    "type": "error",
                    "message": "Administrative events cannot be sent via client WebSocket."
                })
            elif event_type in ["delivery_location_ping", "location_ping"]:
                raw_lat = data.get("latitude")
                raw_lng = data.get("longitude")
                try:
                    if raw_lat is None or raw_lng is None:
                        raise ValueError("Missing coordinates")
                    lat = float(raw_lat)
                    lng = float(raw_lng)
                    if math.isnan(lat) or math.isinf(lat) or math.isnan(lng) or math.isinf(lng):
                        raise ValueError("Invalid coordinate value")
                    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                        raise ValueError("Coordinates out of bounds")
                except (ValueError, TypeError):
                    await websocket.send_json({
                        "error": "Invalid coordinates",
                        "type": "error",
                        "message": "Invalid coordinates"
                    })
                    continue

                payload = {
                    "event": "delivery_location_ping",
                    "type": "delivery_location_ping",
                    "order_id": order_id,
                    "latitude": lat,
                    "longitude": lng,
                    "speed": data.get("speed"),
                    "timestamp": data.get("timestamp"),
                    "sender_id": user_id
                }

                # Persist driver location to DB and calculate ETA (best-effort)
                distance_text = None
                eta_minutes = None
                try:
                    order_res = (
                        supabase_admin.table("orders")
                        .select("latitude, longitude")
                        .eq("id", order_id)
                        .execute()
                    )
                    if order_res.data:
                        order_row = order_res.data[0]
                        dest_lat = order_row.get("latitude")
                        dest_lng = order_row.get("longitude")

                        update_fields = {"driver_latitude": lat, "driver_longitude": lng}

                        if dest_lat is not None and dest_lng is not None:
                            distance_text, eta_minutes = await get_distance_and_eta(
                                lat, lng, float(dest_lat), float(dest_lng)
                            )
                            if distance_text:
                                update_fields["distance_text"] = distance_text
                            if eta_minutes:
                                update_fields["eta_minutes"] = eta_minutes

                        supabase_admin.table("orders").update(update_fields).eq("id", order_id).execute()
                except Exception as db_exc:
                    logger.warning(f"[ws] DB location persist error for order {order_id}: {db_exc}")

                # Enrich broadcast payload with ETA if available
                payload["distance_text"] = distance_text
                payload["eta_minutes"]   = eta_minutes

                await connection_manager.broadcast_to_order(order_id, payload)

            elif event_type in ["ping", "ping_pong"]:
                await websocket.send_json({"event": "pong", "type": "pong"})

            else:
                await websocket.send_json({
                    "error": "Unknown event type",
                    "type": "error",
                    "message": "Unknown event type."
                })

    except WebSocketDisconnect:
        logger.info(f"[ws] Client disconnected cleanly for order {order_id}")
    except asyncio.CancelledError:
        logger.info(f"[ws] Connection task cancelled for order {order_id}")
        raise
    except Exception as exc:
        logger.error(f"[ws] Unexpected websocket error for order {order_id}: {exc}")
    finally:
        connection_manager.disconnect(order_id, websocket)
