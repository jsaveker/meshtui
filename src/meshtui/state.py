"""In-memory mesh state: node database, packet ring buffer, chat log, stats."""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import BROADCAST, ChatMessage, Node, Packet

PACKET_BUFFER = 2000
CHAT_BUFFER = 1000
RATE_WINDOW = 300.0  # seconds of history kept for the packets/min figure


@dataclass
class ForeignChannel:
    """A channel our node holds no key for.

    Identified only by the 8-bit hash every packet carries in the clear, so
    distinct channels can collide. Everything here is metadata - it needs no
    key at all, which is rather the point.
    """

    hash: int
    packets: int = 0
    senders: set[str] = field(default_factory=set)
    ports: Counter[str] = field(default_factory=Counter)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    snr_min: float | None = None
    snr_max: float | None = None
    hops_min: int | None = None
    hops_max: int | None = None
    key_label: str | None = None      # set when a published key decrypts it
    sample: str | None = None

    @property
    def readable(self) -> bool:
        return self.key_label is not None

    def observe(self, packet: Packet) -> None:
        self.packets += 1
        self.senders.add(packet.from_id)
        self.ports[packet.portnum] += 1
        self.last_seen = packet.ts
        if packet.snr is not None:
            self.snr_min = packet.snr if self.snr_min is None else min(self.snr_min, packet.snr)
            self.snr_max = packet.snr if self.snr_max is None else max(self.snr_max, packet.snr)
        if packet.hops is not None:
            self.hops_min = packet.hops if self.hops_min is None else min(self.hops_min, packet.hops)
            self.hops_max = packet.hops if self.hops_max is None else max(self.hops_max, packet.hops)


@dataclass
class RelayStat:
    """Traffic relayed to us by one node, keyed by the low byte of its number.

    Meshtastic only puts that single byte on the wire, so several nodes can
    share a key; `candidates` records the ambiguity rather than hiding it.
    """

    byte: int
    packets: int = 0
    origins: set[str] = field(default_factory=set)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    snr_sum: float = 0.0
    snr_n: int = 0

    @property
    def avg_snr(self) -> float | None:
        return self.snr_sum / self.snr_n if self.snr_n else None


@dataclass
class LocalChannel:
    """One of our own node's channels, where we can see the key."""

    index: int
    name: str
    level: str = "UNKNOWN"
    detail: str = ""
    hash: int | None = None


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    total: int = 0
    sent: int = 0
    by_port: Counter[str] = field(default_factory=Counter)
    _times: deque[float] = field(default_factory=lambda: deque(maxlen=PACKET_BUFFER))

    def record(self, packet: Packet) -> None:
        self.total += 1
        self.by_port[packet.portnum] += 1
        self._times.append(packet.ts)

    def rate_per_min(self) -> float:
        """Packets/min over the trailing RATE_WINDOW, or since start if newer."""
        if not self._times:
            return 0.0
        now = time.time()
        cutoff = now - RATE_WINDOW
        recent = [t for t in self._times if t >= cutoff]
        if not recent:
            return 0.0
        span = max(now - min(recent), 1.0)
        return len(recent) * 60.0 / span

    @property
    def uptime(self) -> float:
        return time.time() - self.started


