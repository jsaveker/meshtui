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


def plausible_relays(candidates: list) -> tuple[list, bool]:
    """Narrow byte-share candidates to who could have rebroadcast, sorted
    most-recently-heard first, plus whether the answer is still a guess.

    THE shared ranking for every place a relay byte becomes a name (chat
    repeat badges, the relays view, hop resolution). Three copies of this
    logic have now each independently credited a rebroadcast to a chat node
    three states away; there will not be a fourth copy.
    """
    plausible = [n for n in candidates if is_rebroadcaster(n)]
    ambiguous = len(plausible) > 1 or (not plausible and len(candidates) > 1)
    pool = sorted(plausible or list(candidates),
                  key=lambda n: -(getattr(n, "last_heard", None) or 0.0))
    return pool, ambiguous

# The frame's 2-bit width field allows 1- to 4-byte per-hop path hashes (all
# seen in the wild, sometimes on the same mesh); a hash is the first byte(s)
# of the repeater's public key whatever the width.
HASH_SIZES = (1, 2, 3, 4)


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
        """One hex hash per hop. The hash width isn't stored - it's derived:
        hops counts hops (from the frame), path holds hops x width bytes."""
        if not self.path:
            return []
        chars = len(self.path) // 2 * 2
        width = 2
        if self.hops and chars % self.hops == 0 and chars // self.hops in (2, 4, 6, 8):
            width = chars // self.hops
        return [self.path[i:i + width] for i in range(0, chars // width * width, width)]


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
        hash_size = raw.get("path_hash_size", 1)
        if hash_size not in HASH_SIZES:
            return None
        path = str(raw.get("path") or "")
        # The frame's path_len IS the hop count, whatever the hash width.
        raw_hops = raw.get("path_len")
        hops = raw_hops if isinstance(raw_hops, int) else len(path) // (2 * hash_size)
        typename = raw.get("payload_typename")
        if typename == "ADVERT" and raw.get("adv_key"):
            return PathObservation(
                ts=packet.ts, kind="advert", origin_id=packet.from_id,
                origin_name=str(raw.get("adv_name") or ""), path=path,
                hops=hops, snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"))
        if typename == "GRP_TXT":
            name, _ = split_sender(str(raw.get("message") or ""))
            return PathObservation(
                ts=packet.ts, kind="channel", origin_name=name, path=path,
                hops=hops, snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"))
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
            hops=path_len, snr=_signal(raw, "snr"), rssi=_signal(raw, "rssi"),
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
        # A hash is the leading byte(s) of the repeater's public key, and a
        # node id is '!' plus the key's first four bytes - so a 2-byte hash
        # pins down 4 id characters and is rarely ambiguous.
        wanted = byte.lower()
        candidates = [n for n in state.nodes.values()
                      if n.node_id[1:1 + len(wanted)].lower() == wanted]
        # Many nodes share any single byte; plausible_relays keeps only who
        # could actually rebroadcast. Prefer positioned among what remains
        # (repeaters advertise locations as a rule).
        pool, ambiguous = plausible_relays(candidates)
        pick = min(pool, key=lambda n: (not n.has_position,
                                        -(n.last_heard or 0.0))) if pool else None
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
        direct = haversine_km(origin.lat, origin.lon, me.lat, me.lon)
        # Physics bound: a packet that arrived in N hops traveled at most N
        # maximum-length legs. A longer straight line means the origin's
        # advertised position is wrong (a stale fix, a default coordinate),
        # and an honest omission beats an ocean-crossing 'direct' figure.
        if direct <= max(1, obs.hops) * MAX_LEG_KM:
            analysis.direct_km = direct
    return analysis


def fmt_mi(km: float) -> str:
    return f"~{km * KM_TO_MI:.1f}mi"


def path_details(state: Any, obs: PathObservation,
                 with_hashes: bool = True) -> tuple[str, PathAnalysis]:
    """The journey tail both bots share: ' aa,bb route: ~Xmi, direct: ~Ymi (r/n)'.

    with_hashes=False gives the shorter distances-only variant, for when the
    full tail would not fit a LoRa payload."""
    analysis = analyze(state, obs)
    tail = ""
    if with_hashes and obs.path:
        tail += " " + ",".join(obs.hop_bytes())
    details = []
    if analysis.route_km:
        details.append(f"route: {fmt_mi(analysis.route_km)}")
    if analysis.direct_km:
        details.append(f"direct: {fmt_mi(analysis.direct_km)}")
    if details:
        tail += " " + ", ".join(details)
    if obs.path and analysis.resolved < obs.hops:
        tail += f" ({analysis.resolved}/{obs.hops})"
    return tail, analysis


def bot_reply(state: Any, obs: PathObservation | None, requester: str) -> str:
    """The !path answer, in the dialect the mesh's other pathbots speak."""
    who = f"@[{requester}]" if requester else "@[?]"
    if obs is None:
        return f"{who} heard you, but no path data for that message"
    if obs.hops <= 0:
        return f"{who} direct (no path)"
    tail, _ = path_details(state, obs)
    return f"{who} [{obs.hops}h]{tail}"


# ------------------------------------------------------------------ map link

def route_geojson(analysis: PathAnalysis) -> dict | None:
    """The positioned route as a FeatureCollection geojson.io can render.

    Property names follow simplestyle the way the mesh's other pathbots'
    working links do: `title` for labels, numbered `marker-symbol` per stop.
    """
    points = analysis.points()
    if len(points) < 2:
        return None
    colors = {"origin": "#2ecc71", "hop": "#f1c40f", "me": "#3498db"}
    features: list[dict] = [{
        "type": "Feature", "properties": {"title": "route"},
        "geometry": {"type": "LineString",
                     "coordinates": [[round(lon, 5), round(lat, 5)]
                                     for lat, lon, _, _ in points]},
    }]
    for index, (lat, lon, label, role) in enumerate(points, start=1):
        features.append({
            "type": "Feature",
            "properties": {"title": f"{index}. {label}",
                           "marker-symbol": str(index),
                           "marker-color": colors.get(role, "#aaaaaa")},
            "geometry": {"type": "Point",
                         "coordinates": [round(lon, 5), round(lat, 5)]},
        })
    return {"type": "FeatureCollection", "features": features}


def geojson_url(geojson: dict) -> str:
    """geojson.io renders JSON carried in its own URL fragment - the payload
    never leaves the viewer's browser except to the map-tile provider.

    base64, not percent-encoding: geojson.io's fragment decoding leaves some
    percent-escapes intact, which breaks its JSON parse ('unterminated
    string'), while base64 data URIs - what the other bots' working links
    use - survive every URL layer untouched."""
    import base64 as _base64
    import json as _json

    payload = _json.dumps(geojson, separators=(",", ":"))
    encoded = _base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return "https://geojson.io/#data=data:application/json;base64," + encoded
