"""Authenticated repeater neighbor pulls merge into normalized graph state."""

from meshtui.service import MeshService

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


service = MeshService(None)
state = service.state
state.upsert_node({"user": {"id": "!aa000001", "longName": "Ridge",
                             "role": "REPEATER"}})
state.upsert_node({"user": {"id": "!4c000001", "longName": "Hill",
                             "role": "REPEATER"}})
service.handle_event("mc_neighbours", (
    "!aa000001", [{"pubkey": "4c000001", "snr": -3.5, "secs_ago": 12}]))
edge = state.neighbor_edges[("!aa000001", "!4c000001")]
check("neighbor prefix resolves to known node", edge.target_id, "!4c000001")
check("neighbor SNR is retained", edge.snr, -3.5)
check("neighbor report age is retained", edge.last_seen is not None, True)

service.handle_event("mc_neighbours", (
    "!aa000001", [{"pubkey": "deadbe", "snr": 1.0}]))
unknown = state.neighbor_edges[("!aa000001", "prefix:deadbe")]
check("unknown prefix remains explicit", unknown.target_id, "prefix:deadbe")

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
