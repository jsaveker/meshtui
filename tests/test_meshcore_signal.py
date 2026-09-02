"""MeshCore signal data must reach the node table.

The RF log is the only place MeshCore ties SNR/RSSI to a sender: it decodes a
heard advert completely (adv_key, name, position) alongside the receive-side
signal numbers, and a flood advert's path grows a byte per repeater so
path_len is the hop count. The old code read `pubkey_prefix` - a field the
parser never sets for adverts - so every RF-log packet was attributed to
nobody and the SNR/Trend/Hop columns stayed blank. Battery is millivolts:
get_bat for the own radio, the status reply for repeaters.
"""
import asyncio, sys, time, types

from meshtui.meshcore_link import MeshCoreLink
from meshtui.model import Node
from meshtui.service import MeshService
from meshtui.widgets.nodes import fmt_battery

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

def collect():
    events = []
    return events, MeshCoreLink(lambda k, p: events.append((k, p)), port="/dev/null")

ADV_KEY = "ab" * 32

# ------------------------------------------------- rx-log advert attribution
events, link = collect()
link._on_rx_log(types.SimpleNamespace(payload={
    "adv_key": ADV_KEY, "adv_name": "Hill Repeater", "adv_lat": 10.3, "adv_lon": 20.3,
    "snr": -6.5, "rssi": -102, "route_typename": "FLOOD", "path_len": 2,
    "path": "1a4c", "payload_typename": "ADVERT", "payload_length": 48,
}))
packets = [p for k, p in events if k == "packet"]
contacts = [p for k, p in events if k == "mc_contact"]
check("rx-log advert emits one packet and one contact",
      (len(packets), len(contacts)), (1, 1))
check("packet is attributed to the sender", packets[0].from_id, "!abababab")
check("flood path_len becomes the hop count", packets[0].hops, 2)
check("the delivering repeater is the LAST path byte", packets[0].relay_node, 0x4C)
check("the complete delivering hash survives", packets[0].relay_hash, "4c")
check("the packet keeps the receive-side SNR", packets[0].snr, -6.5)

# heard direct (path_len 0): the SNR really is the sender's link to us
events, link = collect()
link._on_rx_log(types.SimpleNamespace(payload={
    "adv_key": ADV_KEY, "snr": -3.25, "rssi": -95, "route_typename": "FLOOD",
    "path_len": 0, "payload_typename": "ADVERT", "payload_length": 40}))
direct = next(p for k, p in events if k == "packet")
check("direct advert SNR is credited to the sender", direct.snr, -3.25)
check("direct advert is zero hops", direct.hops, 0)
check("contact update carries the full key", contacts[0].get("public_key"), ADV_KEY)
check("contact last_advert uses our clock",
      abs(contacts[0]["last_advert"] - time.time()) < 5, True)

# an RF log entry with no decoded sender stays unattributed and adds no contact
events, link = collect()
link._on_rx_log(types.SimpleNamespace(payload={
    "snr": 3.0, "rssi": -70, "payload_typename": "GRP_TXT", "payload_length": 30}))
check("senderless rx-log stays unattributed",
      [p.from_id for k, p in events if k == "packet"], ["!00000000"])
check("senderless rx-log adds no contact",
      any(k == "mc_contact" for k, _ in events), False)

# ------------------------------------------------- push advert freshens age
events, link = collect()
link._on_advert(types.SimpleNamespace(payload={"public_key": ADV_KEY}))
contact = next(p for k, p in events if k == "mc_contact")
check("push advert stamps last_advert with our clock",
      abs(contact.get("last_advert", 0) - time.time()) < 5, True)

# ------------------------------------------------- self battery via get_bat
events, link = collect()
link.my_node_id = "!abababab"
class FakeCommands:
    @staticmethod
    async def get_bat():
        return types.SimpleNamespace(payload={"level": 4123})
link.mc = types.SimpleNamespace(commands=FakeCommands)
asyncio.run(link._read_battery())
node_updates = [p for k, p in events if k == "node"]
check("get_bat millivolts become a voltage node update",
      node_updates, [{"id": "!abababab", "deviceMetrics": {"voltage": 4.12}}])

# ------------------------------------------------- repeater status battery
service = MeshService(store=None)
service.handle_event("mc_status", ("!abababab", {"bat": 3874, "uptime": 3600}))
node = service.state.nodes["!abababab"]
check("status reply battery lands on the node as volts", node.voltage, 3.87)
check("status reply uptime lands on the node", node.uptime, 3600)

# ------------------------------------------------- Bat column voltage fallback
check("Bat column falls back to voltage", str(fmt_battery(node)), "3.9V")
check("Bat column still prefers a percentage",
      str(fmt_battery(Node(num=1, node_id="!01", battery=80))), "80%")

