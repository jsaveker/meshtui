"""Scheduled sensor digests for a mesh channel - typically a private one.

Every N minutes, post one compact line summarising what the station already
knows: its own battery, repeater power reported over remote status, and any
environment telemetry heard on the mesh:

    [sensors] Base Station 4.10V 87% | Hilltop Relay 12.9V | Garden 25.9C 44%

Nothing is fetched from the internet and nothing is requested over RF - the
digest only repeats what arrived anyway, so the bot costs the mesh one
message per interval. The last post time is remembered in the store so a
gateway restart does not double-post.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .model import ChannelRef, payload_bytes
from .pathcalc import is_rebroadcaster
from .radio import protocol_payload_limit

log = logging.getLogger(__name__)

META_KEY = "sensorbot_last_post"

# Environment telemetry keys worth a LoRa byte, in display order.
ENV_KEYS = (
    ("temperature", "{:.1f}C"),
    ("relativeHumidity", "{:.0f}%"),
    ("barometricPressure", "{:.0f}hPa"),
    ("voltage", "{:.2f}V"),
)


class SensorBot:
    """Post a telemetry digest on an interval. Time-driven, not event-driven."""

    def __init__(self, service: Any, *, channel: str | int = "#sensors",
                 minutes: float = 60.0) -> None:
        self.service = service
        self.channel = channel
        self.interval = max(5.0, float(minutes)) * 60.0

    # ------------------------------------------------------------ schedule

    def start(self, stop: threading.Event) -> None:
        threading.Thread(target=self._loop, args=(stop,),
                         name="sensor-bot", daemon=True).start()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(60.0):
            try:
                if self.due(time.time()) and self.post_now():
                    self._remember(time.time())
            except Exception:  # noqa: BLE001 - telemetry must never hurt the mesh
                log.warning("sensor post failed", exc_info=True)

    def due(self, now: float) -> bool:
        last = self._last_post()
        return last is None or now - last >= self.interval

    def _last_post(self) -> float | None:
        store = self.service.store
        if store is None or not store.enabled:
            return None
        value = store.get_meta(META_KEY)
        try:
            return float(value) if value else None
        except (TypeError, ValueError):
            return None

    def _remember(self, ts: float) -> None:
        store = self.service.store
        if store is not None and store.enabled:
            store.set_meta(META_KEY, str(ts))

    # ------------------------------------------------------------- posting

    def compose(self) -> str | None:
        """One line of everything known, freshest first, or None if nothing is."""
        state = self.service.state
        entries: list[tuple[float, str]] = []
        for node in state.nodes.values():
            readings: list[str] = []
            if node.env:
                for key, fmt in ENV_KEYS:
                    value = node.env.get(key)
                    if value is not None:
                        readings.append(fmt.format(float(value)))
            elif node.voltage is not None or node.battery is not None:
                # Power telemetry only matters for infrastructure we watch
                # (and ourselves); listing every phone's battery is noise.
                if not (node.is_self or is_rebroadcaster(node)):
                    continue
                if node.voltage is not None:
                    readings.append(f"{node.voltage:.2f}V")
                if node.battery is not None:
                    readings.append(f"{node.battery}%")
            if readings:
                freshness = node.env_ts or node.last_heard or 0.0
                entries.append((freshness, f"{node.label} {' '.join(readings)}"))
        if not entries:
            return None
        entries.sort(key=lambda item: -item[0])
        limit = protocol_payload_limit(state.protocol)
        line = "[sensors]"
        for _, entry in entries:
            candidate = f"{line} {entry} |"
            if payload_bytes(candidate.rstrip(" |")) > limit:
                break
            line = candidate
        return line.rstrip(" |")

    def post_now(self) -> bool:
        """Compose and send one digest. Returns success."""
        text = self.compose()
        if text is None:
            log.info("sensor post skipped: no telemetry heard yet")
            return False
        destination = self._destination()
        if destination is None:
            log.warning("sensor post skipped: channel %r not found", self.channel)
            return False
        receipt = self.service.send_message(text, destination)
        log.info("sensorbot: %s -> %s (%s)", text, destination.name,
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
