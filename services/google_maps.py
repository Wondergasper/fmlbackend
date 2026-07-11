"""
google_maps.py — Google Maps API and IP Geolocation helper service

Functions:
  geocode_address(address)         — Convert a text address → (lat, lng) via Google Geocoding API
  get_distance_and_eta(...)        — Driving distance + ETA via Google Distance Matrix API
  detect_location_by_ip(ip)        — Auto-detect approximate user location from their IP address
                                     (primary: ip-api.com free tier, no key required;
                                      fallback: returns None on error so callers degrade gracefully)

All network calls use httpx with a 5-second timeout so they never block the event loop for long.
Every function returns None / (None, None) on any failure — callers must handle gracefully.
"""

import logging
import httpx
from typing import Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = settings.google_maps_api_key
_TIMEOUT = 5.0  # seconds for all external HTTP calls


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Convert a human-readable address to (latitude, longitude) using the
    Google Geocoding API.

    Returns:
        (lat, lng) tuple on success, or None if the address cannot be resolved
        or if GOOGLE_MAPS_API_KEY is not configured.
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.warning("[maps] GOOGLE_MAPS_API_KEY not set — skipping geocoding.")
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_MAPS_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "OK" or not data.get("results"):
            logger.warning(f"[maps] Geocoding failed for '{address}': status={data.get('status')}")
            return None

        location = data["results"][0]["geometry"]["location"]
        return float(location["lat"]), float(location["lng"])

    except Exception as exc:
        logger.warning(f"[maps] Geocoding error for '{address}': {exc}")
        return None


# ---------------------------------------------------------------------------
# Distance Matrix / ETA
# ---------------------------------------------------------------------------

async def get_distance_and_eta(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> Tuple[Optional[str], Optional[int]]:
    """
    Calculate the driving distance and estimated travel time between two
    coordinate pairs using the Google Distance Matrix API.

    Returns:
        (distance_text, eta_minutes) e.g. ("4.2 km", 12)
        or (None, None) on failure.
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.warning("[maps] GOOGLE_MAPS_API_KEY not set — skipping distance calculation.")
        return None, None

    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": f"{dest_lat},{dest_lng}",
        "mode": "driving",
        "units": "metric",
        "key": GOOGLE_MAPS_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "OK":
            logger.warning(f"[maps] Distance Matrix failed: status={data.get('status')}")
            return None, None

        rows = data.get("rows", [])
        if not rows or not rows[0].get("elements"):
            return None, None

        element = rows[0]["elements"][0]
        if element.get("status") != "OK":
            return None, None

        distance_text = element["distance"]["text"]        # e.g. "4.2 km"
        duration_secs = element["duration"]["value"]       # seconds
        eta_minutes = max(1, round(duration_secs / 60))   # at least 1 minute

        return distance_text, eta_minutes

    except Exception as exc:
        logger.warning(f"[maps] Distance Matrix error: {exc}")
        return None, None


# ---------------------------------------------------------------------------
# Automatic IP-based Location Detection
# ---------------------------------------------------------------------------

async def detect_location_by_ip(ip_address: str) -> Optional[dict]:
    """
    Automatically resolve a user's approximate location from their IP address.

    Strategy:
        1. Primary:  ip-api.com  (free, no API key, up to 45 req/min)
        2. Fallback: Returns None so callers degrade gracefully.

    Returns a dict with keys:
        {
            "latitude": float,
            "longitude": float,
            "city": str,
            "region": str,
            "country": str,
            "country_code": str,
            "timezone": str,
            "isp": str,
        }
    or None if resolution fails (e.g. localhost / private IP / API down).
    """
    # Skip obviously private / loopback addresses — they cannot be geolocated
    _private_prefixes = ("127.", "10.", "192.168.", "::1", "localhost")
    if not ip_address or any(ip_address.startswith(p) for p in _private_prefixes):
        logger.debug(f"[maps] Skipping IP geolocation for private/loopback address: {ip_address}")
        return None

    # ── Primary: ip-api.com (free, JSON, no API key) ───────────────────────
    try:
        url = f"http://ip-api.com/json/{ip_address}"
        params = {
            "fields": "status,message,country,countryCode,region,regionName,city,"
                      "lat,lon,timezone,isp"
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "success":
            logger.warning(f"[maps] ip-api.com failed for {ip_address}: {data.get('message')}")
            return None

        return {
            "latitude":     float(data["lat"]),
            "longitude":    float(data["lon"]),
            "city":         data.get("city", ""),
            "region":       data.get("regionName", ""),
            "country":      data.get("country", ""),
            "country_code": data.get("countryCode", ""),
            "timezone":     data.get("timezone", ""),
            "isp":          data.get("isp", ""),
        }

    except Exception as exc:
        logger.warning(f"[maps] IP geolocation error for {ip_address}: {exc}")
        return None