# --------------------------------------- relayed SNR goes to the relay only
from meshtui.model import Packet
from meshtui.state import MeshState

state = MeshState()
state.protocol = "meshcore"
origin = state.upsert_node({"user": {"id": "!abababab"}})
repeater = state.upsert_node({"user": {"id": "!4c000000", "longName": "Hilltop"}})
state.add_packet(Packet(ts=time.time(), from_id="!abababab", to_id="^all",
                        portnum="RXLOG_APP", summary="advert (rf)", snr=-6.5,
                        hops=2, relay_node=0x4C))
check("origin of a relayed packet gets NO snr", origin.snr, None)
relay = state.relays[0x4C]
check("the relay records the snr", (relay.last_snr, list(relay.snr_history)),
      (-6.5, [-6.5]))
check("meshcore relay byte resolves via the key's first byte",
      [n.node_id for n in state.resolve_relay(0x4C)], ["!4c000000"])
state.add_packet(Packet(ts=time.time(), from_id="!abababab", to_id="^all",
                        portnum="RXLOG_APP", summary="advert (rf)", snr=2.0, hops=0))
check("a direct packet credits the origin", origin.snr, 2.0)

# ---------------------------------------------- channel audit (meshcore)
events, link = collect()
link.channels = [(0, "Public"), (2, "#bot"), (5, "#weak"), (7, "#sealed")]
link.channel_secrets = {0: bytes(range(16)), 2: bytes(range(16, 32)),
                        5: b"\x42" * 16}
link.channel_hashes = {0: "11", 2: "2a"}
records = link._channel_security()
by_name = {r["name"]: r for r in records}
check("the Public channel is called what it is",
      (by_name["Public"]["level"], by_name["Public"]["hash"]), ("PUBLIC", 0x11))
check("a random shared key grades AES128 with an honest caveat",
      (by_name["#bot"]["level"], "QR" in by_name["#bot"]["detail"]), ("AES128", True))
check("a degenerate key is flagged weak", by_name["#weak"]["level"], "WEAK")
check("an unreadable key stays unknown", by_name["#sealed"]["level"], "UNKNOWN")

audit_service = MeshService(store=None)
audit_service.handle_event("mc_channel_security", records)
check("channel verdicts land in state for the audit screen",
      [c.level for c in audit_service.state.local_channels],
      ["PUBLIC", "AES128", "WEAK", "UNKNOWN"])

# a group text on a channel we hold no key for feeds the foreign table
events, link = collect()
link._on_rx_log(types.SimpleNamespace(payload={
    "payload_typename": "GRP_TXT", "payload_length": 40, "snr": -2.0,
    "chan_hash": "7f", "path": "4c", "path_len": 1}))
foreign_packet = next(p for k, p in events if k == "packet")
check("an unknown-channel group text is marked encrypted with its hash",
      (foreign_packet.encrypted, foreign_packet.channel), (True, 0x7F))
audit_state = MeshState()
audit_state.add_packet(foreign_packet)
check("it lands in the foreign-channels table, sender honestly uncounted",
      (0x7F in audit_state.foreign_channels,
       len(audit_state.foreign_channels[0x7F].senders)), (True, 0))
known = types.SimpleNamespace(payload={
    "payload_typename": "GRP_TXT", "payload_length": 40, "snr": -2.0,
    "chan_hash": "11", "chan_name": "#bot", "message": "A: hi",
    "path": "4c", "path_len": 1})
events, link = collect()
link._on_rx_log(known)
known_packet = next(p for k, p in events if k == "packet")
check("a channel we can read is not foreign", known_packet.encrypted, False)

# --------------------------------------------------- local radio stats poll
events, link = collect()
class FakeStats:
    @staticmethod
    async def get_stats_radio():
        return types.SimpleNamespace(payload={
            "noise_floor": -108, "last_rssi": -95, "last_snr": 5.25,
            "tx_air_secs": 42, "rx_air_secs": 900})
link.mc = types.SimpleNamespace(commands=FakeStats)
asyncio.run(link._read_radio_stats())
stats_events = [p for k, p in events if k == "mc_radio_stats"]
check("radio stats are emitted from the local query",
      stats_events, [{"noise_floor": -108, "last_rssi": -95, "last_snr": 5.25,
                      "tx_air_secs": 42, "rx_air_secs": 900}])
service2 = MeshService(store=None)
service2.handle_event("mc_radio_stats", stats_events[0])
check("radio stats land in radio_info for snapshots",
      service2.state.radio_info.get("noise_floor"), -108)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
