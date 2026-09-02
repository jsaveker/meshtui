"""Replay SQLite or packet-capture windows as a read-only ghost gateway."""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any

from .gateway import Gateway
from .meshcore_link import contact_to_node, key_to_id, last_path_hash
from .model import (ChatMessage, DeliveryStatus, DestinationRef, Packet,
                    SendReceipt)
from .radio import RadioLink
from .radio import flatten
from .service import MeshService
from .store import LAST_OBSERVER, Store


def default_replay_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / f"meshtui-{os.getuid()}-ghost.sock"


def packet_from_row(row: dict[str, Any]) -> Packet:
    try:
        raw = json.loads(row.get("raw") or "{}")
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    portnum = row.get("portnum") or "UNKNOWN"
    relay_hash = row.get("relay_hash")
    if not relay_hash and portnum == "RXLOG_APP":
        relay_hash = last_path_hash(
            raw.get("path"), raw.get("path_len"), raw.get("path_hash_size"))
    return Packet(
        ts=float(row.get("ts") or 0.0), from_id=row.get("from_id") or "?",
        to_id=row.get("to_id") or "?", portnum=portnum,
        summary=row.get("summary") or "", channel=int(row.get("channel") or 0),
        snr=row.get("snr"), rssi=row.get("rssi"), hops=row.get("hops"),
        packet_id=row.get("packet_id"), encrypted=portnum == "ENCRYPTED",
        relay_node=raw.get("relayNode"), via_mqtt=bool(raw.get("viaMqtt")), raw=raw,
        relay_hash=relay_hash)


