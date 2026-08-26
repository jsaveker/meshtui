"""Transport layer.

Everything that talks to (or pretends to be) a radio lives here. The rest of
the app only ever sees normalized `Packet` / `Node` objects delivered through
one `emit(kind, payload)` callback.

The meshtastic library runs its own reader thread and fires callbacks from it,
so `emit` is always called off the UI thread. The app is responsible for
marshalling back (Textual's `call_from_thread`).
"""

from __future__ import annotations

import base64
import errno
import glob
import grp
import logging
import os
import pwd
import random
import threading
import time
from typing import Any, Callable

from .model import BROADCAST, DEFAULT_MAX_PAYLOAD, ChatMessage, Node, Packet, payload_bytes

log = logging.getLogger(__name__)

Emit = Callable[[str, Any], None]

# Event kinds pushed through emit():
#   "status"   -> str            connection/status line for the log
#   "error"    -> str            something went wrong
#   "connected"-> dict           {my_node_id, my_node_name, firmware, channels, device}
#   "lost"     -> str            connection dropped
#   "packet"   -> Packet
#   "node"     -> dict           raw NodeDB record for MeshState.upsert_node
#   "chat"     -> ChatMessage
#   "ack"      -> int            packet id that was acknowledged


def find_serial_ports() -> list[str]:
    """Best-effort list of candidate Meshtastic serial ports."""
    ports: list[str] = []
    try:
        from meshtastic.util import findPorts  # type: ignore

        ports = list(findPorts(True))
    except Exception:  # library missing or API drift - fall back to globbing
        log.debug("findPorts unavailable, globbing /dev", exc_info=True)
    if not ports:
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
            ports.extend(sorted(glob.glob(pattern)))
    # de-dup while preserving order
    seen: set[str] = set()
    return [p for p in ports if not (p in seen or seen.add(p))]


SERIAL_GROUPS = ("uucp", "dialout", "plugdev")


