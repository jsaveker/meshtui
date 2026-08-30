"""Path observations: how packets actually travel the mesh to reach us.

Every RF-logged packet carries the path it took - one byte per repeater,
appended in the order traversed. Decoded adverts name their origin outright;
channel messages embed the sender's name in their text. Collected over time,
these observations show which repeaters carry the mesh, how far routes wander
compared to the straight line, and how a node's reachability changes - the
dataset behind the paths explorer and the !path bot.

Byte resolution and distance math live here so the bot reply, the persisted
record, and the explorer all agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .geo import haversine_km

KM_TO_MI = 0.621371

# A single LoRa hop beyond this is not real - it means a hop byte matched the
# wrong node - so no distance estimate at all beats a continent-sized one.
MAX_LEG_KM = 400.0


def is_rebroadcaster(node: Any) -> bool:
    """Only repeaters, routers, and room servers rebroadcast - matching the
    literal role strings both protocols use (REPEATER/ROUTER/ROOM/REP)."""
    role = (getattr(node, "role", "") or "").upper()
    return any(tag in role for tag in ("REP", "ROUTER", "ROOM"))

# One byte per hop is the MeshCore default; wider hashes are rare and we skip
# byte resolution for them rather than mis-attribute.
HASH_SIZE = 1


@dataclass
class PathObservation:
    """One packet's journey, as heard by our radio."""

    ts: float
    kind: str                     # "advert" | "channel"
    origin_id: str = ""           # "!xxxxxxxx" when the packet names its sender
    origin_name: str = ""         # advert name, or parsed from channel text
    path: str = ""                # hex, two chars per repeater byte
    hops: int = 0
    snr: float | None = None
    rssi: int | None = None
    channel: int | None = None    # channel slot for channel messages

    @property
    def key(self) -> tuple:
        """Collapses the RF-log sighting and the decoded message of the same
        packet (they arrive within milliseconds) into one observation."""
        return (int(self.ts), self.kind, self.path, self.hops)

    def hop_bytes(self) -> list[str]:
        return [self.path[i:i + 2] for i in range(0, len(self.path) // 2 * 2, 2)]


def split_sender(text: str) -> tuple[str, str]:
    """MeshCore channel messages embed the sender: 'Name: message'."""
    name, sep, rest = text.partition(": ")
    if sep and name and len(name) <= 40:
        return name.strip(), rest.strip()
    return "", text.strip()


def _signal(raw: dict[str, Any], key: str) -> Any:
    value = raw.get(key.lower())
    return value if value is not None else raw.get(key.upper())


def obs_from_packet(packet: Any) -> PathObservation | None:
    """Derive a path observation from a live packet, or None.

    Both the gateway and an attached TUI run this on their own packet stream,
    so the explorer works in either mode without a new wire event.
    """
    raw = packet.raw or {}
    if not isinstance(raw, dict):
        return None
    if packet.portnum == "RXLOG_APP":
        if raw.get("path_hash_size", HASH_SIZE) != HASH_SIZE:
            return None
        path = str(raw.get("path") or "")
        typename = raw.get("payload_typename")
        if typename == "ADVERT" and raw.get("adv_key"):
            return PathObservation(
                ts=packet.ts, kind="advert", origin_id=packet.from_id,
                origin_name=str(raw.get("adv_name") or ""), path=path,
                hops=len(path) // 2, snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"))
        if typename == "GRP_TXT":
            name, _ = split_sender(str(raw.get("message") or ""))
            return PathObservation(
                ts=packet.ts, kind="channel", origin_name=name, path=path,
                hops=len(path) // 2, snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"))
        return None
    if packet.portnum == "TEXT_MESSAGE_APP" and raw.get("type") == "CHAN":
        # The frame itself always carries path_len; the byte list appears when
        # the library's RX-log correlation matched (decrypt_channels on).
        path_len = raw.get("path_len")
        if not isinstance(path_len, int) or not 0 <= path_len < 64:
            return None
        path = str(raw.get("path") or "")
        name, _ = split_sender(str(raw.get("text") or ""))
        return PathObservation(
            ts=packet.ts, kind="channel", origin_name=name, path=path,
            hops=len(path) // 2 if path else path_len,
            snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"),
            channel=packet.channel)
    return None


# ------------------------------------------------------------------ analysis

@dataclass
class Hop:
    byte: str
    node: Any | None = None       # resolved Node, if exactly one candidate fits
    ambiguous: bool = False       # several known nodes share the byte

    @property
    def label(self) -> str:
        if self.node is None:
            return f"0x{self.byte}"
        name = self.node.long_name or self.node.node_id
        return f"{name}?" if self.ambiguous else name


@dataclass
class PathAnalysis:
    origin: Any | None
    me: Any | None
    hops: list[Hop] = field(default_factory=list)
    route_km: float | None = None
    direct_km: float | None = None

    @property
    def resolved(self) -> int:
        return sum(1 for h in self.hops if h.node is not None and not h.ambiguous)

    @property
    def stretch(self) -> float | None:
        """How far the route wandered: route distance over the straight line."""
        if self.route_km and self.direct_km and self.direct_km > 0.05:
            return self.route_km / self.direct_km
        return None

    def points(self) -> list[tuple[float, float, str, str]]:
        """Positioned chain in travel order: (lat, lon, label, role)."""
        out: list[tuple[float, float, str, str]] = []
        if self.origin is not None and self.origin.has_position:
            out.append((self.origin.lat, self.origin.lon,
                        self.origin.long_name or self.origin.node_id, "origin"))
        for hop in self.hops:
            # An ambiguous hop is a guess; a guessed position poisons the
            # drawing and the distance sum, so only confident hops join.
            if hop.node is not None and not hop.ambiguous and hop.node.has_position:
                out.append((hop.node.lat, hop.node.lon, hop.label, "hop"))
        if self.me is not None and self.me.has_position:
            out.append((self.me.lat, self.me.lon, "me", "me"))
        return out


def node_by_name(state: Any, name: str) -> Any | None:
    if not name:
        return None
    wanted = name.casefold()
    for node in state.nodes.values():
        if (node.long_name or "").casefold() == wanted:
            return node
    return None


def analyze(state: Any, obs: PathObservation) -> PathAnalysis:
    origin = state.nodes.get(obs.origin_id) or node_by_name(state, obs.origin_name)
    me = state.nodes.get(state.my_node_id or "")
    hops: list[Hop] = []
    for byte in obs.hop_bytes():
        try:
            candidates = state.resolve_relay(int(byte, 16))
        except ValueError:
            candidates = []
        # Many nodes share any single byte; only rebroadcasters can be hops,
        # so a lone repeater match is CONFIDENT even when chat nodes share
        # the byte. Prefer positioned (repeaters advertise locations as a
        # rule) and recently heard among what remains.
        plausible = [n for n in candidates if is_rebroadcaster(n)]
        pool = plausible or candidates
        pick = min(pool, key=lambda n: (not n.has_position,
                                        -(n.last_heard or 0.0))) if pool else None
        ambiguous = (len(plausible) > 1
                     or (not plausible and len(candidates) > 1))
        hops.append(Hop(byte=byte, node=pick, ambiguous=ambiguous))
    analysis = PathAnalysis(origin=origin, me=me, hops=hops)

    chain = analysis.points()
    if len(chain) >= 2:
        legs = [haversine_km(a[0], a[1], b[0], b[1])
                for a, b in zip(chain, chain[1:])]
        # One impossible leg means a byte matched the wrong node somewhere;
        # report nothing rather than a continent-sized route.
        if max(legs) <= MAX_LEG_KM:
            analysis.route_km = sum(legs)
    if (origin is not None and origin.has_position
            and me is not None and me.has_position):
        analysis.direct_km = haversine_km(origin.lat, origin.lon, me.lat, me.lon)
    return analysis


def fmt_mi(km: float) -> str:
    return f"~{km * KM_TO_MI:.1f}mi"


def bot_reply(state: Any, obs: PathObservation | None, requester: str) -> str:
    """The !path answer, in the dialect the mesh's other pathbots speak."""
    who = f"@[{requester}]" if requester else "@[?]"
    if obs is None:
        return f"{who} heard you, but no path data for that message"
    if obs.hops <= 0:
        return f"{who} direct (no path)"
    analysis = analyze(state, obs)
    reply = f"{who} [{obs.hops}h]"
    if obs.path:
        reply += " " + ",".join(obs.hop_bytes())
    details = []
    if analysis.route_km:
        details.append(f"route: {fmt_mi(analysis.route_km)}")
    if analysis.direct_km:
        details.append(f"direct: {fmt_mi(analysis.direct_km)}")
    if details:
        reply += " " + ", ".join(details)
    if obs.path and analysis.resolved < obs.hops:
        reply += f" ({analysis.resolved}/{obs.hops})"
    return reply