class ReplayLink(RadioLink):
    """A timeline-backed RadioLink that never permits transmissions."""

    def __init__(self, emit, packets: list[Packet], messages: list[ChatMessage],
                 nodes: list[dict[str, Any]], *, protocol: str = "meshtastic",
                 observer: str | None = None, speed: float = 1.0,
                 loop: bool = False, source: str = "sqlite") -> None:
        super().__init__(emit)
        if speed <= 0:
            raise ValueError("replay speed must be greater than zero")
        self.packets = packets
        self.messages = messages
        self.nodes = nodes
        self.protocol = protocol
        self.observer = observer or "!00000000"
        self.speed = speed
        self.loop = loop
        self.source = source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.connected = True
        self.emit("connected", {
            "device": f"replay://{self.source}", "my_node_id": self.observer,
            "my_node_name": "Ghost mesh", "firmware": "replay",
            "protocol": self.protocol, "channels": [(0, "Replay")],
        })
        for record in self.nodes:
            copied = dict(record)
            copied["lastHeard"] = time.time()
            self.emit("node", copied)
        self._thread = threading.Thread(target=self._run, name="mesh-replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.connected = False

    def send(self, text: str, destination: DestinationRef,
             message_id: str) -> SendReceipt:
        return SendReceipt(message_id, destination, DeliveryStatus.FAILED,
                           detail="ghost replay is read-only")

    def send_text(self, text: str, dest: str = "^all",
                  channel: int = 0) -> tuple[bool, int | None]:
        return False, None

    def _run(self) -> None:
        source = [(packet.ts, "packet", packet) for packet in self.packets]
        source += [(message.ts, "chat", message) for message in self.messages]
        source.sort(key=lambda item: (item[0], item[1]))
        if not source:
            self.emit("status", "ghost replay contains no events")
            self._stop.wait()
            return
        while not self._stop.is_set():
            base_source = source[0][0]
            base_live = time.time()
            previous_source = base_source
            for source_ts, kind, payload in source:
                delay = max(0.0, source_ts - previous_source) / self.speed
                if self._stop.wait(delay):
                    return
                live_ts = base_live + (source_ts - base_source) / self.speed
                if kind == "packet":
                    copied = Packet(**{**payload.__dict__, "ts": live_ts})
                else:
                    values = {**payload.__dict__, "ts": live_ts,
                              "repeated_by": set(payload.repeated_by)}
                    copied = ChatMessage(**values)
                self.emit(kind, copied)
                previous_source = source_ts
            self.emit("status", f"ghost replay complete: {len(source)} events")
            if not self.loop:
                self._stop.wait()
                return


def build_replay_gateway(database: str | Path, *, socket_path: str | Path | None = None,
                         start_ts: float | None = None, end_ts: float | None = None,
                         limit: int = 20000, speed: float = 1.0,
                         loop: bool = False, protocol: str = "auto") -> Gateway:
    source = Store(database)
    if not source.path.exists():
        raise ValueError(f"no SQLite capture at {source.path}")
    packet_rows = source.replay_packets(start_ts, end_ts, limit)
    packets = [packet_from_row(row) for row in packet_rows]
    messages = source.replay_messages(start_ts, end_ts, limit)
    if packets and messages and start_ts is None and end_ts is None:
        # Bare-limit windows cover different spans of history - N packets are
        # hours, N chat messages are days - and merging them stalls the ghost
        # in days of chat-only gaps before the first packet plays. Anchor the
        # replay on the dense packet window.
        window_start = packets[0].ts
        messages = [m for m in messages if m.ts >= window_start]
    nodes = source.known_nodes()
    if protocol == "auto":
        # The synthetic portnums MeshCore traffic is actually stored under.
        meshcore_ports = {"RXLOG_APP", "ADVERT_APP", "PATH_APP", "ADMIN_APP",
                          "ROUTING_APP", "STATUS_APP"}
        protocol = "meshcore" if any(packet.portnum in meshcore_ports
                                     for packet in packets) else "meshtastic"
    observer = source.get_meta(LAST_OBSERVER)
    service = MeshService(None)
    link = ReplayLink(service.handle_event, packets, messages, nodes,
                      protocol=protocol, observer=str(observer) if observer else None,
                      speed=speed, loop=loop)
    service.state.protocol = protocol
    return Gateway(service, link, socket_path or default_replay_socket_path())


def _pcap_records(path: str | Path, start_ts: float | None = None,
                  end_ts: float | None = None, limit: int = 20000
                  ) -> list[tuple[float, bytes, int]]:
    """Read classic PCAP or PCAPNG without adding a packet-library dependency."""
    capture = Path(path)
    if not capture.exists():
        raise ValueError(f"no packet capture at {capture}")
    data = capture.read_bytes()
    if len(data) < 12:
        raise ValueError("packet capture is truncated")
    records: list[tuple[float, bytes, int]] = []

    def add(ts: float, payload: bytes, linktype: int) -> None:
        if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts):
            if len(records) < max(1, limit):
                records.append((ts, payload, linktype))

    magics = {
        b"\xd4\xc3\xb2\xa1": ("<", 1_000_000.0),
        b"\xa1\xb2\xc3\xd4": (">", 1_000_000.0),
        b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000.0),
        b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000.0),
    }
    if data[:4] in magics:
        endian, resolution = magics[data[:4]]
        if len(data) < 24:
            raise ValueError("PCAP global header is truncated")
        linktype = struct.unpack_from(endian + "I", data, 20)[0]
        offset = 24
        while offset + 16 <= len(data) and len(records) < max(1, limit):
            seconds, fraction, captured, _original = struct.unpack_from(
                endian + "IIII", data, offset)
            offset += 16
            if captured > 16 * 1024 * 1024 or offset + captured > len(data):
                raise ValueError("PCAP packet record is truncated or unreasonably large")
            add(seconds + fraction / resolution, data[offset:offset + captured], linktype)
            offset += captured
        return records

    if data[:4] != b"\x0a\x0d\x0d\x0a":
        raise ValueError("capture is neither classic PCAP nor PCAPNG")

    offset = 0
    endian = "<"
    interfaces: list[tuple[int, float]] = []
    fallback_ts = 0.0
    while offset + 12 <= len(data) and len(records) < max(1, limit):
        block_type_bytes = data[offset:offset + 4]
        if block_type_bytes == b"\x0a\x0d\x0d\x0a":
            if offset + 12 > len(data):
                raise ValueError("PCAPNG section header is truncated")
            bom = data[offset + 8:offset + 12]
            if bom == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif bom == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise ValueError("PCAPNG has an invalid byte-order marker")
            interfaces = []
        block_type, block_len = struct.unpack_from(endian + "II", data, offset)
        if block_len < 12 or block_len % 4 or offset + block_len > len(data):
            raise ValueError("PCAPNG block is truncated or malformed")
        if struct.unpack_from(endian + "I", data, offset + block_len - 4)[0] != block_len:
            raise ValueError("PCAPNG block length footer does not match")
        body = data[offset + 8:offset + block_len - 4]
        if block_type == 1 and len(body) >= 8:  # Interface Description Block
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            resolution = 1_000_000.0
            option = 8
            while option + 4 <= len(body):
                code, length = struct.unpack_from(endian + "HH", body, option)
                option += 4
                value = body[option:option + length]
                option += (length + 3) & ~3
                if code == 0:
                    break
                if code == 9 and value:
                    exponent = value[0] & 0x7f
                    resolution = float((2 if value[0] & 0x80 else 10) ** exponent)
            interfaces.append((linktype, resolution))
        elif block_type == 6 and len(body) >= 20:  # Enhanced Packet Block
            interface, high, low, captured, _original = struct.unpack_from(
                endian + "IIIII", body, 0)
            if interface >= len(interfaces) or 20 + captured > len(body):
                raise ValueError("PCAPNG enhanced packet references invalid data")
            linktype, resolution = interfaces[interface]
            timestamp = ((high << 32) | low) / resolution
            fallback_ts = timestamp
            add(timestamp, body[20:20 + captured], linktype)
        elif block_type == 3 and len(body) >= 4 and interfaces:  # Simple Packet Block
            original = struct.unpack_from(endian + "I", body, 0)[0]
            fallback_ts = fallback_ts + 0.001 if fallback_ts else time.time()
            add(fallback_ts, body[4:4 + min(original, len(body) - 4)], interfaces[0][0])
        offset += block_len
    return records