def find_wifi_nodes(timeout: float = 3.0) -> list[tuple[str, str, str]]:
    """Discover nodes advertising the Meshtastic TCP service over mDNS.

    The firmware registers `_meshtastic._tcp` with the node's short name and
    id in TXT records, so a radio on WiFi announces itself. Uses avahi-browse
    when present rather than taking on a zeroconf dependency; returns
    (host, address, label) tuples.
    """
    import shutil
    import subprocess

    if not shutil.which("avahi-browse"):
        return []
    try:
        out = subprocess.run(
            ["avahi-browse", "-rptk", "_meshtastic._tcp"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:  # noqa: BLE001 - discovery is best effort
        log.debug("avahi-browse failed", exc_info=True)
        return []

    found: dict[str, tuple[str, str, str]] = {}
    for line in out.splitlines():
        # =;iface;proto;name;type;domain;hostname;address;port;txt...
        parts = line.split(";")
        if not parts or parts[0] != "=" or len(parts) < 9:
            continue
        hostname, address, port = parts[6], parts[7], parts[8]
        txt = " ".join(parts[9:])
        label = parts[3] or hostname
        for key in ("shortname", "id"):
            marker = f"{key}="
            if marker in txt:
                value = txt.split(marker, 1)[1].split('"')[0].strip()
                if value:
                    label = f"{label} ({value})" if key == "id" else value
        found[address] = (hostname.rstrip("."), address,
                          f"{label}  port {port}")
    return list(found.values())

_MAX_PAYLOAD: int | None = None


def max_payload_bytes() -> int:
    """Largest data payload a single packet can carry, per the installed
    library's protobuf constant (DATA_PAYLOAD_LEN)."""
    global _MAX_PAYLOAD
    if _MAX_PAYLOAD is None:
        try:
            from meshtastic.protobuf import mesh_pb2  # type: ignore

            _MAX_PAYLOAD = int(mesh_pb2.Constants.DATA_PAYLOAD_LEN)
        except Exception:  # noqa: BLE001 - library missing or renamed
            _MAX_PAYLOAD = DEFAULT_MAX_PAYLOAD
    return _MAX_PAYLOAD


def permission_hint(port: str) -> str:
    """Explain an EACCES on `port` in terms of what the user must actually do.

    Being in /etc/group is not enough: supplementary groups are fixed at login,
    so a usermod does nothing for sessions that are already running.
    """
    try:
        owning_group = grp.getgrgid(os.stat(port).st_gid).gr_name
    except OSError:
        owning_group = next(iter(SERIAL_GROUPS))

    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        in_db = user in grp.getgrnam(owning_group).gr_mem
    except KeyError:
        in_db = False
    in_session = owning_group in {grp.getgrgid(g).gr_name for g in os.getgroups()}

    if in_session:
        return f"{port}: permission denied even though you are in '{owning_group}'."
    if in_db:
        return (
            f"{port} is owned by group '{owning_group}'. You ARE a member, but this "
            f"login session started before that was granted - group membership is "
            f"fixed at login. Log out and back in (or reboot), or start meshtui from "
            f"a shell you entered with:  newgrp {owning_group}"
        )
    return (
        f"{port} is owned by group '{owning_group}' and you are not a member. Run:  "
        f"sudo usermod -aG {owning_group} $USER   then log out and back in."
    )


def is_busy_error(exc: BaseException) -> bool:
    """The meshtastic library opens the port with exclusive=True, so a second
    instance fails with EAGAIN rather than anything self-explanatory."""
    if isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.EBUSY):
        return True
    return "exclusively lock" in str(exc).lower()


def is_permission_error(exc: BaseException) -> bool:
    """pyserial wraps EACCES in SerialException, so isinstance checks miss it."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.EACCES:
        return True
    return "permission denied" in str(exc).lower()


def _fmt_position(pos: dict[str, Any]) -> str:
    lat = pos.get("latitude")
    lon = pos.get("longitude")
    if lat is None and pos.get("latitudeI") is not None:
        lat = pos["latitudeI"] * 1e-7
    if lon is None and pos.get("longitudeI") is not None:
        lon = pos["longitudeI"] * 1e-7
    if lat is None or lon is None:
        return "position (no fix)"
    out = f"{lat:.5f}, {lon:.5f}"
    if pos.get("altitude") is not None:
        out += f"  {pos['altitude']}m"
    if pos.get("satsInView"):
        out += f"  {pos['satsInView']} sats"
    speed = pos.get("groundSpeed")
    if speed:
        track = pos.get("groundTrack")
        out += f"  {speed}m/s"
        if track is not None:
            # ground_track is scaled by 1e5 despite the protobuf comment.
            out += f" @{(track / 1e5) % 360:.0f}deg"
    return out


ENV_UNITS: list[tuple[str, str, str]] = [
    ("temperature", "{:.1f}", "C"),
    ("relativeHumidity", "{:.0f}", "%RH"),
    ("barometricPressure", "{:.0f}", "hPa"),
    ("lux", "{:.0f}", "lux"),
    ("iaq", "{:.0f}", "IAQ"),
    ("windSpeed", "{:.1f}", "m/s wind"),
    ("windDirection", "{:.0f}", "deg"),
    ("rainfall1H", "{:.1f}", "mm/h"),
    ("soilMoisture", "{:.0f}", "% soil"),
    ("radiation", "{:.2f}", "uSv/h"),
    ("distance", "{:.0f}", "mm"),
    ("weight", "{:.1f}", "kg"),
    ("current", "{:.0f}", "mA"),
]

AIR_UNITS: list[tuple[str, str, str]] = [
    ("pm25Standard", "{:.0f}", "PM2.5"),
    ("pm10Standard", "{:.0f}", "PM10"),
    ("pm100Standard", "{:.0f}", "PM100"),
    ("co2", "{:.0f}", "ppm CO2"),
]


def _fmt_telemetry(tel: dict[str, Any]) -> str:
    dm = tel.get("deviceMetrics") or {}
    env = tel.get("environmentMetrics") or {}
    air = tel.get("airQualityMetrics") or {}
    local = tel.get("localStats") or {}
    bits: list[str] = []
    if dm.get("batteryLevel") is not None:
        volts = f" {dm['voltage']:.2f}V" if dm.get("voltage") else ""
        bits.append(f"bat {dm['batteryLevel']}%{volts}")
    if dm.get("channelUtilization") is not None:
        bits.append(f"chUtil {dm['channelUtilization']:.1f}%")
    if dm.get("airUtilTx") is not None:
        bits.append(f"airTx {dm['airUtilTx']:.1f}%")
    for key, fmt, unit in ENV_UNITS:
        if env.get(key) is not None:
            bits.append(f"{fmt.format(env[key])}{unit}")
    for key, fmt, unit in AIR_UNITS:
        if air.get(key) is not None:
            bits.append(f"{fmt.format(air[key])}{unit}")
    if local:
        # A node reporting on the health of its own view of the mesh.
        tx, rx = local.get("numPacketsTx"), local.get("numPacketsRx")
        if tx is not None and rx is not None:
            bits.append(f"tx{tx}/rx{rx}")
        if local.get("numRxDupe") is not None:
            bits.append(f"dupe {local['numRxDupe']}")
        if local.get("noiseFloor") is not None:
            bits.append(f"noise {local['noiseFloor']:.0f}dBm")
        if local.get("numOnlineNodes") is not None:
            bits.append(f"{local['numOnlineNodes']}/{local.get('numTotalNodes', '?')} online")
    return "  ".join(bits) or "telemetry"


def _expand_payload(portnum: str, payload: bytes) -> dict[str, Any]:
    """Decode a Data payload into the dict shape the packet formatters expect."""
    from google.protobuf.json_format import MessageToDict
    from meshtastic.protobuf import mesh_pb2, telemetry_pb2

    if portnum == "POSITION_APP":
        return {"position": MessageToDict(mesh_pb2.Position.FromString(payload))}
    if portnum == "NODEINFO_APP":
        return {"user": MessageToDict(mesh_pb2.User.FromString(payload))}
    if portnum == "TELEMETRY_APP":
        return {"telemetry": MessageToDict(telemetry_pb2.Telemetry.FromString(payload))}
    return {}


def _try_published_decrypt(raw: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Open a packet our node has no key for, using only PUBLISHED keys.

    These keys ship in Meshtastic's own source and the protobuf documents them
    as "only minimally secure, because they are listed in this source code", so
    reading traffic that uses one is what every client already does. This never
    attempts to recover a key that is actually secret.

    Returns (decoded_dict, key_label), or None if no published key applies.
    """
    blob = raw.get("encrypted")
    if not blob:
        return None
    if isinstance(blob, str):
        try:
            blob = base64.b64decode(blob)
        except Exception:  # noqa: BLE001
            return None
    try:
        from . import crypto

        got = crypto.try_keys(raw.get("id"), raw.get("from"), bytes(blob))
    except Exception:  # noqa: BLE001 - never let this break the packet feed
        log.debug("published-key decrypt failed", exc_info=True)
        return None
    if got is None:
        return None

    payload = bytes(got.data.payload)
    decoded: dict[str, Any] = {"portnum": got.portnum, "payload": payload}
    if got.portnum == "TEXT_MESSAGE_APP":
        decoded["text"] = payload.decode("utf-8", errors="replace")
    else:
        try:
            decoded.update(_expand_payload(got.portnum, payload))
        except Exception:  # noqa: BLE001 - a summary is better than nothing
            pass
    return decoded, got.key_label


UNKNOWN_SNR = -128        # RouteDiscovery sentinel for "no measurement"
SNR_SCALE = 4.0           # SNR fields are carried in quarter-dB


def _snr_at(values: list[Any], index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    if value is None or value == UNKNOWN_SNR:
        return None
    return value / SNR_SCALE


def traceroute_hops(raw: dict[str, Any], decoded: dict[str, Any] | None = None
                    ) -> tuple[list[tuple[int, float | None]], list[tuple[int, float | None]]]:
    """Split a traceroute reply into (towards, back) lists of (node_num, snr).

    The reply travels from the traced node, so `to` is us and `from` is the
    node we asked about. Each SNR list carries one more entry than its route:
    the final endpoint reports the signal it received.
    """
    decoded = decoded if decoded is not None else (raw.get("decoded") or {})
    disc = decoded.get("traceroute") or {}
    route = list(disc.get("route") or [])
    snr_towards = list(disc.get("snrTowards") or [])
    route_back = list(disc.get("routeBack") or [])
    snr_back = list(disc.get("snrBack") or [])

    towards: list[tuple[int, float | None]] = []
    for i, num in enumerate(route):
        towards.append((num, _snr_at(snr_towards, i)))
    if raw.get("from") is not None:
        towards.append((raw["from"], _snr_at(snr_towards, len(route))))

    back: list[tuple[int, float | None]] = []
    if route_back or snr_back:
        for i, num in enumerate(route_back):
            back.append((num, _snr_at(snr_back, i)))
        if raw.get("to") is not None:
            back.append((raw["to"], _snr_at(snr_back, len(route_back))))
    return towards, back


def _fmt_traceroute(raw: dict[str, Any], decoded: dict[str, Any]) -> str:
    towards, back = traceroute_hops(raw, decoded)
    if not towards:
        return "traceroute (no route)"
    hops = len(towards) - 1
    parts = []
    for num, snr in towards:
        tag = f"!{num:08x}"
        parts.append(f"{tag}({snr:+.1f})" if snr is not None else tag)
    label = "direct" if hops == 0 else f"{hops} hop{'s' if hops != 1 else ''}"
    return f"{label}: " + " -> ".join(parts) + ("  +return" if back else "")


def flatten(raw: dict[str, Any]) -> Packet:
    """Turn a meshtastic packet dict into a display-ready Packet."""
    decoded = raw.get("decoded") or {}
    decrypted_with: str | None = None
    if not decoded and raw.get("encrypted"):
        opened = _try_published_decrypt(raw)
        if opened is not None:
            decoded, decrypted_with = opened
    portnum = decoded.get("portnum") or ("ENCRYPTED" if raw.get("encrypted") else "UNKNOWN")

    if portnum == "TEXT_MESSAGE_APP":
        summary = decoded.get("text") or _payload_text(decoded)
        summary = f'"{summary}"'
    elif portnum == "POSITION_APP":
        summary = _fmt_position(decoded.get("position") or {})
    elif portnum == "NODEINFO_APP":
        user = decoded.get("user") or {}
        name = user.get("longName", "?")
        short = user.get("shortName", "")
        hw = user.get("hwModel", "")
        summary = f"{name} ({short}) {hw}".strip()
    elif portnum == "TELEMETRY_APP":
        summary = _fmt_telemetry(decoded.get("telemetry") or {})
    elif portnum == "ROUTING_APP":
        routing = decoded.get("routing") or {}
        reason = routing.get("errorReason", "ACK")
        summary = f"reply to #{decoded.get('requestId', '?')}: {reason}"
    elif portnum == "TRACEROUTE_APP":
        summary = _fmt_traceroute(raw, decoded)
    elif portnum == "ENCRYPTED":
        summary = f"encrypted, {len(raw.get('encrypted') or b'')}B"
    else:
        payload = decoded.get("payload") or b""
        summary = f"{len(payload)}B payload"

    hops = None
    if raw.get("hopStart") is not None and raw.get("hopLimit") is not None:
        hops = raw["hopStart"] - raw["hopLimit"]
    elif raw.get("hopsAway") is not None:
        hops = raw["hopsAway"]

    return Packet(
        ts=float(raw.get("rxTime") or time.time()),
        from_id=raw.get("fromId") or _num_to_id(raw.get("from")),
        to_id=raw.get("toId") or _num_to_id(raw.get("to")),
        portnum=portnum,
        summary=summary,
        channel=raw.get("channel", 0) or 0,
        snr=raw.get("rxSnr"),
        rssi=raw.get("rxRssi"),
        hops=hops,
        packet_id=raw.get("id"),
        encrypted=portnum == "ENCRYPTED",
        relay_node=raw.get("relayNode"),
        via_mqtt=bool(raw.get("viaMqtt")),
        decrypted_with=decrypted_with,
        raw=raw,
    )


def _payload_text(decoded: dict[str, Any]) -> str:
    payload = decoded.get("payload") or b""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


def _num_to_id(num: Any) -> str:
    if num is None:
        return "?"
    if isinstance(num, str):
        return num
    if num == 0xFFFFFFFF:
        return BROADCAST
    return f"!{num:08x}"


class RadioLink:
    """Base interface the app talks to."""

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self.connected = False

    def start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def send_text(
        self, text: str, dest: str = BROADCAST, channel: int = 0
    ) -> tuple[bool, int | None]:
        """Returns (accepted, packet_id). A packet id of None is NOT failure -
        some transports do not report one - so success is reported separately."""
        raise NotImplementedError

    def request_traceroute(self, dest: str, hop_limit: int = 5) -> None:
        self.emit("error", "traceroute not supported by this link")


class MeshtasticLink(RadioLink):
    """Shared behaviour for any link the meshtastic library can drive.

    Serial and TCP differ only in how the connection is opened: everything
    above that - the pubsub wiring, packet handling, sending - is identical,
    because both interfaces derive from the library's MeshInterface.
    """

    def __init__(self, emit: Emit) -> None:
        super().__init__(emit)
        self.iface: Any = None
        self._pub: Any = None

    # Subclasses implement these two.
    def _describe(self) -> str:
        raise NotImplementedError

    def _open(self) -> Any:
        raise NotImplementedError

    def _explain(self, exc: Exception) -> str:
        return f"could not connect to {self._describe()}: {exc}"

    def start(self) -> None:
        from pubsub import pub

        self._pub = pub
        # pubsub keeps only weak references to listeners, so these must stay
        # bound to `self` and `self` must outlive the connection.
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")

        try:
            target = self._describe()
        except Exception as exc:  # noqa: BLE001 - nothing to connect to
            self.emit("error", str(exc))
            return
        self.emit("status", f"opening {target} ...")
        try:
            self.iface = self._open()
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            self.emit("error", self._explain(exc))


    def stop(self) -> None:
        if self._pub is not None:
            for fn, topic in (
                (self._on_receive, "meshtastic.receive"),
                (self._on_established, "meshtastic.connection.established"),
                (self._on_lost, "meshtastic.connection.lost"),
                (self._on_node_updated, "meshtastic.node.updated"),
            ):
                try:
                    self._pub.unsubscribe(fn, topic)
                except Exception:  # noqa: BLE001
                    pass
        if self.iface is not None:
            try:
                self.iface.close()
            except Exception:  # noqa: BLE001
                pass
            self.iface = None
        self.connected = False

    # ------------------------------------------------------------ callbacks

    def _on_receive(self, packet: dict[str, Any], interface: Any) -> None:
        try:
            pkt = flatten(packet)
        except Exception as exc:  # noqa: BLE001 - never let a bad packet kill the reader
            self.emit("error", f"bad packet: {exc}")
            return
        self.emit("packet", pkt)

        decoded = packet.get("decoded") or {}
        portnum = decoded.get("portnum")
        if portnum == "NODEINFO_APP" and decoded.get("user"):
            self.emit("node", {"num": packet.get("from"), "user": decoded["user"]})
        elif portnum == "TEXT_MESSAGE_APP":
            self.emit(
                "chat",
                ChatMessage(
                    ts=pkt.ts,
                    from_id=pkt.from_id,
                    from_name="",  # filled in by the app from the node table
                    to_id=pkt.to_id,
                    text=decoded.get("text") or _payload_text(decoded),
                    channel=pkt.channel,
                    packet_id=pkt.packet_id,
                ),
            )
        elif portnum == "ROUTING_APP":
            routing = decoded.get("routing") or {}
            if routing.get("errorReason") in (None, "NONE") and decoded.get("requestId"):
                self.emit("ack", decoded["requestId"])

    def _on_established(self, interface: Any, topic: Any = None) -> None:
        self.connected = True
        info: dict[str, Any] = {"device": self.port or ""}
        try:
            me = interface.getMyNodeInfo() or {}
            user = me.get("user") or {}
            info["my_node_id"] = user.get("id") or _num_to_id(me.get("num"))
            info["my_node_name"] = user.get("longName") or user.get("shortName") or ""
        except Exception:  # noqa: BLE001
            info["my_node_id"] = None
            info["my_node_name"] = ""
        try:
            meta = getattr(interface, "metadata", None)
            info["firmware"] = getattr(meta, "firmware_version", "") if meta else ""
        except Exception:  # noqa: BLE001
            info["firmware"] = ""
        info["channels"] = self._channel_names(interface)
        info["channel_security"] = self._channel_security(interface)
        self.emit("connected", info)

        # Seed the node table with whatever the device already knows.
        for record in (getattr(interface, "nodes", None) or {}).values():
            self.emit("node", record)

    def _on_lost(self, interface: Any, topic: Any = None) -> None:
        self.connected = False
        self.emit("lost", "connection to node lost")

    def _on_node_updated(self, node: dict[str, Any], interface: Any) -> None:
        self.emit("node", node)

    @staticmethod
    def _channel_names(interface: Any) -> list[str]:
        names: list[str] = []
        try:
            channels = getattr(interface.localNode, "channels", None) or []
            for idx, ch in enumerate(channels):
                role = getattr(ch, "role", None)
                # role 0 == DISABLED; stop listing once channels run out
                if role is not None and int(role) == 0:
                    continue
                name = getattr(getattr(ch, "settings", None), "name", "") or ""
                names.append(name or ("LongFast" if idx == 0 else f"ch{idx}"))
        except Exception:  # noqa: BLE001
            pass
        return names or ["LongFast"]

    @staticmethod
    def _channel_security(interface: Any) -> list[dict[str, Any]]:
        """Grade our OWN channels, where the PSK is genuinely ours to inspect."""
        from . import crypto

        out: list[dict[str, Any]] = []
        try:
            channels = getattr(interface.localNode, "channels", None) or []
            for idx, ch in enumerate(channels):
                role = getattr(ch, "role", None)
                if role is not None and int(role) == 0:
                    continue
                settings = getattr(ch, "settings", None)
                name = getattr(settings, "name", "") or ""
                psk = bytes(getattr(settings, "psk", b"") or b"")
                level, detail = crypto.classify_psk(psk)
                expanded = crypto.expand_psk(psk)
                out.append({
                    "index": idx,
                    "name": name or ("LongFast" if idx == 0 else f"ch{idx}"),
                    "level": level,
                    "detail": detail,
                    "hash": crypto.channel_hash(name, expanded) if expanded else None,
                })
        except Exception:  # noqa: BLE001
            log.debug("could not read channel security", exc_info=True)
        return out

    # ---------------------------------------------------------------- send

    def send_text(
        self, text: str, dest: str = BROADCAST, channel: int = 0
    ) -> tuple[bool, int | None]:
        if self.iface is None:
            self.emit("error", "not connected")
            return (False, None)
        try:
            sent = self.iface.sendText(
                text, destinationId=dest, wantAck=True, channelIndex=channel
            )
            return (True, getattr(sent, "id", None))
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"send failed: {exc}")
            return (False, None)

    def request_traceroute(self, dest: str, hop_limit: int = 5) -> None:
        """Send a traceroute without waiting for the reply.

        meshtastic's own sendTraceRoute() calls waitForTraceRoute(), which
        blocks for tens of seconds, and its response handler prints to stdout -
        both fatal in a TUI. The reply comes back as an ordinary
        TRACEROUTE_APP packet, so it is enough to send the request and let the
        normal receive path render it.
        """
        if self.iface is None:
            self.emit("error", "not connected")
            return
        try:
            from meshtastic.protobuf import mesh_pb2, portnums_pb2

            self.iface.sendData(
                mesh_pb2.RouteDiscovery(),
                destinationId=dest,
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
                hopLimit=hop_limit,
            )
            hops = "direct only" if hop_limit <= 1 else f"up to {hop_limit} hops"
            self.emit("status", f"traceroute sent to {dest} ({hops}); reply may take ~30s")
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"traceroute failed: {exc}")


