"""MeshCore mapping tests. No radio required.

MeshCore's data model differs from Meshtastic's - contacts keyed by X25519
public key rather than a node database - so the translation into meshtui's
shared model is where bugs would hide.
"""

import sys
import time
import types

from meshtui.meshcore_link import (
    CONTACT_TYPES,
    MeshCoreLink,
    contact_to_node,
    key_to_id,
    key_to_num,
)
from meshtui.state import MeshState

failures: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


print("public key -> node id")
KEY = "c0decafe" + "11" * 28
check("hex key truncates to 8", key_to_id(KEY), "!c0decafe")
check("bytes accepted", key_to_id(bytes.fromhex(KEY)), "!c0decafe")
check("numeric id matches", key_to_num(KEY), 0xC0DECAFE)
check("empty key is safe", key_to_id(None), "!00000000")
check("stable across calls", key_to_id(KEY), key_to_id(KEY.upper()))

print("\ncontact -> node record")
contact = {
    "public_key": KEY,
    "adv_name": "Ridge Solar",
    "type": 2,                      # repeater
    "adv_lat": 0.30,
    "adv_lon": 0.30,
    "out_path_len": 2,
    "last_advert": 1787000000,
}
record = contact_to_node(contact)
check("id", record["user"]["id"], "!c0decafe")
check("long name", record["user"]["longName"], "Ridge Solar")
check("short name derived", record["user"]["shortName"], "Ridg")
check("type mapped", record["user"]["hwModel"], "REPEATER")
check("hops from path length", record["hopsAway"], 2)
check("position carried", record["position"]["latitude"], 0.30)
check("last advert carried", record["lastHeard"], 1787000000)

# A flood-routed contact reports -1, which is "no fixed path", not zero hops.
flood = contact_to_node({"public_key": KEY, "adv_name": "X", "out_path_len": -1})
check("flood route has no hop count", "hopsAway" in flood, False)

unnamed = contact_to_node({"public_key": KEY})
check("key-only update carries no destructive placeholder name",
      "longName" in unnamed["user"], False)

print("\ncontact types")
for value, label in CONTACT_TYPES.items():
    got = contact_to_node({"public_key": KEY, "type": value})["user"]["hwModel"]
    check(f"type {value} -> {label}", got, label)

print("\nrecords feed MeshState cleanly")
state = MeshState()
node = state.upsert_node(contact_to_node(contact))
check("node registered", node.node_id, "!c0decafe")
check("name available", node.name, "Ridge Solar")
check("role usable by the admin screen", node.role, "REPEATER")
check("position parsed", node.has_position, True)

print("\nevent handlers emit the right kinds")
emitted = []
link = MeshCoreLink(lambda k, p: emitted.append((k, p)))


def event(payload):
    return types.SimpleNamespace(payload=payload)


link._on_advert(event({"public_key": KEY, "adv_name": "Solar", "snr": 6.25}))
kinds = [k for k, _ in emitted]
check("advert emits contact + packet", kinds, ["mc_contact", "packet"])
packet = emitted[-1][1]
check("advert packet port", packet.portnum, "ADVERT_APP")
check("advert snr carried", packet.snr, 6.25)

emitted.clear()
link._on_channel_message(event({"pubkey_prefix": KEY, "text": "hello mesh",
                                "channel_idx": 0}))
check("channel msg emits chat + packet", [k for k, _ in emitted], ["chat", "packet"])
check("chat text", emitted[0][1].text, "hello mesh")
check("chat channel", emitted[0][1].channel, 0)

emitted.clear()
link._on_direct_message(event({"pubkey_prefix": KEY, "text": "dm here"}))
check("dm is not a broadcast channel", emitted[0][1].channel, -1)
check("dm addressed to us", emitted[0][1].to_id, "self")

