"""
services/routing.py — Open-Source Routing, Geocoding & Distance Engine

Provides high-performance, cost-effective routing and geolocation alternatives:
1. OSRM (Open Source Routing Machine) — Sub-millisecond open-source driving distance & ETA
2. Nominatim (OpenStreetMap) — Free geocoding and reverse geocoding
3. Haversine Math Engine — Instant offline zero-latency distance & urban transit ETA
4. Smart Hybrid Fallbacks — Degrades gracefully without dropping API responses
"""

import math
import logging
import httpx
from typing import Optional, Tuple, Dict, Any

from config import settings
from services.google_maps import get_distance_and_eta as google_distance_and_eta, geocode_address as google_geocode

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0
_USER_AGENT = "FarmConnectLogistics/2.1 (contact@farmconnect.ng)"


# ---------------------------------------------------------------------------
# Mathematical Haversine & ETA Estimation (Zero Cost, Offline Capable)
# ---------------------------------------------------------------------------

def calculate_haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth in kilometers.
    """
    R = 6371.0  # Earth's radius in km
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 2)


def estimate_urban_eta_minutes(distance_km: float, average_speed_kmh: float = 30.0) -> int:
    """
    Estimate driving ETA in minutes assuming typical urban traffic speed (default 30 km/h).
    Minimum duration returned is 1 minute.
    """
    if distance_km <= 0.05:
        return 1
    # Adding a 20% road winding factor for urban street grids
    effective_road_distance = distance_km * 1.2
    hours = effective_road_distance / average_speed_kmh
    return max(1, round(hours * 60))


# ---------------------------------------------------------------------------
# Open Source Routing Machine (OSRM)
# ---------------------------------------------------------------------------

async def get_osrm_distance_and_eta(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> Tuple[Optional[str], Optional[int]]:
    """
    Calculate driving distance and ETA using an OSRM routing server.
    Note: OSRM expects coordinates formatted as {longitude},{latitude}.
    """
    base_url = (settings.osrm_url or "https://router.project-osrm.org").rstrip("/")
    url = f"{base_url}/route/v1/driving/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    params = {"overview": "false", "steps": "false"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            logger.warning(f"[osrm] Route query failed: code={data.get('code')}")
            return None, None

        primary_route = data["routes"][0]
        distance_meters = primary_route.get("distance", 0.0)
        duration_seconds = primary_route.get("duration", 0.0)

        distance_km = round(distance_meters / 1000.0, 1)
        distance_text = f"{distance_km} km"
        eta_minutes = max(1, round(duration_seconds / 60.0))

        return distance_text, eta_minutes

    except Exception as exc:
        logger.warning(f"[osrm] Routing error: {exc}")
        return None, None


# ---------------------------------------------------------------------------
# OpenStreetMap Nominatim Geocoding
# ---------------------------------------------------------------------------

async def nominatim_geocode(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocode an address string to (latitude, longitude) using OpenStreetMap Nominatim.
    """
    base_url = (settings.nominatim_url or "https://nominatim.openstreetmap.org").rstrip("/")
    url = f"{base_url}/search"
    params = {"q": address, "format": "json", "limit": 1}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()

        if not data or not isinstance(data, list):
            return None

        result = data[0]
        return float(result["lat"]), float(result["lon"])

    except Exception as exc:
        logger.warning(f"[nominatim] Geocoding error for '{address}': {exc}")
        return None


async def nominatim_reverse_geocode(latitude: float, longitude: float) -> Optional[str]:
    """
    Reverse geocode coordinates into a human-readable display address.
    """
    base_url = (settings.nominatim_url or "https://nominatim.openstreetmap.org").rstrip("/")
    url = f"{base_url}/reverse"
    params = {"lat": latitude, "lon": longitude, "format": "json"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()

        return data.get("display_name")

    except Exception as exc:
        logger.warning(f"[nominatim] Reverse geocoding error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Master Hybrid Distance & ETA Service
# ---------------------------------------------------------------------------

async def get_optimized_distance_and_eta(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    provider: Optional[str] = None,
) -> Tuple[str, int, str]:
    """
    Unified, fail-safe routing calculator.

    Order of resolution:
    1. Configured provider (OSRM or Google Maps).
    2. Alternate provider.
    3. Mathematical Haversine urban estimate.

    Guarantees returning (distance_text, eta_minutes, provider_used) without failure.
    """
    chosen_provider = provider or getattr(settings, "geo_provider", "osrm").lower()

    # 1. Try Primary: OSRM
    if chosen_provider == "osrm":
        dist_text, eta = await get_osrm_distance_and_eta(origin_lat, origin_lng, dest_lat, dest_lng)
        if dist_text and eta:
            return dist_text, eta, "osrm"

        # Fallback to Google Maps if key present
        if getattr(settings, "google_maps_api_key", ""):
            dist_text, eta = await google_distance_and_eta(origin_lat, origin_lng, dest_lat, dest_lng)
            if dist_text and eta:
                return dist_text, eta, "google_maps"

    # 2. Try Primary: Google Maps
    elif chosen_provider == "google":
        if getattr(settings, "google_maps_api_key", ""):
            dist_text, eta = await google_distance_and_eta(origin_lat, origin_lng, dest_lat, dest_lng)
            if dist_text and eta:
                return dist_text, eta, "google_maps"

        # Fallback to OSRM
        dist_text, eta = await get_osrm_distance_and_eta(origin_lat, origin_lng, dest_lat, dest_lng)
        if dist_text and eta:
            return dist_text, eta, "osrm"

    # 3. Fail-safe Math Fallback (Haversine + Urban Velocity Model)
    dist_km = calculate_haversine_distance_km(origin_lat, origin_lng, dest_lat, dest_lng)
    eta = estimate_urban_eta_minutes(dist_km)
    return f"{dist_km} km", eta, "haversine_estimate"


async def geocode_hybrid(address: str) -> Optional[Tuple[float, float]]:
    """
    Hybrid geocoding resolver: tries Nominatim (OSM), then Google Geocoding.
    """
    # 1. Try Nominatim (Free, Open-Source)
    coords = await nominatim_geocode(address)
    if coords:
        return coords

    # 2. Fallback to Google Geocoding if configured
    if getattr(settings, "google_maps_api_key", ""):
        return await google_geocode(address)

    return None


async def get_distance_and_eta(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> Tuple[Optional[str], Optional[int]]:
    """
    Drop-in replacement for Google get_distance_and_eta using open-source OSRM + fail-safe fallbacks.
    Returns (distance_text, eta_minutes) e.g. ("4.2 km", 12).
    """
    dist_text, eta, _ = await get_optimized_distance_and_eta(origin_lat, origin_lng, dest_lat, dest_lng)
    return dist_text, eta