class SerialLink(MeshtasticLink):
    """Talks to a node over USB serial."""

    def __init__(self, emit: Emit, port: str | None = None) -> None:
        super().__init__(emit)
        self.port = port

    def _describe(self) -> str:
        if self.port is None:
            candidates = find_serial_ports()
            if not candidates:
                raise RuntimeError(
                    "No serial ports found. Plug in a node, or check that you are "
                    "in the 'uucp' group (see README)."
                )
            self.port = candidates[0]
            if len(candidates) > 1:
                self.emit("status", f"{len(candidates)} ports found, using {self.port}")
        return self.port

    def _open(self) -> Any:
        import meshtastic.serial_interface

        port = self.port or ""
        # Check access up front so the common failure gets a useful message
        # rather than a wrapped errno from deep inside pyserial.
        if os.path.exists(port) and not os.access(port, os.R_OK | os.W_OK):
            raise PermissionError(port)
        return meshtastic.serial_interface.SerialInterface(devPath=port)

    def _explain(self, exc: Exception) -> str:
        port = self.port or "the serial port"
        if is_permission_error(exc):
            return permission_hint(port)
        if is_busy_error(exc):
            return (f"{port} is already in use - another meshtui, the meshtastic CLI, "
                    f"or a serial monitor still has it open. Only one process can talk "
                    f"to the radio at a time.")
        return f"could not open {port}: {exc}"