def _network_payload(data: bytes, linktype: int) -> bytes | None:
    """Peel common PCAP link/network headers to reach a serial/UDP payload."""
    offset = 0
    if linktype == 1 and len(data) >= 14:  # Ethernet
        ether_type = int.from_bytes(data[12:14], "big")
        offset = 14
        if ether_type in (0x8100, 0x88A8) and len(data) >= 18:
            ether_type = int.from_bytes(data[16:18], "big")
            offset = 18
        if ether_type not in (0x0800, 0x86DD):
            return None
    elif linktype == 113 and len(data) >= 16:  # Linux cooked capture v1
        offset = 16
    elif linktype == 0 and len(data) >= 4:  # BSD loopback
        offset = 4
    elif linktype not in (101, 228, 229):  # raw IPv4/IPv6 variants
        return None
    if offset >= len(data):
        return None
    version = data[offset] >> 4
    if version == 4 and len(data) >= offset + 20:
        ihl = (data[offset] & 0x0f) * 4
        protocol = data[offset + 9]
        start = offset + ihl
    elif version == 6 and len(data) >= offset + 40:
        protocol = data[offset + 6]
        start = offset + 40
    else:
        return None
    if protocol == 17 and len(data) >= start + 8:  # UDP
        return data[start + 8:]
    if protocol == 6 and len(data) >= start + 20:  # TCP, no stream reassembly
        header = ((data[start + 12] >> 4) & 0x0f) * 4
        return data[start + header:]
    return None


def _capture_candidates(data: bytes, linktype: int) -> list[bytes]:
    candidates = [data]
    network = _network_payload(data, linktype)
    if network and network != data:
        candidates.insert(0, network)
    # Meshtastic's stream framing is 94 c3 + a big-endian protobuf length.
    for source in list(candidates):
        cursor = 0
        while True:
            start = source.find(b"\x94\xc3", cursor)
            if start < 0 or start + 4 > len(source):
                break
            length = int.from_bytes(source[start + 2:start + 4], "big")
            end = start + 4 + length
            if 0 < length <= 65535 and end <= len(source):
                candidates.insert(0, source[start + 4:end])
            cursor = start + 2
    unique: list[bytes] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def _meshtastic_packet(data: bytes, timestamp: float) -> Packet | None:
    from google.protobuf.json_format import MessageToDict
    from google.protobuf.message import DecodeError
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    mesh_packet = None
    try:
        wrapper = mesh_pb2.FromRadio.FromString(data)
        if wrapper.HasField("packet"):
            mesh_packet = wrapper.packet
    except DecodeError:
        pass
    if mesh_packet is None:
        try:
            candidate = mesh_pb2.MeshPacket.FromString(data)
        except DecodeError:
            return None
        meaningful = (getattr(candidate, "from") or candidate.to or candidate.id
                      or candidate.encrypted or candidate.HasField("decoded"))
        if not meaningful:
            return None
        mesh_packet = candidate
    raw = MessageToDict(mesh_packet)
    raw["from"] = getattr(mesh_packet, "from")
    raw["to"] = mesh_packet.to
    raw["rxTime"] = timestamp
    if mesh_packet.HasField("decoded"):
        decoded = raw.setdefault("decoded", {})
        decoded["payload"] = bytes(mesh_packet.decoded.payload)
        decoded["portnum"] = portnums_pb2.PortNum.Name(mesh_packet.decoded.portnum)
        if decoded["portnum"] == "TEXT_MESSAGE_APP":
            decoded["text"] = bytes(mesh_packet.decoded.payload).decode(
                "utf-8", errors="replace")
        else:
            try:
                from .radio import _expand_payload
                decoded.update(_expand_payload(decoded["portnum"], decoded["payload"]))
            except Exception:  # noqa: BLE001 - encrypted/unknown payload still replays
                pass
    return flatten(raw)


