"""Compose routing intent and the mail-style receipt timeline."""

import os
import tempfile

from meshtui.events import destination_from_dict, destination_to_dict
from meshtui.meshcore_link import MeshCoreLink
from meshtui.model import DeliveryStatus, PeerRef, ChatMessage
from meshtui.service import MeshService
from meshtui.store import Store
from meshtui.widgets.chat_render import _receipt_timeline

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


key = "c0decafe" + "11" * 28
contact = {"public_key": key, "out_path": "4c82", "out_path_len": 2,
           "out_path_hash_mode": 0, "adv_name": "Walker"}
link = MeshCoreLink(lambda *_: None, port=None)
link.contacts["!c0decafe"] = contact

flood = link._destination_for(PeerRef("meshcore", "!c0decafe", None, "flood", 1))
direct = link._destination_for(PeerRef("meshcore", "!c0decafe", None, "direct", 1))
auto = link._destination_for(PeerRef("meshcore", "!c0decafe", None, "auto", 1))
check("flood override is per-send", flood["out_path_len"], -1)
check("direct override is per-send", direct["out_path_len"], 0)
check("auto keeps learned hops", auto["out_path_len"], 2)
check("route override does not mutate contact", contact["out_path_len"], 2)

ref = PeerRef("meshcore", "!c0decafe", key, "flood", 2)
check("gateway wire keeps compose route intent",
      destination_from_dict(destination_to_dict(ref)), ref)

message = ChatMessage(ts=1, from_id="!me", from_name="you", to_id="!c0decafe",
                      text="status", outgoing=True,
                      delivery_status=DeliveryStatus.DELIVERED.value, acked=True,
                      repeated_by={"Ridge", "Hill"})
timeline = str(_receipt_timeline(message))
check("receipt timeline starts queued", timeline.startswith("  queued"), True)
check("receipt timeline shows radio acceptance", "radio sent" in timeline, True)
check("receipt timeline counts repeats", "heard 2 repeats" in timeline, True)
check("receipt timeline ends in ACK", timeline.endswith("ACK"), True)

# Broadcasts have no end-to-end ACK - 'waiting' promised a step that cannot
# come. A repeated broadcast is complete; a fresh one is listening; an old
# unheard one just stops at what actually happened.
import time as _time
broadcast = ChatMessage(ts=_time.time() - 600, from_id="!me", from_name="you",
                        to_id="^all", text="wx", channel=13, outgoing=True,
                        delivery_status=DeliveryStatus.SENT.value,
                        repeated_by={"Hilltop Solar Repeater"})
line = str(_receipt_timeline(broadcast))
check("a repeated broadcast terminates at its repeats",
      line.endswith("heard 1 repeat") and "waiting" not in line, True)
fresh = ChatMessage(ts=_time.time(), from_id="!me", from_name="you", to_id="^all",
                    text="wx", channel=13, outgoing=True,
                    delivery_status=DeliveryStatus.SENT.value)
check("a fresh unheard broadcast is listening, not waiting",
      str(_receipt_timeline(fresh)).endswith("listening for repeats"), True)
old = ChatMessage(ts=_time.time() - 600, from_id="!me", from_name="you", to_id="^all",
                  text="wx", channel=13, outgoing=True,
                  delivery_status=DeliveryStatus.SENT.value)
old_line = str(_receipt_timeline(old))
check("an old unheard broadcast ends at radio sent",
      old_line.endswith("radio sent") and "waiting" not in old_line, True)
dm_pending = ChatMessage(ts=_time.time(), from_id="!me", from_name="you",
                         to_id="!c0decafe", text="hi", outgoing=True,
                         delivery_status=DeliveryStatus.SENT.value)
check("a DM still waits for its ACK",
      str(_receipt_timeline(dm_pending)).endswith("waiting"), True)

tmpdir = tempfile.mkdtemp(prefix="meshtui-compose-")
path = os.path.join(tmpdir, "mesh.db")
store = Store(path, flush_interval=0.01)
check("store opens", store.open(), True)
service = MeshService(store)
receipt = service.send_message("queued flood", ref)
check("offline compose is queued", receipt.status, DeliveryStatus.QUEUED)
store.close()

store2 = Store(path, flush_interval=0.01)
check("store reopens", store2.open(), True)
service2 = MeshService(store2)
restored = service2.outbox[receipt.message_id].destination
check("route mode survives retry restart", restored.route_mode, "flood")
check("path hash size survives retry restart", restored.path_hash_size, 2)
store2.close()

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
