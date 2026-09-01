"""Opt-in MQTT export with Home Assistant discovery.

The gateway already owns the normalized, persisted mesh state.  This module is
an event sink for that state: it never opens a radio and it never reaches into
Home Assistant.  Operators point it at any MQTT broker, and retained discovery
documents make the telemetry appear in Home Assistant when discovery is
enabled there.

No broker, hostname, credentials, entity IDs, or node IDs are built in.  Position
export is deliberately disabled unless the operator explicitly opts in.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import ChatMessage, Node, Packet, SendReceipt
from .notifications import Notification
from .service import MeshService

log = logging.getLogger(__name__)


def _slug(value: str, fallback: str = "mesh") -> str:
    """Return a stable MQTT/Home Assistant identifier fragment."""
    clean = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return clean or fallback


def default_gateway_id() -> str:
    return _slug(socket.gethostname(), "gateway")


def _topic_root(value: str, label: str) -> str:
    value = value.strip().strip("/")
    if not value or any(char in value for char in ("#", "+", "\x00")):
        raise ValueError(f"{label} must be a non-empty MQTT topic without wildcards")
    return value


@dataclass(frozen=True)
class MQTTConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    ca_certs: str | None = None
    base_topic: str = "meshtui"
    discovery_prefix: str = "homeassistant"
    gateway_id: str = field(default_factory=default_gateway_id)
    include_position: bool = False
    publish_events: bool = False
    active_seconds: float = 15 * 60
    refresh_seconds: float = 60.0
    qos: int = 0
    # Channels MQTT clients may transmit to. Empty = inbound sends disabled;
    # this is a licensed radio, so nothing on the broker gets to key it up
    # unless the operator explicitly allowlists a channel.
    send_channels: tuple[str, ...] = ()
    send_min_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("MQTT host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("MQTT port must be between 1 and 65535")
        if self.qos not in (0, 1, 2):
            raise ValueError("MQTT QoS must be 0, 1, or 2")
        if self.username is not None and not self.username:
            raise ValueError("MQTT username must not be empty")
        if self.password is not None and self.username is None:
            raise ValueError("an MQTT password requires a username")
        if self.ca_certs and not self.tls:
            raise ValueError("an MQTT CA certificate requires TLS")
        if not math.isfinite(self.active_seconds) or self.active_seconds <= 0:
            raise ValueError("MQTT active window must be greater than zero")
        if not math.isfinite(self.refresh_seconds) or self.refresh_seconds <= 0:
            raise ValueError("MQTT refresh interval must be greater than zero")
        normalized = tuple(dict.fromkeys(
            str(name).lstrip("#").strip().casefold()
            for name in self.send_channels if str(name).strip().lstrip("#")))
        object.__setattr__(self, "send_channels", normalized)
        if not math.isfinite(self.send_min_seconds) or self.send_min_seconds < 0:
            raise ValueError("MQTT send interval must be zero or more seconds")
        object.__setattr__(self, "base_topic", _topic_root(self.base_topic, "MQTT base topic"))
        object.__setattr__(self, "discovery_prefix",
                           _topic_root(self.discovery_prefix, "discovery prefix"))
        object.__setattr__(self, "gateway_id", _slug(self.gateway_id, "gateway"))


@dataclass(frozen=True)
class _Metric:
    field: str
    name: str
    component: str = "sensor"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None


BASE_METRICS: tuple[_Metric, ...] = (
    _Metric("active", "Active", component="binary_sensor", device_class="connectivity"),
    _Metric("last_heard", "Last heard", device_class="timestamp"),
    _Metric("age_seconds", "Last heard age", unit="s", device_class="duration"),
    _Metric("snr", "SNR", unit="dB", state_class="measurement",
            icon="mdi:signal"),
    _Metric("rssi", "RSSI", unit="dBm", state_class="measurement",
            icon="mdi:signal"),
    _Metric("hops", "Hops", state_class="measurement", icon="mdi:transit-connection-variant"),
    _Metric("battery", "Battery", unit="%", device_class="battery",
            state_class="measurement"),
    _Metric("voltage", "Voltage", unit="V", device_class="voltage",
            state_class="measurement"),
    _Metric("channel_utilization", "Channel utilization", unit="%",
            state_class="measurement", icon="mdi:chart-donut"),
    _Metric("air_util_tx", "Transmit airtime", unit="%", state_class="measurement",
            icon="mdi:radio-tower"),
    _Metric("uptime_seconds", "Uptime", unit="s", device_class="duration",
            state_class="total_increasing"),
    _Metric("packets", "Packets heard", state_class="total_increasing",
            icon="mdi:counter"),
    _Metric("latitude", "Latitude", unit="°", state_class="measurement",
            icon="mdi:latitude"),
    _Metric("longitude", "Longitude", unit="°", state_class="measurement",
            icon="mdi:longitude"),
    _Metric("altitude", "Altitude", unit="m", device_class="distance",
            state_class="measurement"),
)


ENV_HINTS: dict[str, tuple[str | None, str | None]] = {
    "temperature": ("°C", "temperature"),
    "relative_humidity": ("%", "humidity"),
    "barometric_pressure": ("hPa", "atmospheric_pressure"),
    "gas_resistance": ("Ω", None),
    "iaq": (None, "aqi"),
    "distance": ("m", "distance"),
    "lux": ("lx", "illuminance"),
    "white_lux": ("lx", "illuminance"),
    "wind_speed": ("m/s", "wind_speed"),
    "wind_gust": ("m/s", "wind_speed"),
    "wind_direction": ("°", None),
    "weight": ("kg", "weight"),
}


def _snake(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return _slug(value)


def _iso_timestamp(value: float | None) -> str | None:
    if value is None or value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace(
            "+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _paho_client(client_id: str):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:  # pragma: no cover - exercised by CLI/runtime
        raise RuntimeError(
            "MQTT support is optional; install it with 'uv sync --extra mqtt' "
            "or 'pip install meshtui[mqtt]'"
        ) from exc
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):  # paho-mqtt 1.x compatibility
        return mqtt.Client(client_id=client_id)


class HomeAssistantMQTT:
    """Publish normalized mesh telemetry and retained HA discovery documents."""

    def __init__(self, service: MeshService, config: MQTTConfig, *,
                 client: Any | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.service = service
        self.config = config
        self.client = client or _paho_client(f"meshtui-{config.gateway_id}")
        self.clock = clock
        self._connected = False
        self._started = False
        self._stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._discovery_payloads: dict[str, str] = {}
        self._published_nodes: set[str] = set()
        self._last_error = ""
        self._sends_accepted = 0
        self._sends_dropped = 0
        self._last_send_ts = 0.0

    @property
    def availability_topic(self) -> str:
        return f"{self.config.base_topic}/{self.config.gateway_id}/availability"

    @property
    def gateway_state_topic(self) -> str:
        return f"{self.config.base_topic}/{self.config.gateway_id}/state"

    @property
    def send_topic(self) -> str:
        return f"{self.config.base_topic}/{self.config.gateway_id}/send"

    def status(self) -> dict[str, Any]:
        return {
            "kind": "mqtt_home_assistant",
            "connected": self._connected,
            "host": self.config.host,
            "port": self.config.port,
            "base_topic": self.config.base_topic,
            "gateway_id": self.config.gateway_id,
            "nodes_published": len(self._published_nodes),
            "position_enabled": self.config.include_position,
            "events_enabled": self.config.publish_events,
            "send_channels": list(self.config.send_channels),
            "sends_accepted": self._sends_accepted,
            "sends_dropped": self._sends_dropped,
            "error": self._last_error or None,
        }

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.will_set(self.availability_topic, "offline", qos=self.config.qos,
                             retain=True)
        if self.config.username is not None:
            self.client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            kwargs = {"ca_certs": self.config.ca_certs} if self.config.ca_certs else {}
            self.client.tls_set(**kwargs)
        if hasattr(self.client, "reconnect_delay_set"):
            self.client.reconnect_delay_set(min_delay=1, max_delay=120)
        self.client.connect_async(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="meshtui-mqtt-refresh", daemon=True)
        self._refresh_thread.start()

    def close(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)
            self._refresh_thread = None
        if self._connected:
            try:
                info = self.client.publish(self.availability_topic, "offline",
                                           qos=self.config.qos, retain=True)
                if hasattr(info, "wait_for_publish"):
                    info.wait_for_publish(timeout=1.0)
            except Exception:  # noqa: BLE001
                log.debug("could not publish MQTT shutdown state", exc_info=True)
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        self._connected = False
        self._started = False

    def _on_connect(self, client, userdata, flags, reason_code=0, properties=None) -> None:
        failed = bool(getattr(reason_code, "is_failure", False))
        try:
            failed = failed or int(reason_code) != 0
        except (TypeError, ValueError):
            pass
        if failed:
            self._connected = False
            self._last_error = f"broker rejected connection: {reason_code}"
            log.warning("MQTT connection failed: %s", reason_code)
            return
        self._connected = True
        self._last_error = ""
        self._discovery_payloads.clear()  # republish after a broker restart
        self._publish(self.availability_topic, "online", retain=True)
        if self.config.send_channels:
            client.on_message = self._on_message
            client.subscribe(self.send_topic, qos=self.config.qos)
        self._publish_gateway()
        with self.service.lock:
            nodes = list(self.service.state.nodes.values())
        for node in nodes:
            self.publish_node(node)

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False

    def _on_message(self, client, userdata, message) -> None:
        """An inbound send request: {"channel": "...", "text": "..."} or bare
        text (which goes to the first allowlisted channel).

        Everything here transmits on a real radio, so it is deliberately
        narrow: allowlisted channels only, one message per interval, payload
        truncated to what the protocol carries.
        """
        try:
            self.handle_send(bytes(message.payload).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a bad request must not kill paho's loop
            self._sends_dropped += 1
            log.warning("MQTT send request failed", exc_info=True)

    def handle_send(self, raw: str) -> bool:
        import json as _json
        channel_name = self.config.send_channels[0] if self.config.send_channels else None
        text = raw.strip()
        if text.startswith("{"):
            try:
                data = _json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                text = str(data.get("text") or data.get("message") or "").strip()
                requested = str(data.get("channel") or "").lstrip("#").strip().casefold()
                if requested:
                    channel_name = requested
        if not text or channel_name is None:
            self._sends_dropped += 1
            return False
        if channel_name not in self.config.send_channels:
            self._sends_dropped += 1
            log.warning("MQTT send to %r refused: not an allowlisted channel", channel_name)
            return False
        now = self.clock()
        if now - self._last_send_ts < self.config.send_min_seconds:
            self._sends_dropped += 1
            log.warning("MQTT send dropped: faster than one per %.0fs",
                        self.config.send_min_seconds)
            return False
        from .model import ChannelRef, payload_bytes
        from .radio import protocol_payload_limit
        with self.service.lock:
            state = self.service.state
            destination = None
            for position, item in enumerate(state.channels):
                if isinstance(item, tuple):
                    slot, name = int(item[0]), str(item[1])
                else:
                    slot, name = position, str(item)
                if name.lstrip("#").casefold() == channel_name:
                    destination = ChannelRef(state.protocol, slot, name)
                    break
            if destination is None:
                self._sends_dropped += 1
                log.warning("MQTT send refused: channel %r not on the radio", channel_name)
                return False
            limit = protocol_payload_limit(state.protocol)
            while payload_bytes(text) > limit:
                text = text[:-1]
            self._last_send_ts = now
            receipt = self.service.send_message(text, destination)
        self._sends_accepted += 1
        log.info("mqtt send: %s -> %s (%s)", text, destination.name, receipt.status.value)
        return True

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.config.refresh_seconds):
            if not self._connected:
                continue
            self._publish_gateway()
            with self.service.lock:
                nodes = list(self.service.state.nodes.values())
            for node in nodes:
                self.publish_node(node)

    def handle_event(self, kind: str, payload: Any) -> None:
        if not self._connected:
            return
        if self.config.publish_events:
            self._publish_event(kind, payload)
        if kind in ("connected", "lost", "mc_radio_stats", "status"):
            self._publish_gateway()
        if kind == "connected":
            # The service may just have switched observer scope and cleared
            # receiver-relative SNR/hop history. Replace every retained node
            # state immediately so MQTT never keeps the previous radio's view.
            with self.service.lock:
                nodes = list(self.service.state.nodes.values())
            for node in nodes:
                self.publish_node(node)
            return
        if kind in ("node", "mc_contact") and isinstance(payload, Node):
            self.publish_node(payload)
            return
        node_id: str | None = None
        if kind == "packet" and isinstance(payload, Packet):
            node_id = payload.from_id
        elif kind in ("mc_status", "mc_telemetry") and isinstance(payload, tuple):
            node_id = str(payload[0])
        if node_id:
            with self.service.lock:
                node = self.service.state.nodes.get(node_id)
            if node is not None:
                self.publish_node(node)

    def _publish_event(self, kind: str, payload: Any) -> None:
        """Publish an opt-in normalized event stream, never raw radio payloads."""
        topic_kind = None
        data: dict[str, Any] | None = None
        if kind == "packet" and isinstance(payload, Packet):
            topic_kind = "packet"
            data = {
                "timestamp": _iso_timestamp(payload.ts),
                "protocol": self.service.state.protocol,
                "from": payload.from_id,
                "to": payload.to_id,
                "port": payload.portnum,
                "channel": payload.channel,
                "summary": payload.summary,
                "snr": payload.snr,
                "rssi": payload.rssi,
                "hops": payload.hops,
                "packet_id": payload.packet_id,
                "encrypted": payload.encrypted,
                "via_mqtt": payload.via_mqtt,
            }
        elif kind == "chat" and isinstance(payload, ChatMessage):
            topic_kind = "message"
            data = {
                "timestamp": _iso_timestamp(payload.ts),
                "protocol": self.service.state.protocol,
                "from": payload.from_id,
                "from_name": payload.from_name,
                "to": payload.to_id,
                "channel": payload.channel,
                "text": payload.text,
                "outgoing": payload.outgoing,
                "message_id": payload.message_id,
                "delivery_status": payload.delivery_status,
                "repeats": len(payload.repeated_by),
                "path_hash_size": payload.path_hash_size,
                "route_mode": payload.route_mode,
            }
        elif kind == "receipt" and isinstance(payload, SendReceipt):
            topic_kind = "receipt"
            data = {
                "timestamp": _iso_timestamp(payload.updated_ts),
                "message_id": payload.message_id,
                "status": payload.status.value,
                "protocol_id": payload.protocol_id,
                "detail": payload.detail,
            }
        elif kind in ("connected", "lost"):
            topic_kind = "gateway"
            data = {
                "timestamp": _iso_timestamp(self.clock()),
                "event": kind,
                "connected": self.service.state.connected,
                "protocol": self.service.state.protocol,
                "node_id": self.service.state.my_node_id,
            }
        if topic_kind is not None and data is not None:
            topic = (f"{self.config.base_topic}/{self.config.gateway_id}/events/"
                     f"{topic_kind}")
            self._publish(topic, self._json(data), retain=False)

    def notify(self, notification: Notification) -> None:
        """Publish a non-retained event for Home Assistant MQTT automations."""
        if not self._connected:
            return
        topic = f"{self.config.base_topic}/{self.config.gateway_id}/events"
        data = {
            "event_type": notification.kind,
            "title": notification.title,
            "message": notification.message,
            "data": notification.data,
            "timestamp": _iso_timestamp(self.clock()),
        }
        self._publish(topic, self._json(data), retain=False)

    def _publish_gateway(self) -> None:
        now = self.clock()
        with self.service.lock:
            state = self.service.state
            active = sum(
                node.last_heard is not None and now - node.last_heard <= self.config.active_seconds
                for node in state.nodes.values()
            )
            data = {
                "connected": state.connected,
                "protocol": state.protocol,
                "node_id": state.my_node_id,
                "nodes_known": len(state.nodes),
                "nodes_active": active,
                "radio": dict(state.radio_info),
                "updated_at": _iso_timestamp(now),
            }
        self._publish(self.gateway_state_topic, self._json(data), retain=True)
        for metric in (
            _Metric("connected", "Radio connected", component="binary_sensor",
                    device_class="connectivity"),
            _Metric("nodes_known", "Nodes known", state_class="measurement"),
            _Metric("nodes_active", "Nodes active", state_class="measurement"),
        ):
            self._publish_discovery("gateway", "MeshTUI gateway", metric,
                                    self.gateway_state_topic, gateway=True)

    def publish_node(self, node: Node) -> None:
        if not self._connected:
            return
        state = self._node_state(node)
        node_key = _slug(node.node_id.removeprefix("!"), "unknown")
        state_topic = (f"{self.config.base_topic}/{self.config.gateway_id}/nodes/"
                       f"{node_key}/state")
        self._publish(state_topic, self._json(state), retain=True)
        self._published_nodes.add(node_key)

        metrics = list(BASE_METRICS)
        if not self.config.include_position:
            metrics = [metric for metric in metrics
                       if metric.field not in ("latitude", "longitude", "altitude")]
        for metric in metrics:
            if state.get(metric.field) is not None:
                self._publish_discovery(node_key, node.name, metric, state_topic, node=node)
        for field, value in state.items():
            if value is None or not field.startswith(("environment_", "mesh_")):
                continue
            label = field.removeprefix("environment_").removeprefix("mesh_")
            pretty = label.replace("_", " ").title()
            unit, device_class = ENV_HINTS.get(label, (None, None))
            prefix = "Environment" if field.startswith("environment_") else "Mesh"
            metric = _Metric(field, f"{prefix} {pretty}", unit=unit,
                             device_class=device_class, state_class="measurement")
            self._publish_discovery(node_key, node.name, metric, state_topic, node=node)

    def _node_state(self, node: Node) -> dict[str, Any]:
        now = self.clock()
        age = None if node.last_heard is None else max(0.0, now - node.last_heard)
        result: dict[str, Any] = {
            "node_id": node.node_id,
            "name": node.name,
            "short_name": node.short_name,
            "protocol": self.service.state.protocol,
            "active": bool(age is not None and age <= self.config.active_seconds),
            "last_heard": _iso_timestamp(node.last_heard),
            "age_seconds": None if age is None else round(age, 1),
            "snr": node.snr,
            "rssi": node.rssi,
            "hops": node.hops,
            "snr_history": list(node.snr_history),
            "battery": node.battery,
            "voltage": node.voltage,
            "channel_utilization": node.ch_util,
            "air_util_tx": node.air_util,
            "uptime_seconds": node.uptime,
            "packets": node.packets,
            "via_mqtt": node.via_mqtt,
            "updated_at": _iso_timestamp(now),
        }
        if self.config.include_position:
            result.update({"latitude": node.lat, "longitude": node.lon,
                           "altitude": node.alt})
        for key, value in node.env.items():
            result[f"environment_{_snake(key)}"] = value
        for key, value in node.local_stats.items():
            result[f"mesh_{_snake(key)}"] = value
        return result

    def _publish_discovery(self, object_key: str, display_name: str, metric: _Metric,
                           state_topic: str, *, node: Node | None = None,
                           gateway: bool = False) -> None:
        identity = f"{self.config.gateway_id}_{object_key}"
        entity_key = f"meshtui_{identity}_{metric.field}"
        component = metric.component
        topic = (f"{self.config.discovery_prefix}/{component}/{entity_key}/config")
        value_template = f"{{{{ value_json.{metric.field} }}}}"
        document: dict[str, Any] = {
            "name": metric.name,
            "unique_id": entity_key,
            "state_topic": state_topic,
            "value_template": value_template,
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [f"meshtui_{identity}"],
                "name": display_name,
                "manufacturer": "MeshTUI",
                "model": ("Gateway" if gateway else
                          ((node.hw_model or self.service.state.protocol or "Mesh node")
                           if node is not None else "Mesh node")),
            },
            "origin": {"name": "MeshTUI", "sw_version": "0.1.0",
                       "support_url": "https://github.com/jsaveker/meshtui"},
        }
        if component == "binary_sensor":
            document["value_template"] = (
                f"{{{{ 'ON' if value_json.{metric.field} else 'OFF' }}}}")
            document["payload_on"] = "ON"
            document["payload_off"] = "OFF"
        if metric.unit:
            document["unit_of_measurement"] = metric.unit
        if metric.device_class:
            document["device_class"] = metric.device_class
        if metric.state_class:
            document["state_class"] = metric.state_class
        if metric.icon:
            document["icon"] = metric.icon
        payload = self._json(document)
        if self._discovery_payloads.get(topic) == payload:
            return
        self._publish(topic, payload, retain=True)
        self._discovery_payloads[topic] = payload

    def _publish(self, topic: str, payload: str, *, retain: bool) -> None:
        try:
            info = self.client.publish(topic, payload, qos=self.config.qos, retain=retain)
            rc = getattr(info, "rc", 0)
            if rc not in (0, None):
                self._last_error = f"publish returned rc={rc}"
        except Exception as exc:  # noqa: BLE001 - event sink must not break the radio
            self._last_error = str(exc)
            log.warning("MQTT publish failed for %s: %s", topic, exc)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(_safe_json(value), separators=(",", ":"), sort_keys=True,
                          default=repr, allow_nan=False)


def _safe_json(value: Any) -> Any:
    """Replace radio-originated non-finite floats with JSON null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return value