print("\nroom posts stay in the room thread and show their signed author")
ROOM_KEY = "feedface" + "01" * 28
AUTHOR_KEY = "aabbccdd" + "02" * 28
link.contacts = {
    "!feedface": {"public_key": ROOM_KEY, "adv_name": "Town Room", "type": 3},
    "!aabbccdd": {"public_key": AUTHOR_KEY, "adv_name": "Walker", "type": 1},
}
emitted.clear()
link._on_direct_message(event({
    "pubkey_prefix": ROOM_KEY[:12], "signature": AUTHOR_KEY[:8],
    "txt_type": 2, "sender_timestamp": 1234, "text": "arrived safely",
}))
room_post = emitted[0][1]
check("room thread belongs to room", room_post.from_id, "!feedface")
check("room post author resolved", room_post.from_name, "Walker")
check("room post timestamp retained", room_post.ts, 1234)
check("packet summary identifies room author", "room post by Walker" in emitted[1][1].summary,
      True)

emitted.clear()
link._on_login_ok(event({"pubkey_prefix": KEY}))
check("login success emitted", emitted[0], ("mc_login", ("!c0decafe", True)))
check("session recorded", "!c0decafe" in link.logged_in, True)
link._on_login_fail(event({"pubkey_prefix": KEY}))
check("failed login clears the session", "!c0decafe" in link.logged_in, False)

emitted.clear()
link._on_cli_reply(event({"pubkey_prefix": KEY, "response": "v1.17.1"}))
check("cli reply", emitted[0], ("mc_cli", ("!c0decafe", "v1.17.1")))

print("\nsparse channel slots keep their real index")
import asyncio
from meshtui.widgets.channels import parse_secret

# MeshCore slots are not contiguous. A channel at slot 12 must still be
# addressed as 12, not as "the third tab".
state2 = MeshState()
state2.channels = [(0, "Public"), (5, "#austin"), (12, "Ops")]
check("name lookup by real index", state2.channel_name(12), "Ops")
check("gap index is not a channel", state2.channel_name(3), "ch3")
check("first slot still works", state2.channel_name(0), "Public")

# A bare list of names (Meshtastic) stays positional.
state3 = MeshState()
state3.channels = ["LongFast", "Ops", "Private"]
check("meshtastic list is positional", state3.channel_name(1), "Ops")

print("\nchannel secret parsing")
check("32 hex chars accepted", parse_secret("00112233445566778899aabbccddeeff"),
      bytes.fromhex("00112233445566778899aabbccddeeff"))
check("colons tolerated", parse_secret("00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff") is not None, True)
check("wrong length rejected", parse_secret("00112233"), None)
check("non-hex rejected", parse_secret("z" * 32), None)

print("\nstored packets replay without knowing the protocol")
# Replay used to re-run Meshtastic's decoder over stored rows, which turned
# every MeshCore packet into "? -> ? UNKNOWN" on startup.
from meshtui.app import MeshTUI
import json as _json

mc_row = {
    "ts": 1787880000.0, "from_id": "!bcdecafe", "to_id": "^all",
    "portnum": "ADVERT_APP", "channel": 0, "snr": 11.5, "rssi": -56,
    "hops": None, "packet_id": None, "summary": "advert from !bcdecafe",
    "raw": _json.dumps({"adv_name": "Ridge Solar Repeater"}),
}
p1 = MeshTUI._packet_from_row(mc_row)
check("meshcore portnum survives", p1.portnum, "ADVERT_APP")
check("meshcore sender survives", p1.from_id, "!bcdecafe")
check("meshcore summary survives", p1.summary, "advert from !bcdecafe")
check("meshcore snr survives", p1.snr, 11.5)