class TCPLink(MeshtasticLink):
    """Talks to a node over WiFi, using the same API the serial link uses.

    Meshtastic's TCP server speaks the identical protobuf stream, and the
    library's TCPInterface shares MeshInterface with SerialInterface, so
    everything above this class is unchanged.
    """

    DEFAULT_PORT = 4403

    def __init__(self, emit: Emit, host: str, port: int | None = None) -> None:
        super().__init__(emit)
        # Accept "host", "host:port" and bare IPv6 in brackets.
        self.host, self.port = self._split(host, port or self.DEFAULT_PORT)

    @staticmethod
    def _split(host: str, default_port: int) -> tuple[str, int]:
        host = host.strip()
        if host.startswith("[") and "]" in host:            # [::1]:4403
            addr, _, rest = host[1:].partition("]")
            if rest.startswith(":") and rest[1:].isdigit():
                return addr, int(rest[1:])
            return addr, default_port
        if host.count(":") == 1:
            addr, _, port = host.partition(":")
            if port.isdigit():
                return addr, int(port)
        return host, default_port

    def _describe(self) -> str:
        return f"{self.host}:{self.port}"

    def _open(self) -> Any:
        import meshtastic.tcp_interface

        return meshtastic.tcp_interface.TCPInterface(
            hostname=self.host, portNumber=self.port
        )

    def _explain(self, exc: Exception) -> str:
        import socket

        target = self._describe()
        if isinstance(exc, socket.gaierror):
            return (f"cannot resolve {self.host}. If you are using a .local name, "
                    f"mDNS must be working; otherwise use the radio's IP address.")
        if isinstance(exc, (ConnectionRefusedError,)):
            return (f"{target} refused the connection. The radio is reachable but its "
                    f"TCP server is not running - check that WiFi is enabled on it.")
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return (f"{target} did not respond. Check the radio is powered, on the "
                    f"same network, and that the address is right.")
        if isinstance(exc, OSError) and exc.errno == errno.EHOSTUNREACH:
            return f"{target} is unreachable from this machine."
        return f"could not connect to {target}: {exc}"

