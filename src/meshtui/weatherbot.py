"""Scheduled weather posts for a mesh channel.

Three times a day (configurable), fetch current conditions and today's
forecast from Open-Meteo - keyless and free for non-commercial use - for the
station's own advertised position, and post one compact line in the dialect
the channel's other weather bot speaks:

    [Field Site] 94F (feels 101F), Humidity 41%, Wind E 1mph,
    partly cloudy | Hi 99F Lo 75F, rain 20%

A posted slot is remembered in the store, so a gateway restart minutes after
a post does not repeat it, and a slot missed entirely (gateway down) is
skipped rather than posted stale.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

from .geo import compass
from .model import ChannelRef, payload_bytes
from .radio import protocol_payload_limit

log = logging.getLogger(__name__)

OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
    "weather_code,wind_speed_10m,wind_direction_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto&forecast_days=1"
)

# WMO weather interpretation codes, compressed to what fits a LoRa line.
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
       56: "frz drizzle", 57: "frz drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "frz rain", 67: "frz rain", 71: "light snow",
       73: "snow", 75: "heavy snow", 77: "snow", 80: "showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms"}

# A slot posts if the gateway notices within this window of its time; later
# than that means we were down, and stale weather is noise.
SLOT_WINDOW_MINUTES = 20
META_KEY = "weatherbot_last_slot"


def parse_times(spec: str) -> list[int]:
    """'07:00,12:00,18:00' -> minutes-of-day, invalid entries dropped."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours, _, minutes = part.partition(":")
            value = int(hours) * 60 + int(minutes or 0)
        except ValueError:
            continue
        if 0 <= value < 24 * 60:
            out.append(value)
    return sorted(set(out))


class WeatherBot:
    """Post the weather on a schedule. Time-driven, not event-driven."""

    def __init__(self, service: Any, *, channel: str | int = "#wx",
                 times: str = "07:00,12:00,18:00", location: str = "") -> None:
        self.service = service
        self.channel = channel
        self.times = parse_times(times) or [7 * 60, 12 * 60, 18 * 60]
        self.location = location.strip()

    # ------------------------------------------------------------ schedule

    def start(self, stop: threading.Event) -> None:
        threading.Thread(target=self._loop, args=(stop,),
                         name="weather-bot", daemon=True).start()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(60.0):
            try:
                slot = self.due_slot(time.localtime())
                if slot and not self._already_posted(slot):
                    if self.post_now():
                        self._remember(slot)
            except Exception:  # noqa: BLE001 - weather must never hurt the mesh
                log.warning("weather post failed", exc_info=True)

    def due_slot(self, now: time.struct_time) -> str | None:
        """The slot key ('YYYY-MM-DD/HH:MM') currently in its posting window."""
        minutes = now.tm_hour * 60 + now.tm_min
        for slot_minutes in self.times:
            if 0 <= minutes - slot_minutes < SLOT_WINDOW_MINUTES:
                stamp = f"{slot_minutes // 60:02d}:{slot_minutes % 60:02d}"
                return time.strftime("%Y-%m-%d", now) + "/" + stamp
        return None

    def _already_posted(self, slot: str) -> bool:
        store = self.service.store
        if store is None or not store.enabled:
            return False
        return store.get_meta(META_KEY) == slot

    def _remember(self, slot: str) -> None:
        store = self.service.store
        if store is not None and store.enabled:
            store.set_meta(META_KEY, slot)

    # ------------------------------------------------------------- posting

    def _position(self) -> tuple[float, float] | None:
        state = self.service.state
        me = state.nodes.get(state.my_node_id or "")
        if me is not None and me.has_position:
            return me.lat, me.lon
        return None

    def fetch(self, lat: float, lon: float) -> dict:
        request = urllib.request.Request(
            OPEN_METEO.format(lat=round(lat, 4), lon=round(lon, 4)),
            headers={"User-Agent": "meshtui-weatherbot"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def compose(self, data: dict) -> str:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        temp = round(current.get("temperature_2m", 0))
        feels = round(current.get("apparent_temperature", temp))
        parts = [f"{temp}F" + (f" (feels {feels}F)" if abs(feels - temp) >= 3 else "")]
        humidity = current.get("relative_humidity_2m")
        if humidity is not None:
            parts.append(f"Humidity {round(humidity)}%")
        wind = current.get("wind_speed_10m")
        if wind is not None:
            direction = compass(current.get("wind_direction_10m") or 0)
            parts.append(f"Wind {direction} {round(wind)}mph")
        condition = WMO.get(current.get("weather_code"))
        if condition:
            parts.append(condition)
        line = ", ".join(parts)
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_probability_max") or []
        forecast = []
        if highs and lows:
            forecast.append(f"Hi {round(highs[0])}F Lo {round(lows[0])}F")
        if rain and rain[0] is not None:
            forecast.append(f"rain {round(rain[0])}%")
        if forecast:
            line += " | " + ", ".join(forecast)
        label = self.location or self.service.state.my_node_name or "here"
        return f"[{label}] {line}"

    def post_now(self) -> bool:
        """Fetch, compose, and send one report. Returns success."""
        position = self._position()
        if position is None:
            log.warning("weather post skipped: station has no position")
            return False
        try:
            data = self.fetch(*position)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather fetch failed: %s", exc)
            return False
        state = self.service.state
        text = self.compose(data)
        destination = self._destination()
        if destination is None:
            log.warning("weather post skipped: channel %r not found", self.channel)
            return False
        limit = protocol_payload_limit(state.protocol)
        if payload_bytes(text) > limit:
            text = text[: limit - 1]
        receipt = self.service.send_message(text, destination)
        log.info("weatherbot: %s -> %s (%s)", text, destination.name,
                 receipt.status.value)
        return True

    def _destination(self) -> ChannelRef | None:
        state = self.service.state
        if isinstance(self.channel, int):
            return ChannelRef(state.protocol, self.channel,
                              state.channel_name(self.channel))
        wanted = str(self.channel).lstrip("#").casefold()
        for position, item in enumerate(state.channels):
            if isinstance(item, tuple):
                slot, name = int(item[0]), str(item[1])
            else:
                slot, name = position, str(item)
            if name.lstrip("#").casefold() == wanted:
                return ChannelRef(state.protocol, slot, name)
        return None