mt_row = {
    "ts": 1787880001.0, "from_id": "!5b1bf491", "to_id": "^all",
    "portnum": "POSITION_APP", "channel": 0, "snr": 6.25, "rssi": -67,
    "hops": 2, "packet_id": 4242, "summary": "0.30, 0.30",
    "raw": _json.dumps({"relayNode": 145, "viaMqtt": True}),
}
p2 = MeshTUI._packet_from_row(mt_row)
check("meshtastic relay byte survives", p2.relay_node, 145)
check("meshtastic mqtt flag survives", p2.via_mqtt, True)
check("meshtastic hops survive", p2.hops, 2)
check("meshtastic packet id survives", p2.packet_id, 4242)

broken = MeshTUI._packet_from_row({"ts": 1.0, "portnum": "ENCRYPTED", "raw": "not json"})
check("unparseable raw does not crash replay", broken.portnum, "ENCRYPTED")
check("encrypted flag derived", broken.encrypted, True)
check("missing sender falls back", broken.from_id, "?")

print("\nrepeater console output is not chat")
# send_cmd travels as a text message tagged CLI_DATA, and so does the reply -
# the tag is the only thing separating console output from someone saying hello.
emitted.clear()
link._admin_target = "!c0decafe"
link._on_direct_message(event({"pubkey_prefix": KEY, "txt_type": 1,
                               "text": "v1.17.1-d929643 (Build: 14-Aug-2026)"}))
kinds2 = [k for k, _ in emitted]
check("cli reply does not become chat", "chat" in kinds2, False)
check("cli reply reaches the admin log", "mc_cli" in kinds2, True)
check("cli text preserved", emitted[0][1][1], "v1.17.1-d929643 (Build: 14-Aug-2026)")
check("cli reply labelled as admin traffic",
      next(p for k, p in emitted if k == "packet").portnum, "ADMIN_APP")

emitted.clear()
link._on_direct_message(event({"pubkey_prefix": KEY, "txt_type": 3, "text": "ver"}))
check("an echoed CLI_CMD is also not chat", [k for k, _ in emitted][0], "mc_cli")

emitted.clear()
link._on_direct_message(event({"pubkey_prefix": KEY, "txt_type": 0, "text": "hello"}))
check("a plain direct message is still chat", [k for k, _ in emitted][0], "chat")
emitted.clear()
link._on_direct_message(event({"pubkey_prefix": KEY, "text": "no txt_type"}))
check("a message with no txt_type is still chat", [k for k, _ in emitted][0], "chat")

print("\nadmin replies attribute to the node we addressed")
# LOGIN_SUCCESS only carries pubkey_prefix on long enough frames, and CLI_REPLY
# never carries one at all - so a reply must be matched to who we asked.
link.contacts["!c0decafe"] = {"public_key": KEY, "adv_name": "Repeater"}
link.logged_in.clear()
link._pending_login = "!c0decafe"
link._on_login_ok(event({"permissions": 1, "is_admin": True}))   # no pubkey field
check("keyless login attributes to the target", "!c0decafe" in link.logged_in, True)
check("phantom node not created", "!00000000" in link.logged_in, False)
check("admin target remembered", link._admin_target, "!c0decafe")

emitted.clear()
link._on_cli_reply(event({"text": "v1.17.1"}))                   # CLI_REPLY has no key
check("keyless cli reply attributes to the target",
      emitted[0], ("mc_cli", ("!c0decafe", "v1.17.1")))

# When the payload does identify the sender, that wins over the fallback.
link._admin_target = "!ffffffff"
emitted.clear()
link._on_cli_reply(event({"pubkey_prefix": KEY[:12], "text": "from me"}))
check("payload key beats the fallback", emitted[0][1][0], "!c0decafe")

link.logged_in.clear()
link._pending_login = "!c0decafe"
link._on_login_fail(event({}))
check("keyless failure clears the right node", link.logged_in, set())

print("\ncommands refuse unknown contacts rather than crashing")
emitted.clear()
link.remote_command("!deadbeef", "ver")
check("unknown contact errors", emitted[0][0], "error")
emitted.clear()
ok, _ = link.send_text("hi", dest="!deadbeef")
check("send to unknown contact fails cleanly", ok, False)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("PASS")