async def _meshcore_packets(records: list[tuple[float, bytes, int]]) -> tuple[
        list[Packet], list[dict[str, Any]]]:
    from meshcore.meshcore_parser import MeshcorePacketParser

    parser = MeshcorePacketParser()
    packets: list[Packet] = []
    nodes: list[dict[str, Any]] = []
    for timestamp, frame, linktype in records:
        parsed = None
        for candidate in _capture_candidates(frame, linktype):
            header = candidate[0]
            route_type, payload_type = header & 0x03, (header & 0x3c) >> 2
            cursor = 1 + (4 if route_type in (0, 3) else 0)
            if payload_type > 11 or cursor >= len(candidate):
                continue
            path_byte = candidate[cursor]
            required = cursor + 1 + (path_byte & 0x3f) * (((path_byte >> 6) & 3) + 1)
            if required > len(candidate):
                continue
            parsed = await parser.parsePacketPayload(candidate, {
                "payload_length": len(candidate), "recv_time": timestamp})
            break
        if parsed is None:
            continue
        from_id = key_to_id(parsed.get("adv_key") or parsed.get("pubkey_prefix") or "")
        kind = str(parsed.get("payload_typename") or "rx").lower()
        summary = f"rf {kind} {parsed.get('payload_length', '?')}B"
        if parsed.get("adv_key"):
            from_id = key_to_id(parsed["adv_key"])
            summary = f"advert from {parsed.get('adv_name') or from_id} (pcap)"
            contact = {"public_key": parsed["adv_key"],
                       "last_advert": timestamp}
            for field in ("adv_name", "adv_lat", "adv_lon", "adv_type"):
                if parsed.get(field) is not None:
                    contact[field] = parsed[field]
            nodes.append(contact_to_node(contact))
        packets.append(Packet(
            ts=timestamp, from_id=from_id, to_id="^all", portnum="RXLOG_APP",
            summary=summary, hops=(parsed.get("path_len")
                                  if parsed.get("route_typename") in ("FLOOD", "TC_FLOOD")
                                  else None), raw=parsed))
    return packets, nodes


def build_pcap_replay_gateway(capture: str | Path, *,
                              socket_path: str | Path | None = None,
                              start_ts: float | None = None,
                              end_ts: float | None = None, limit: int = 20000,
                              speed: float = 1.0, loop: bool = False,
                              protocol: str = "auto") -> Gateway:
    """Decode supported MeshPacket/FromRadio/MeshCore frames from PCAP/PCAPNG."""
    records = _pcap_records(capture, start_ts, end_ts, limit)
    packets: list[Packet] = []
    nodes: list[dict[str, Any]] = []
    if protocol in ("auto", "meshtastic"):
        for timestamp, frame, linktype in records:
            for candidate in _capture_candidates(frame, linktype):
                packet = _meshtastic_packet(candidate, timestamp)
                if packet is not None:
                    packets.append(packet)
                    break
    if not packets and protocol in ("auto", "meshcore"):
        import asyncio
        packets, nodes = asyncio.run(_meshcore_packets(records))
        if packets:
            protocol = "meshcore"
    elif packets:
        protocol = "meshtastic"
    if not packets:
        raise ValueError(
            "capture contains no supported mesh frames (expected Meshtastic MeshPacket/"
            "FromRadio serial or UDP payloads, or raw MeshCore frames)")
    messages: list[ChatMessage] = []
    for packet in packets:
        decoded = packet.raw.get("decoded") if isinstance(packet.raw, dict) else None
        if packet.portnum == "TEXT_MESSAGE_APP" and isinstance(decoded, dict):
            text = decoded.get("text")
            if text:
                messages.append(ChatMessage(
                    ts=packet.ts, from_id=packet.from_id, from_name="",
                    to_id=packet.to_id, text=str(text), channel=packet.channel,
                    packet_id=packet.packet_id))
    service = MeshService(None)
    observer = "!00000000"
    link = ReplayLink(service.handle_event, packets, messages, nodes,
                      protocol=protocol, observer=observer, speed=speed,
                      loop=loop, source="pcap")
    service.state.protocol = protocol
    return Gateway(service, link, socket_path or default_replay_socket_path())
