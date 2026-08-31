"""Read-only companion snapshot and self-contained browser document."""

import time

from meshtui.model import BROADCAST, ChatMessage
from meshtui.pathcalc import PathObservation
from meshtui.service import MeshService
from meshtui.web import COMPANION_HTML, companion_snapshot

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


service = MeshService(None)
state = service.state
state.connected = True
state.protocol = "meshcore"
state.my_node_id = "!c0decafe"
me = state.upsert_node({"user": {"id": "!c0decafe", "longName": "Base"},
                        "position": {"latitude": 0.3, "longitude": 0.3}})
me.is_self = True
state.upsert_node({"user": {"id": "!aa000001", "longName": "Walker"},
                   "position": {"latitude": 0.1, "longitude": 0.1}})
state.upsert_node({"user": {"id": "!4c000001", "longName": "Hill",
                             "role": "REPEATER"},
                   "position": {"latitude": 0.2, "longitude": 0.2}})
state.add_chat(ChatMessage(ts=time.time(), from_id="channel:2:anonymous", from_name="",
                           to_id=BROADCAST, text="Walker: hello <script>alert(1)</script>",
                           channel=2, path_hash_size=1, route_mode="flood"))
state.note_path(PathObservation(ts=time.time(), kind="channel", origin_name="Walker",
                                path="4c", hops=1, channel=2))

snapshot = companion_snapshot(service)
check("snapshot is read-only state", snapshot["connected"], True)
check("snapshot includes positioned nodes", len(snapshot["nodes"]), 3)
check("MeshCore sender prefix is normalized", snapshot["messages"][0]["from"], "Walker")
check("message content stays data", "<script>" in snapshot["messages"][0]["text"], True)
check("snapshot contains a drawable route", len(snapshot["routes"][0]["points"]), 3)
check("browser renders text with textContent", "textContent=value" in COMPANION_HTML, True)
check("browser has no message send form", "<form" in COMPANION_HTML, False)
check("browser has no external CDN", "https://" in COMPANION_HTML, False)

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
