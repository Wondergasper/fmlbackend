"""
websocket_manager.py — Centralized WebSocket Connection Manager for Farm-Connect
"""

import logging
import asyncio
import threading
from typing import Dict, Set, Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class OrderConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._async_lock: Optional[asyncio.Lock] = None
        self._thread_lock = threading.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            with self._thread_lock:
                if self._async_lock is None:
                    self._async_lock = asyncio.Lock()
        return self._async_lock

    async def connect(self, order_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            if order_id not in self.active_connections:
                self.active_connections[order_id] = set()
            self.active_connections[order_id].add(websocket)

    def disconnect(self, order_id: str, websocket: WebSocket):
        with self._thread_lock:
            if order_id in self.active_connections:
                self.active_connections[order_id].discard(websocket)
                if not self.active_connections[order_id]:
                    del self.active_connections[order_id]

    async def _send_local(self, order_id: str, message: dict):
        async with self.lock:
            if order_id not in self.active_connections:
                return
            active = list(self.active_connections[order_id])

        if not active:
            return

        results = await asyncio.gather(
            *[connection.send_json(message) for connection in active],
            return_exceptions=True
        )

        dead_sockets = set()
        for connection, result in zip(active, results):
            if isinstance(result, Exception):
                logger.warning(f"[ws] Error sending to order socket {order_id}: {result}")
                dead_sockets.add(connection)

        for ws in dead_sockets:
            self.disconnect(order_id, ws)

    async def broadcast_to_order(self, order_id: str, message: dict):
        await self._send_local(order_id, message)

    async def broadcast_order_status(self, order_id: str, status: str, note: str = None):
        payload = {
            "event": "order_status_update",
            "type": "order_status_update",
            "order_id": order_id,
            "status": status,
            "note": note
        }
        await self.broadcast_to_order(order_id, payload)

    async def broadcast_vendor_alert(self, order_id: str, vendor_id: str, message: str, extra_data: dict = None):
        payload = {
            "event": "vendor_order_alert",
            "type": "vendor_order_alert",
            "order_id": order_id,
            "vendor_id": vendor_id,
            "message": message
        }
        if extra_data:
            payload.update(extra_data)
        await self.broadcast_to_order(order_id, payload)
        await self._send_vendor_local(vendor_id, payload)

    async def connect_vendor(self, vendor_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            key = f"vendor:{vendor_id}"
            if key not in self.active_connections:
                self.active_connections[key] = set()
            self.active_connections[key].add(websocket)

    def disconnect_vendor(self, vendor_id: str, websocket: WebSocket):
        with self._thread_lock:
            key = f"vendor:{vendor_id}"
            if key in self.active_connections:
                self.active_connections[key].discard(websocket)
                if not self.active_connections[key]:
                    del self.active_connections[key]

    async def _send_vendor_local(self, vendor_id: str, message: dict):
        key = f"vendor:{vendor_id}"
        async with self.lock:
            if key not in self.active_connections:
                return
            active = list(self.active_connections[key])

        if not active:
            return

        results = await asyncio.gather(
            *[connection.send_json(message) for connection in active],
            return_exceptions=True
        )

        dead_sockets = set()
        for connection, result in zip(active, results):
            if isinstance(result, Exception):
                logger.warning(f"[ws] Error sending to vendor socket {vendor_id}: {result}")
                dead_sockets.add(connection)

        for ws in dead_sockets:
            self.disconnect_vendor(vendor_id, ws)

    async def connect_rider(self, rider_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            key = f"rider:{rider_id}"
            if key not in self.active_connections:
                self.active_connections[key] = set()
            self.active_connections[key].add(websocket)
            if "riders_pool" not in self.active_connections:
                self.active_connections["riders_pool"] = set()
            self.active_connections["riders_pool"].add(websocket)

    def disconnect_rider(self, rider_id: str, websocket: WebSocket):
        with self._thread_lock:
            key = f"rider:{rider_id}"
            if key in self.active_connections:
                self.active_connections[key].discard(websocket)
                if not self.active_connections[key]:
                    del self.active_connections[key]
            if "riders_pool" in self.active_connections:
                self.active_connections["riders_pool"].discard(websocket)
                if not self.active_connections["riders_pool"]:
                    del self.active_connections["riders_pool"]

    async def broadcast_to_riders(self, message: dict):
        """Broadcast message to all connected delivery riders."""
        async with self.lock:
            if "riders_pool" not in self.active_connections:
                return
            active = list(self.active_connections["riders_pool"])

        if not active:
            return

        results = await asyncio.gather(
            *[connection.send_json(message) for connection in active],
            return_exceptions=True
        )

        dead_sockets = set()
        for connection, result in zip(active, results):
            if isinstance(result, Exception):
                logger.warning(f"[ws] Error sending to rider socket: {result}")
                dead_sockets.add(connection)

        for ws in dead_sockets:
            with self._thread_lock:
                if "riders_pool" in self.active_connections:
                    self.active_connections["riders_pool"].discard(ws)

    async def broadcast_new_delivery_offer(self, offer_data: dict):
        """Alert all connected couriers of a new delivery offer."""
        payload = {
            "type": "NEW_OFFER",
            "event": "NEW_OFFER",
            "data": offer_data,
        }
        await self.broadcast_to_riders(payload)

    async def broadcast_task_assigned(self, task_data: dict):
        """Notify couriers and order subscribers when an offer is accepted/assigned."""
        payload = {
            "type": "TASK_ASSIGNED",
            "event": "TASK_ASSIGNED",
            "data": task_data,
        }
        order_id = task_data.get("order_id") or task_data.get("id")
        if order_id:
            await self.broadcast_to_order(order_id, payload)
        await self.broadcast_to_riders(payload)

    async def broadcast_order_updated(self, order_id: str, status: str, metadata: dict = None):
        """Broadcast order updates to order subscribers and couriers."""
        payload = {
            "type": "ORDER_UPDATED",
            "event": "ORDER_UPDATED",
            "order_id": order_id,
            "status": status,
            "data": metadata or {},
        }
        await self.broadcast_to_order(order_id, payload)
        await self.broadcast_to_riders(payload)

    async def start_redis_listener(self):
        """
        Listen for multi-instance pub/sub events via Redis if available.
        Subscribes to 'order_events' and pattern 'order:*'.
        """
        try:
            client = getattr(self, "redis", None)
            if client is None:
                try:
                    from services.rate_limiter import redis_client
                    client = redis_client
                except Exception:
                    client = None

            if client is None:
                logger.info("[ws] Redis client not initialized; skipping Redis pub/sub listener.")
                return

            pubsub = client.pubsub()
            try:
                await pubsub.subscribe("order_events")
                if hasattr(pubsub, "psubscribe"):
                    await pubsub.psubscribe("order:*")
            except Exception as sub_err:
                logger.warning(f"[ws] Failed to subscribe to Redis pubsub channels: {sub_err}")
                return

            logger.info("[ws] Subscribed to Redis channels ('order_events', 'order:*')")

            async for message in pubsub.listen():
                if message and message.get("type") in ("message", "pmessage"):
                    try:
                        raw_data = message.get("data")
                        if raw_data is None:
                            continue
                        import json
                        if isinstance(raw_data, (str, bytes)):
                            data = json.loads(raw_data)
                        elif isinstance(raw_data, dict):
                            data = raw_data
                        else:
                            continue

                        if isinstance(data, dict):
                            order_id = data.get("order_id")
                            payload = data.get("payload") if "payload" in data else data
                            if order_id and payload:
                                await self._send_local(order_id, payload)
                    except Exception as e:
                        logger.warning(f"[ws] Error processing redis pubsub message: {e}")
        except asyncio.CancelledError:
            logger.info("[ws] Redis listener task cancelled.")
            raise
        except Exception as exc:
            logger.warning(f"[ws] Redis pubsub listener stopped: {exc}")



connection_manager = OrderConnectionManager()
