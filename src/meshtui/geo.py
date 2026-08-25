"""Great-circle helpers for the map and distance columns."""

from __future__ import annotations

import math

EARTH_R_KM = 6371.0088
KM_PER_DEG_LAT = 111.32


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(deg: float) -> str:
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((deg + 11.25) % 360 / 22.5)]


def fmt_km(km: float) -> str:
    if km < 1:
        return f"{km * 1000:.0f}m"
    if km < 10:
        return f"{km:.1f}km"
    return f"{km:.0f}km"


def km_offsets(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """East/north offset in km from a reference point (equirectangular).

    Accurate enough over mesh distances and far cheaper than a real projection.
    """
    east = (lon - ref_lon) * KM_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    north = (lat - ref_lat) * KM_PER_DEG_LAT
    return east, north
