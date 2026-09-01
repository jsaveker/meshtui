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
check("a wildly future lastHeard is unknown, not fresh", node.last_heard, None)

node = state.upsert_node({"user": {"id": "!aabbccdd"}, "lastHeard": time.time() + 60})
check("small future skew is clamped to now",
      node.last_heard is not None and node.last_heard <= time.time(), True)

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

# positions: (0,0) is a GPS-unset sentinel, out-of-range pairs are corrupt
node = state.upsert_node({"user": {"id": "!aabbccdd"},
                          "position": {"latitude": 0.0, "longitude": 0.0}})
check("Null Island is not a position", (node.lat, node.lon), (None, None))
node = state.upsert_node({"user": {"id": "!aabbccdd"},
                          "position": {"latitude": -120.0, "longitude": 40.0}})
check("swapped/corrupt coordinates are rejected", node.lat, None)
node = state.upsert_node({"user": {"id": "!aabbccdd"},
                          "position": {"latitude": 10.3, "longitude": 20.3}})
check("a real position is stored", (node.lat, node.lon), (10.3, 20.3))
node = state.upsert_node({"user": {"id": "!aabbccdd"},
                          "position": {"latitude": 0.0, "longitude": 0.0}})
check("a later sentinel does not erase a real position",
      (node.lat, node.lon), (10.3, 20.3))

service = MeshService(store=None)
packet = Packet(ts=time.time(), from_id="!00000000", to_id="^all",
                portnum="RXLOG_APP", summary="rx 12B", raw={"from": 0})
service.receive_packet(packet)
check("an unattributed packet creates no node", "!00000000" in service.state.nodes, False)

# --- a future last_heard persisted BEFORE hygiene existed must not keep
# resurrecting: the store sweeps it on open, and the restore path refuses
# it (regression: DougsFBGr2's +15h clock outranked the live repeater on
# every restart because MAX() upserts pinned the value forever) ---
import os
import tempfile

from meshtui.state import sane_heard
from meshtui.store import Store

check("sane_heard passes an honest past stamp", sane_heard(1000.0, now=2000.0), 1000.0)
check("sane_heard clamps small skew to now", sane_heard(2100.0, now=2000.0), 2000.0)
check("sane_heard rejects a garbage clock", sane_heard(2000.0 + 54000, now=2000.0), None)
check("sane_heard tolerates junk types", sane_heard("soon"), None)

tmp = os.path.join(tempfile.mkdtemp(prefix="meshtui-hygiene-"), "mesh.db")
store = Store(tmp, flush_interval=0.01)
check("store opens", store.open(), True)
store._conn.execute(
    "INSERT INTO nodes (node_id, num, long_name, role, last_heard)"
    " VALUES ('!bc64ce00', 1, 'Garbage Clock', 'REPEATER', ?)",
    (time.time() + 15 * 3600,))
store._conn.commit()
store.close()
store2 = Store(tmp, flush_interval=0.01)
check("store reopens", store2.open(), True)
row = store2._conn.execute(
    "SELECT last_heard FROM nodes WHERE node_id='!bc64ce00'").fetchone()
check("open() sweeps the future stamp from disk", row[0], None)
store2.close()

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
