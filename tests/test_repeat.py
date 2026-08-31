"""A sent channel message shows which repeaters rebroadcast it.

MeshCore reports each rebroadcast as an RX_LOG group-text carrying the repeater
path and a stable pkt_hash. The service ties those repeats to the message we
just sent, so the chat can show 'repeated by ...' like the phone apps do.
"""

import sys
import time

from meshtui.model import ChatMessage
from meshtui.service import MeshService

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class FakeLink:
    channel_hashes = {0: "11"}


svc = MeshService(store=None)
svc.attach_link(FakeLink())
# a synthetic repeater contact whose key starts bc -> node_id !bcdecafe
svc.state.upsert_node({"num": 0xbcdecafe, "user": {
    "id": "!bcdecafe", "longName": "Ridge Solar Repeater",
    "shortName": "RSG", "hwModel": "REPEATER", "role": "REPEATER"}})
svc.state.my_node_id = "!c0decafe"

now = time.time()
# our just-sent channel-0 message
mine = ChatMessage(ts=now, from_id="!c0decafe", from_name="you", to_id="^all",
                   text="storm inbound", channel=0, outgoing=True,
                   message_id="m1")
svc.state.add_chat(mine)

print("first repeat, matched by channel + time window")
svc.note_repeat({"chan_hash": "11", "pkt_hash": 111, "path": ["bc"], "ts": now + 1})
check("repeater resolved to a name", "Ridge Solar Repeater" in mine.repeated_by, True)
check("pkt hash anchored", mine.repeat_pkt, 111)

print("\nsecond repeat, same packet, another repeater -> accumulates")
# a second repeater 0x22
svc.state.upsert_node({"num": 0x22ffffff, "user": {
    "id": "!22ffffff", "longName": "e422", "shortName": "e422",
    "hwModel": "REPEATER", "role": "REPEATER"}})
svc.note_repeat({"chan_hash": "11", "pkt_hash": 111, "path": ["22"], "ts": now + 3})
check("second repeater added", len(mine.repeated_by), 2)

print("\nunknown repeater byte shows as hex")
svc.note_repeat({"chan_hash": "11", "pkt_hash": 111, "path": ["9f"], "ts": now + 4})
check("unknown byte labelled", "0x9f" in mine.repeated_by, True)

print("\na repeat on a different channel is not attributed to this message")
other = ChatMessage(ts=now, from_id="!c0decafe", from_name="you", to_id="^all",
                    text="ops msg", channel=5, outgoing=True, message_id="m2")
svc.state.add_chat(other)
svc.note_repeat({"chan_hash": "77", "pkt_hash": 222, "path": ["bc"], "ts": now + 2})
check("wrong-channel repeat not attached to m1", mine.repeat_pkt, 111)
check("wrong-channel repeat not attached to m2", other.repeated_by, set())

print("\na different packet on the same channel is NOT added to an anchored msg")
svc.note_repeat({"chan_hash": "11", "pkt_hash": 555, "path": ["da"], "ts": now + 5})
check("foreign packet not attached to m1", 555 in svc._repeat_pkt_to_message, False)
check("m1 repeaters unchanged by foreign packet", len(mine.repeated_by), 3)

print("\na stale repeat (past the window, unknown packet) is ignored")
old = ChatMessage(ts=now - 600, from_id="!c0decafe", from_name="you", to_id="^all",
                  text="ancient", channel=0, outgoing=True, message_id="m3")
svc.state.add_chat(old)
r = svc.note_repeat({"chan_hash": "11", "pkt_hash": 999, "path": ["bc"], "ts": now + 700})
# newest channel-0 outgoing within window is 'mine'? mine.ts=now, event ts now+700 -> out of window
check("stale repeat matches nothing", r, None)

# --- a path byte must credit the plausible repeater, not the first match ---
svc2 = MeshService(store=None)
svc2.state.upsert_node({"user": {"id": "!4c000001", "longName": "Distant Chat Node",
                                 "role": "CHAT"}, "lastHeard": time.time() - 86400})
svc2.state.upsert_node({"user": {"id": "!4c000002", "longName": "Local Repeater",
                                 "role": "REPEATER"}, "lastHeard": time.time() - 60})
check("the repeater outranks a chat node sharing the byte",
      svc2._repeater_label("4c"), "Local Repeater")
svc2.state.upsert_node({"user": {"id": "!4c000003", "longName": "Other Repeater",
                                 "role": "REPEATER"}, "lastHeard": time.time() - 30})
check("two repeaters on one byte stays honestly ambiguous",
      svc2._repeater_label("4c").endswith("?"), True)
check("an unknown byte stays a hex label", svc2._repeater_label("ff"), "0xff")

print()
if failures:
    print(f"FAIL: {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("PASS")