class DemoLink(RadioLink):
    """Synthetic mesh so the UI can be developed and demoed without hardware."""

    NAMES = [
        ("Basecamp Relay", "BASE", "TBEAM"),
        ("Ridgeline Solar", "RIDG", "HELTEC_V3"),
        ("Field Handheld", "FLD", "T_DECK"),
        ("Harbor Repeater", "HARB", "RAK4631"),
        ("Trailhead Sensor", "TRAIL", "HELTEC_V3"),
        ("Mobile Jeep", "JEEP", "TBEAM"),
        ("Weather Mast", "WXMT", "RAK4631"),
    ]
    CHATTER = [
        "radio check, anybody copy?",
        "got you 5 by 5",
        "heading up the ridge, back in an hour",
        "solar at 82%, holding fine overnight",
        "anyone seen the harbor node lately?",
        "rebooted the repeater, should be cleaner now",
        "wx says gusts to 30 tonight",
        "packet loss looks better after moving the antenna",
        "gonna try a traceroute",
        "copy that",
    ]

    def __init__(self, emit: Emit, seed: int = 7) -> None:
        super().__init__(emit)
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._nodes: list[dict[str, Any]] = []
        self._next_id = 0x7000
        self._timers: list[threading.Timer] = []

    def start(self) -> None:
        self._build_nodes()
        self.connected = True
        me = self._nodes[0]
        self.emit(
            "connected",
            {
                "device": "demo://synthetic-mesh",
                "my_node_id": me["user"]["id"],
                "my_node_name": me["user"]["longName"],
                "firmware": "2.5.0.demo",
                "channels": ["LongFast", "Ops", "Private"],
            },
        )
        for node in self._nodes:
            self.emit("node", node)
        self._thread = threading.Thread(target=self._run, name="demo-mesh", daemon=True)
        self._thread.start()
        self.emit("status", "demo mode - synthetic traffic, no radio attached")

    def stop(self) -> None:
        self._stop.set()
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.connected = False

    def _build_nodes(self) -> None:
        now = time.time()
        # Arbitrary placeholder origin for the synthetic mesh - not a real
        # location, and unrelated to whatever radio you actually own.
        base_lat, base_lon = 37.8044, -122.2712
        for idx, (long_name, short, hw) in enumerate(self.NAMES):
            num = 0x33000000 + idx * 0x1111
            self._nodes.append(
                {
                    "num": num,
                    "user": {
                        "id": f"!{num:08x}",
                        "longName": long_name,
                        "shortName": short,
                        "hwModel": hw,
                    },
                    "position": {
                        "latitude": base_lat + self._rng.uniform(-0.12, 0.12),
                        "longitude": base_lon + self._rng.uniform(-0.16, 0.16),
                        "altitude": self._rng.randint(5, 400),
                    },
                    "deviceMetrics": {
                        "batteryLevel": self._rng.randint(38, 100),
                        "voltage": round(self._rng.uniform(3.6, 4.2), 2),
                        "channelUtilization": round(self._rng.uniform(0.5, 12.0), 1),
                        "airUtilTx": round(self._rng.uniform(0.1, 4.0), 2),
                    },
                    "snr": round(self._rng.uniform(-14, 11), 2),
                    "hopsAway": 0 if idx == 0 else self._rng.randint(0, 3),
                    "lastHeard": now - self._rng.randint(0, 900),
                }
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._rng.uniform(0.4, 2.6))
            if self._stop.is_set():
                return
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.emit("error", f"demo error: {exc}")

    def _tick(self) -> None:
        node = self._rng.choice(self._nodes[1:])
        roll = self._rng.random()
        if roll < 0.22:
            self._emit_text(node)
        elif roll < 0.50:
            self._emit_position(node)
        elif roll < 0.78:
            self._emit_telemetry(node)
        elif roll < 0.90:
            self._emit_nodeinfo(node)
        else:
            self._emit_raw(node, "ROUTING_APP", {"routing": {"errorReason": "NONE"}, "requestId": 0})

    def _pid(self) -> int:
        self._next_id += self._rng.randint(1, 40)
        return self._next_id

    def _base(self, node: dict[str, Any], to: int = 0xFFFFFFFF) -> dict[str, Any]:
        hops = node.get("hopsAway") or 0
        return {
            "from": node["num"],
            "to": to,
            "fromId": node["user"]["id"],
            "toId": BROADCAST if to == 0xFFFFFFFF else f"!{to:08x}",
            "id": self._pid(),
            "rxTime": time.time(),
            "rxSnr": round(self._rng.uniform(-16, 12), 2),
            "rxRssi": self._rng.randint(-122, -55),
            "hopStart": 3,
            "hopLimit": 3 - hops,
            "channel": self._rng.choice([0, 0, 0, 1, 2]),
        }

    def _emit_raw(self, node: dict[str, Any], portnum: str, decoded: dict[str, Any]) -> None:
        raw = self._base(node)
        raw["decoded"] = {"portnum": portnum, **decoded}
        self.emit("packet", flatten(raw))

    def _emit_text(self, node: dict[str, Any]) -> None:
        raw = self._base(node)
        text = self._rng.choice(self.CHATTER)
        raw["decoded"] = {"portnum": "TEXT_MESSAGE_APP", "text": text}
        pkt = flatten(raw)
        self.emit("packet", pkt)
        self.emit(
            "chat",
            ChatMessage(
                ts=pkt.ts,
                from_id=pkt.from_id,
                from_name="",
                to_id=pkt.to_id,
                text=text,
                channel=pkt.channel,
                packet_id=pkt.packet_id,
            ),
        )

    def _emit_position(self, node: dict[str, Any]) -> None:
        pos = node["position"]
        pos["latitude"] += self._rng.uniform(-0.002, 0.002)
        pos["longitude"] += self._rng.uniform(-0.002, 0.002)
        self._emit_raw(node, "POSITION_APP", {"position": dict(pos)})
        self.emit("node", {"num": node["num"], "position": dict(pos)})

    def _emit_telemetry(self, node: dict[str, Any]) -> None:
        dm = node["deviceMetrics"]
        dm["batteryLevel"] = max(5, min(100, dm["batteryLevel"] + self._rng.randint(-2, 2)))
        dm["channelUtilization"] = round(
            max(0.1, dm["channelUtilization"] + self._rng.uniform(-1.5, 1.5)), 1
        )
        self._emit_raw(node, "TELEMETRY_APP", {"telemetry": {"deviceMetrics": dict(dm)}})
        self.emit("node", {"num": node["num"], "deviceMetrics": dict(dm)})

    def _emit_nodeinfo(self, node: dict[str, Any]) -> None:
        self._emit_raw(node, "NODEINFO_APP", {"user": dict(node["user"])})
        self.emit("node", {"num": node["num"], "user": dict(node["user"])})

    def send_text(
        self, text: str, dest: str = BROADCAST, channel: int = 0
    ) -> tuple[bool, int | None]:
        # Enforce the same limit as a real radio so demo mode cannot mislead.
        if payload_bytes(text) > max_payload_bytes():
            self.emit("error", "Data payload too big")
            return (False, None)
        pid = self._pid()
        # Fake an ack a moment later so the UI's pending/ack path gets exercised.
        timer = threading.Timer(self._rng.uniform(0.4, 1.2), lambda: self.emit("ack", pid))
        timer.daemon = True
        timer.start()
        self._timers = [t for t in self._timers if t.is_alive()] + [timer]
        return (True, pid)

    def request_traceroute(self, dest: str, hop_limit: int = 5) -> None:
        node = self._nodes[0]
        route = [n["num"] for n in self._rng.sample(self._nodes[1:], 2)]
        self._emit_raw(node, "TRACEROUTE_APP", {"traceroute": {"route": route}})
