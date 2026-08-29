"""Node records from untrusted clocks and unidentified senders must not
poison the node table.

MeshCore adverts stamp last_advert with the sender's clock - seen wrong by
days and, once, by 51 years - and a future last-heard renders a negative age
and floats the node above every genuinely recent one, burying the real data.
And "!00000000" (key_to_id's placeholder when an RX-log entry has no pubkey
prefix) must never become a phantom node collecting packet counts.
"""
import sys, time

from meshtui.model import Packet
from meshtui.state import MeshState
from meshtui.service import MeshService

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

state = MeshState()

node = state.upsert_node({"user": {"id": "!aabbccdd"}, "lastHeard": time.time() + 5 * 86400})
check("future lastHeard is clamped to now", node.last_heard <= time.time(), True)

past = time.time() - 3600
node = state.upsert_node({"user": {"id": "!aabbccdd"}, "lastHeard": past})
check("past lastHeard is kept as-is", node.last_heard, past)

try:
    state.upsert_node({"user": {"id": "!00000000"}})
    rejected = False
except ValueError:
    rejected = True
check("placeholder sender id is rejected", rejected, True)
check("no phantom node was created", "!00000000" in state.nodes, False)

service = MeshService(store=None)
packet = Packet(ts=time.time(), from_id="!00000000", to_id="^all",
                portnum="RXLOG_APP", summary="rx 12B", raw={"from": 0})
service.receive_packet(packet)
check("an unattributed packet creates no node", "!00000000" in service.state.nodes, False)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
