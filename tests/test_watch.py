"""Watch-filter grammar and protocol-scoped named views."""

import os
import tempfile
import time

from meshtui.model import Packet
from meshtui.preferences import OperatorPreferences
from meshtui.state import MeshState
from meshtui.watch import parse_watch

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


state = MeshState()
state.protocol = "meshcore"
state.channels = [(7, "Public"), (12, "Ops")]
state.upsert_node({"user": {"id": "!aa000001", "longName": "Walker"}})
packet = Packet(ts=time.time(), from_id="!aa000001", to_id="^all",
                portnum="TEXT_MESSAGE_APP", summary="Walker: status green",
                channel=7, hops=4, snr=3.5, raw={"protocol": "meshcore"})

watch = parse_watch("proto:mc hop>=3 snr<5 chan:#public")
check("documented expression matches", watch.matches(packet, state), True)
check("hop comparison rejects lower paths",
      parse_watch("hop<3").matches(packet, state), False)
check("protocol aliases normalize",
      parse_watch("proto:meshcore").matches(packet, state), True)
check("node name lookup works",
      parse_watch("from:walker text:green").matches(packet, state), True)

try:
    parse_watch("__import__('os')")
    invalid = False
except ValueError:
    invalid = True
check("arbitrary expressions are rejected", invalid, True)

tmpdir = tempfile.mkdtemp(prefix="meshtui-watch-")
path = os.path.join(tmpdir, "preferences.json")
prefs = OperatorPreferences(path)
prefs.save_view("meshcore", "public-long", watch.expression)
check("named view round trips", prefs.views("meshcore")["public-long"], watch.expression)
check("named view is protocol scoped", prefs.views("meshtastic"), {})
check("named view can be deleted", prefs.delete_view("meshcore", "public-long"), True)

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