class MeshState:
    """Single source of truth for everything the UI renders."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.packets: deque[Packet] = deque(maxlen=PACKET_BUFFER)
        self.chat: deque[ChatMessage] = deque(maxlen=CHAT_BUFFER)
        self.stats = Stats()
        self.channels: list[str] = []
        self.local_channels: list[LocalChannel] = []
        self.foreign_channels: dict[int, ForeignChannel] = {}
        self.relays: dict[int, RelayStat] = {}
        self.relay_edges: Counter[tuple[str, int]] = Counter()
        self.mqtt_packets: int = 0
        # Newest packet timestamp folded into state, so a restart can
        # replay only what the persisted snapshot has not already seen.
        self.last_packet_ts: float = 0.0
        self.my_node_id: str | None = None
        self.my_node_name: str = ""
        self.device_path: str = ""
        self.firmware: str = ""
        self.connected: bool = False

    # ---------------------------------------------------------------- nodes

    def upsert_node(self, raw: dict[str, Any]) -> Node:
        """Merge a NodeDB entry (or a synthesised one) into the node table.

        Only overwrites fields that are actually present, so a bare packet
        sighting never wipes out richer data we already have.
        """
        num = raw.get("num")
        user = raw.get("user") or {}
        node_id = user.get("id") or raw.get("id") or (f"!{num:08x}" if num else None)
        if node_id is None:
            raise ValueError("node record has neither num nor id")
        if num is None:
            num = int(node_id.lstrip("!"), 16)

        node = self.nodes.get(node_id)
        if node is None:
            node = Node(num=num, node_id=node_id)
            self.nodes[node_id] = node

        if user.get("longName"):
            node.long_name = user["longName"]
        if user.get("shortName"):
            node.short_name = user["shortName"]
        if user.get("hwModel"):
            node.hw_model = str(user["hwModel"])
        if user.get("role"):
            node.role = str(user["role"])

        for src, dst in (("snr", "snr"), ("hopsAway", "hops"), ("lastHeard", "last_heard")):
            if raw.get(src) is not None:
                setattr(node, dst, raw[src])
        if raw.get("viaMqtt"):
            node.via_mqtt = True

        metrics = raw.get("deviceMetrics") or {}
        for src, dst in (
            ("batteryLevel", "battery"),
            ("voltage", "voltage"),
            ("channelUtilization", "ch_util"),
            ("airUtilTx", "air_util"),
            ("uptimeSeconds", "uptime"),
        ):
            if metrics.get(src) is not None:
                setattr(node, dst, metrics[src])

        pos = raw.get("position") or {}
        if pos.get("latitude") is not None:
            node.lat = pos["latitude"]
        elif pos.get("latitudeI") is not None:
            node.lat = pos["latitudeI"] * 1e-7
        if pos.get("longitude") is not None:
            node.lon = pos["longitude"]
        elif pos.get("longitudeI") is not None:
            node.lon = pos["longitudeI"] * 1e-7
        if pos.get("altitude") is not None:
            node.alt = pos["altitude"]

        if self.my_node_id and node_id == self.my_node_id:
            node.is_self = True
        return node

    def node_name(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        return node.label if node else (node_id or "?")

    def resolve(self, token: str) -> Node | None:
        """Look a node up by !id, short name, or long name (case-insensitive)."""
        token = token.strip()
        if not token:
            return None
        if token in self.nodes:
            return self.nodes[token]
        low = token.lower()
        for node in self.nodes.values():
            if node.short_name.lower() == low or node.node_id.lower() == low:
                return node
        for node in self.nodes.values():
            if node.long_name.lower() == low:
                return node
        return None

    def sorted_nodes(self, key: str = "heard") -> list[Node]:
        nodes = list(self.nodes.values())
        if key == "name":
            nodes.sort(key=lambda n: n.name.lower())
        elif key == "snr":
            nodes.sort(key=lambda n: (n.snr is None, -(n.snr or 0)))
        elif key == "hops":
            nodes.sort(key=lambda n: (n.hops is None, n.hops or 0))
        elif key == "packets":
            nodes.sort(key=lambda n: -n.packets)
        else:  # heard - most recently seen first, self always pinned to top
            nodes.sort(key=lambda n: (not n.is_self, -(n.last_heard or 0)))
        return nodes

    # -------------------------------------------------------------- packets

    def add_packet(self, packet: Packet, historical: bool = False,
                   fold: bool = True) -> None:
        """Fold a packet into every derived view.

        `historical` marks a packet replayed from the database on startup: it
        rebuilds node and mesh state exactly as a live packet would, but must
        not inflate this session's counters or re-count per-node totals that
        were already restored from the nodes table.

        `fold=False` adds the packet to the visible buffer only. Startup uses
        it to show scrollback for packets the persisted snapshot has already
        counted, which would otherwise be tallied twice.
        """
        self.packets.append(packet)
        if not historical:
            self.stats.record(packet)
        if not fold:
            return
        self.last_packet_ts = max(self.last_packet_ts, packet.ts)
        if packet.via_mqtt:
            self.mqtt_packets += 1
        self._record_relay(packet)
        self._record_telemetry(packet)
        self._record_motion(packet)
        # Packets we could not decrypt carry the channel HASH here; decoded
        # ones carry the channel INDEX. Only the former are "foreign".
        if packet.portnum == "ENCRYPTED" or packet.decrypted_with:
            channel = self.foreign_channels.get(packet.channel)
            if channel is None:
                channel = ForeignChannel(hash=packet.channel, first_seen=packet.ts)
                self.foreign_channels[packet.channel] = channel
            channel.observe(packet)
            if packet.decrypted_with and channel.key_label is None:
                channel.key_label = packet.decrypted_with
                channel.sample = packet.summary[:60]
        node = self.nodes.get(packet.from_id)
        if node is not None:
            if not historical:
                # Replayed packets must not double-count: the nodes table
                # already carries the lifetime total.
                node.packets += 1
            # Never let a replayed or out-of-order packet drag this backwards.
            node.last_heard = max(node.last_heard or 0.0, packet.ts)
            if packet.snr is not None:
                node.snr = packet.snr
                # History comes from live packets only; NodeDB snapshots would
                # replay stale values and flatten the trend.
                node.snr_history.append(packet.snr)
            if packet.rssi is not None:
                node.rssi = packet.rssi
            if packet.hops is not None:
                node.hops = packet.hops

    def _record_relay(self, packet: Packet) -> None:
        """Every packet names the node that last forwarded it - that is the
        only routing evidence on the wire, and enough to build a real graph."""
        byte = packet.relay_node
        if byte is None:
            return
        relay = self.relays.get(byte)
        if relay is None:
            relay = RelayStat(byte=byte, first_seen=packet.ts)
            self.relays[byte] = relay
        relay.packets += 1
        relay.last_seen = packet.ts
        relay.origins.add(packet.from_id)
        if packet.snr is not None:
            relay.snr_sum += packet.snr
            relay.snr_n += 1
        # A packet relayed by its own originator is a direct reception, not a hop.
        if packet.hops:
            self.relay_edges[(packet.from_id, byte)] += 1

    def _record_telemetry(self, packet: Packet) -> None:
        if packet.portnum != "TELEMETRY_APP":
            return
        node = self.nodes.get(packet.from_id)
        if node is None:
            return
        telemetry = ((packet.raw.get("decoded") or {}).get("telemetry") or {})
        readings: dict[str, float] = {}
        for section in ("environmentMetrics", "airQualityMetrics"):
            for key, value in (telemetry.get(section) or {}).items():
                if isinstance(value, (int, float)):
                    readings[key] = float(value)
        if readings:
            node.env.update(readings)
            node.env_ts = packet.ts
        local = telemetry.get("localStats") or {}
        if local:
            node.local_stats = {k: float(v) for k, v in local.items()
                                if isinstance(v, (int, float))}
            node.local_stats_ts = packet.ts

    def _record_motion(self, packet: Packet) -> None:
        if packet.portnum != "POSITION_APP":
            return
        node = self.nodes.get(packet.from_id)
        if node is None:
            return
        pos = ((packet.raw.get("decoded") or {}).get("position") or {})
        speed = pos.get("groundSpeed")
        if speed is not None:
            node.speed_mps = float(speed)
        track = pos.get("groundTrack")
        if track is not None:
            # Scaled by 1e5 on the wire; the protobuf comment saying 1/100
            # degrees does not match what real nodes send.
            node.heading_deg = (float(track) / 1e5) % 360.0
        if pos.get("satsInView") is not None:
            node.sats = int(pos["satsInView"])
        if pos.get("precisionBits") is not None:
            node.precision_bits = int(pos["precisionBits"])
        if pos.get("locationSource"):
            node.location_source = str(pos["locationSource"])

        # Take coordinates from the packet itself rather than node.lat/lon: the
        # latter comes from NodeDB snapshots, which arrive on their own schedule,
        # so relying on it produces a track that never moves.
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        if lat is None and pos.get("latitudeI") is not None:
            lat = pos["latitudeI"] * 1e-7
        if lon is None and pos.get("longitudeI") is not None:
            lon = pos["longitudeI"] * 1e-7
        if lat is None or lon is None:
            return
        node.lat, node.lon = lat, lon
        last = node.track[-1] if node.track else None
        if last is None or abs(last[0] - lat) > 1e-6 or abs(last[1] - lon) > 1e-6:
            node.track.append((lat, lon, packet.ts))

    def resolve_relay(self, byte: int) -> list[Node]:
        """Nodes whose number ends in this byte. More than one means ambiguity."""
        return [n for n in self.nodes.values() if (n.num & 0xFF) == byte]

    def relay_share(self) -> list[tuple[RelayStat, float]]:
        """Relays sorted by share of all relayed traffic."""
        total = sum(r.packets for r in self.relays.values()) or 1
        return sorted(((r, r.packets / total) for r in self.relays.values()),
                      key=lambda kv: -kv[1])

    def sensor_nodes(self) -> list[Node]:
        return sorted((n for n in self.nodes.values() if n.env),
                      key=lambda n: -(n.env_ts or 0))

    def stats_nodes(self) -> list[Node]:
        return sorted((n for n in self.nodes.values() if n.local_stats),
                      key=lambda n: -(n.local_stats_ts or 0))

    # ----------------------------------------------------------------- chat

    def add_chat(self, message: ChatMessage) -> None:
        self.chat.append(message)

    def chat_for(self, channel: int | None, dm_with: str | None = None) -> Iterable[ChatMessage]:
        for msg in self.chat:
            if dm_with is not None:
                if msg.is_dm and dm_with in (msg.from_id, msg.to_id):
                    yield msg
            elif channel is None or (msg.channel == channel and not msg.is_dm):
                yield msg

    def ack(self, packet_id: int) -> ChatMessage | None:
        for msg in reversed(self.chat):
            if msg.packet_id == packet_id and msg.outgoing:
                msg.acked = True
                return msg
        return None

    # ---------------------------------------------------------------- misc

    def channel_name(self, index: int) -> str:
        if 0 <= index < len(self.channels):
            return self.channels[index]
        return f"ch{index}"

    def reset_link(self) -> None:
        """Clear per-connection facts, keeping history the user may still want."""
        self.connected = False
        self.my_node_id = None
        self.my_node_name = ""
        self.firmware = ""
        self.channels = []
