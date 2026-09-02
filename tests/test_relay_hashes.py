"""Width-preserving relay attribution and legacy SQLite migration."""

import json
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

from meshtui.app import MeshTUI
from meshtui.events import packet_from_dict, packet_to_dict
from meshtui.meshcore_link import MeshCoreLink, last_path_hash
from meshtui.model import Packet, normalize_relay_hash
from meshtui.pathcalc import PathObservation, analyze
from meshtui.service import MeshService
from meshtui.state import MeshState
from meshtui.store import LAST_OBSERVER, Store, state_ts_key
from meshtui.widgets.relays import RelayView, display_relay_share, relay_label


failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


def rx_packet(**overrides):
    payload = {
        "payload_typename": "DATA", "payload_length": 20,
        "path": "aa01bc20", "path_len": 2, "path_hash_size": 2,
    }
    payload.update(overrides)
    events = []
    MeshCoreLink(lambda kind, value: events.append((kind, value)),
                 port="/dev/null")._on_rx_log(types.SimpleNamespace(payload=payload))
    return next(value for kind, value in events if kind == "packet")


print("wire parsing retains exact width")
packet = rx_packet()
check("two-byte final hash survives", (packet.relay_node, packet.relay_hash),
      (0xBC, "bc20"))
check("three-byte final hash survives",
      rx_packet(path="010203bc20c2", path_len=2, path_hash_size=3).relay_hash,
      "bc20c2")
check("missing width is inferred from path length",
      rx_packet(path="aa01bc20", path_len=2, path_hash_size=None).relay_hash,
      "bc20")
check("leading zero and width survive",
      rx_packet(path="abcd00bc", path_len=2, path_hash_size=2).relay_hash,
      "00bc")
check("malformed path cannot name a relay",
      rx_packet(path="aabbcc", path_len=2, path_hash_size=2).relay_hash, None)
check("malformed inferred path cannot name a relay",
      rx_packet(path="aabbcc", path_len=2, path_hash_size=None).relay_hash, None)
check("non-hex inferred path cannot name a relay",
      rx_packet(path="aazz", path_len=2, path_hash_size=None).relay_hash, None)
check("odd-length inferred path cannot be truncated into a relay",
      last_path_hash("abc", 1, None), None)
check("punctuated inferred path cannot be truncated into a relay",
      last_path_hash("aa:", 1, None), None)
check("zero-hop path cannot contain a relay",
      last_path_hash("bc20", 0, 2), None)
check("canonicalizer preserves a leading zero", normalize_relay_hash("00BC"), "00bc")

wire = packet_to_dict(packet)
check("new gateway wire events retain the full hash",
      packet_from_dict(wire).relay_hash, "bc20")
wire.pop("relay_hash")
check("new clients recover full hashes from old gateway events",
      packet_from_dict(wire).relay_hash, "bc20")


print("\nstate and rendering keep exact and ambiguous evidence separate")
state = MeshState()
state.protocol = "meshcore"
near_repeater = state.upsert_node({"user": {
    "id": "!bc20c203", "longName": "Solar Ridge", "shortName": "SRDG",
    "role": "REPEATER"}, "lastHeard": 100.0})
far_repeater = state.upsert_node({"user": {
    "id": "!bc64ced5", "longName": "Far Repeater", "shortName": "FARR",
    "role": "REPEATER"}, "lastHeard": 200.0})


def add_relay(token, count, origin):
    for offset in range(count):
        state.add_packet(Packet(
            ts=1000.0 + offset, from_id=origin, to_id="^all",
            portnum="RXLOG_APP", summary="rf", hops=1, snr=5.0 + offset,
            relay_node=int(token[:2], 16), relay_hash=token,
        ))


add_relay("bc", 1, "!origin001")
add_relay("bc20", 2, "!origin002")
add_relay("bc20c2", 3, "!origin003")
add_relay("bc64", 4, "!origin004")
check("raw aggregates retain every observed width",
      sorted(state.relays), ["bc", "bc20", "bc20c2", "bc64"])
check("full-width origin edges remain separate",
      sorted(token for _, token in state.relay_edges),
      ["bc", "bc20", "bc20c2", "bc64"])
check("exact two-byte hash resolves", relay_label(state, "bc20")[0].plain.startswith("SRDG"),
      True)
ambiguous_before = relay_label(state, "bc")[0].plain
check("one-byte collision is neutral",
      ("Solar Ridge" not in ambiguous_before and "Far Repeater" not in ambiguous_before
       and "2 possible" in ambiguous_before), True)
near_repeater.last_heard, far_repeater.last_heard = 300.0, 50.0
check("recency cannot rename a collision", relay_label(state, "bc")[0].plain,
      ambiguous_before)

service = MeshService(store=None)
service.state = state
check("chat repeat label is neutral too", service._repeater_label("bc"),
      "0xbc (2 possible repeaters)")
check("chat exact hash names its repeater", service._repeater_label("bc20"),
      "Solar Ridge")
analysis = analyze(state, PathObservation(ts=1.0, kind="channel", path="bc", hops=1))
check("path analysis does not attach a node to a collision",
      (analysis.hops[0].node, analysis.hops[0].ambiguous), (None, True))

display = display_relay_share(state)
check("exact aliases coalesce only for display", len(display), 3)
coalesced = next(relay for relay, _ in display if relay.key == "bc20c2")
check("two- and three-byte aliases combine", coalesced.packets, 5)
check("ambiguous byte remains its own row",
      next(relay.packets for relay, _ in display if relay.key == "bc"), 1)
check("storage remains distinct after display", len(state.relays), 4)


def relay_fixture(nodes, buckets):
    fixture = MeshState()
    fixture.protocol = "meshcore"
    for node_id, name, role in nodes:
        fixture.upsert_node({"user": {
            "id": node_id, "longName": name, "shortName": name[:4], "role": role,
        }})
    for token, count in buckets:
        for offset in range(count):
            fixture.add_packet(Packet(
                ts=2000.0 + offset, from_id=f"!origin{token}", to_id="^all",
                portnum="RXLOG_APP", summary="rf", hops=1,
                relay_node=int(token[:2], 16), relay_hash=token,
            ))
    return fixture


real_mix = relay_fixture(
    [("!bc20c203", "Solar Ridge", "REPEATER"),
     ("!bc64ced5", "Far Repeater", "REPEATER")],
    [("bc20", 133), ("bc20c2", 14), ("bc", 41)],
)
real_verdict = RelayView(real_mix)._verdict().plain
check("exact plus ambiguous evidence is not called two relays",
      "2 relays" in real_verdict, False)
check("exact plus ambiguous evidence cannot claim redundancy",
      "no single point" in real_verdict, False)
check("mixed verdict reports its confidence boundary",
      ("78.2% is confidently attributed to Solar Ridge" in real_verdict
       and "21.8% remains unresolved" in real_verdict), True)

one_relay = relay_fixture(
    [("!bc20c203", "Solar Ridge", "REPEATER")],
    [("bc20", 2), ("bc20c2", 3)],
)
one_verdict = RelayView(one_relay)._verdict().plain
check("exact aliases produce a one-relay dependency warning",
      "1 relay carries 100.0%" in one_verdict, True)
check("exact aliases are not presented as two relays", "2 relays" in one_verdict, False)

balanced = relay_fixture(
    [("!aa110001", "Alpha Relay", "REPEATER"),
     ("!bb220002", "Bravo Relay", "REPEATER"),
     ("!cc330003", "Charlie Relay", "REPEATER")],
    [("aa11", 10), ("bb22", 10), ("cc33", 10)],
)
check("three balanced exact relays may claim redundancy",
      "no single point of failure" in RelayView(balanced)._verdict().plain, True)

partly_unknown = relay_fixture(
    [("!aa110001", "Alpha Relay", "REPEATER"),
     ("!bb220002", "Bravo Relay", "REPEATER")],
    [("aa11", 10), ("bb22", 10), ("dd44", 1)],
)
partial_verdict = RelayView(partly_unknown)._verdict().plain
check("an unknown bucket suppresses a redundancy claim",
      "no single point" in partial_verdict, False)
check("an unknown bucket is reported as unresolved",
      "remains unresolved" in partial_verdict, True)

unknown_only = relay_fixture([], [("dd44", 3)])
check("unknown-only evidence does not invent a dependency",
      "dependency cannot yet be determined" in RelayView(unknown_only)._verdict().plain,
      True)

blank_role = MeshState()
blank_role.protocol = "meshcore"
blank_role.upsert_node({"user": {"id": "!ee110001", "longName": "Known Relay",
                                      "role": "REPEATER"}})
blank_role.upsert_node({"user": {"id": "!ee220002", "longName": "Unknown Role",
                                      "role": ""}})
blank_label, blank_ambiguous = relay_label(blank_role, "ee")
check("a blank role cannot make a collision look resolved",
      (blank_ambiguous, "Known Relay" in blank_label.plain), (True, False))

meshtastic = MeshState()
meshtastic.protocol = "meshtastic"
meshtastic.upsert_node({"num": 0x12345691, "user": {"id": "!12345691"}})
meshtastic.add_packet(Packet(ts=1.0, from_id="!origin", to_id="^all",
                              portnum="POSITION_APP", summary="pos", hops=1,
                              relay_node=0x91))
check("Meshtastic remains a one-byte aggregate", sorted(meshtastic.relays), ["91"])
check("legacy integer lookup still works", meshtastic.relays[0x91].packets, 1)
check("Meshtastic resolution still uses the low byte",
      [node.node_id for node in meshtastic.resolve_relay(0x91)], ["!12345691"])


print("\npacket replay is protocol-safe")
meshcore_row = {
    "ts": 1, "from_id": "!origin", "to_id": "^all", "portnum": "RXLOG_APP",
    "summary": "rf", "raw": json.dumps({"path": "abcd00bc", "path_len": 2,
                                           "path_hash_size": 2}),
}
check("old MeshCore packet rows recover their full token",
      MeshTUI._packet_from_row(meshcore_row).relay_hash, "00bc")
meshtastic_row = {
    "ts": 1, "from_id": "!origin", "to_id": "^all", "portnum": "POSITION_APP",
    "summary": "pos", "raw": json.dumps({"relayNode": 0x91, "path": "bc20",
                                            "path_len": 1, "path_hash_size": 2}),
}
replayed = MeshTUI._packet_from_row(meshtastic_row)
check("an unrelated Meshtastic path is not reinterpreted",
      (replayed.relay_node, replayed.relay_hash), (0x91, None))


def create_legacy_scoped(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE relays (local_node TEXT NOT NULL, byte INTEGER,"
                 " packets INTEGER, origins TEXT, first_seen REAL, last_seen REAL,"
                 " snr_sum REAL, snr_n INTEGER, PRIMARY KEY(local_node, byte))")
    conn.execute("CREATE TABLE relay_edges (local_node TEXT NOT NULL, origin TEXT,"
                 " relay_byte INTEGER, packets INTEGER,"
                 " PRIMARY KEY(local_node, origin, relay_byte))")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO relays VALUES (?,?,?,?,?,?,?,?)", [
        ("!observer-a", 0x00, 7, '["!one"]', 10, 20, 21.0, 3),
        ("!observer-b", 0xBC, 11, '["!two"]', 30, 40, 44.0, 4),
    ])
    conn.executemany("INSERT INTO relay_edges VALUES (?,?,?,?)", [
        ("!observer-a", "!one", 0x00, 7),
        ("!observer-b", "!two", 0xBC, 11),
    ])
    conn.execute("INSERT INTO meta VALUES (?, ?)",
                 (state_ts_key("!observer-b"), json.dumps(1234.5)))
    conn.commit()
    conn.close()


print("\nlegacy databases migrate non-destructively and idempotently")
with tempfile.TemporaryDirectory() as tmp:
    scoped_db = Path(tmp) / "scoped.db"
    create_legacy_scoped(scoped_db)
    for _ in range(2):
        migrated = Store(scoped_db, flush_interval=0.01)
        check("scoped legacy database opens", migrated.open(), True)
        migrated.close()
    conn = sqlite3.connect(scoped_db)
    hash_rows = conn.execute(
        "SELECT local_node, relay_hash, packets FROM relay_hashes"
        " ORDER BY local_node").fetchall()
    migrated_detail = conn.execute(
        "SELECT origins, first_seen, last_seen, snr_sum, snr_n FROM relay_hashes"
        " WHERE local_node='!observer-a' AND relay_hash='00'").fetchone()
    edge_rows = conn.execute(
        "SELECT local_node, origin, relay_hash, packets FROM relay_hash_edges"
        " ORDER BY local_node").fetchall()
    legacy_rows = conn.execute("SELECT COUNT(*) FROM relays").fetchone()[0]
    stamp = conn.execute("SELECT value FROM meta WHERE key=?",
                         (state_ts_key("!observer-b"),)).fetchone()[0]
    conn.close()
    check("legacy bytes seed canonical two-char hashes once", hash_rows,
          [("!observer-a", "00", 7), ("!observer-b", "bc", 11)])
    check("legacy aggregate detail is unchanged", migrated_detail,
          ('["!one"]', 10.0, 20.0, 21.0, 3))
    check("legacy full edge counts and scopes survive", edge_rows,
          [("!observer-a", "!one", "00", 7),
           ("!observer-b", "!two", "bc", 11)])
    check("legacy rollback tables remain intact", legacy_rows, 2)
    check("migration leaves state timestamp untouched", json.loads(stamp), 1234.5)

    loaded = Store(scoped_db)
    check("migrated database reopens for reads", loaded.open(), True)
    loaded.local_node = "!observer-a"
    check("observer A reads only its aggregate",
          [(row["relay_hash"], row["packets"]) for row in loaded.load_relays()],
          [("00", 7)])
    loaded.local_node = "!observer-b"
    check("observer B reads only its aggregate",
          [(row["relay_hash"], row["packets"]) for row in loaded.load_relays()],
          [("bc", 11)])
    loaded.close()

    unscoped_db = Path(tmp) / "unscoped.db"
    conn = sqlite3.connect(unscoped_db)
    conn.execute("CREATE TABLE relays (byte INTEGER PRIMARY KEY, packets INTEGER,"
                 " origins TEXT, first_seen REAL, last_seen REAL, snr_sum REAL, snr_n INTEGER)")
    conn.execute("CREATE TABLE relay_edges (origin TEXT, relay_byte INTEGER, packets INTEGER,"
                 " PRIMARY KEY(origin, relay_byte))")
    conn.execute("CREATE TABLE messages (rowid_ INTEGER PRIMARY KEY, ts REAL NOT NULL,"
                 " from_id TEXT, to_id TEXT, channel INTEGER, text TEXT, outgoing INTEGER,"
                 " packet_id INTEGER, acked INTEGER)")
    conn.execute("INSERT INTO messages VALUES (1,1,'!legacy-radio','^all',0,'x',1,1,0)")
    conn.execute("INSERT INTO relays VALUES (188,9,'[\"!old\"]',1,2,3,1)")
    conn.execute("INSERT INTO relay_edges VALUES ('!old',188,9)")
    conn.commit()
    conn.close()
    old = Store(unscoped_db)
    check("unscoped legacy database opens", old.open(), True)
    old.local_node = "!legacy-radio"
    check("unscoped rows inherit the inferred observer",
          [(row["relay_hash"], row["packets"]) for row in old.load_relays()],
          [("bc", 9)])
    old.close()


print("\nnew aggregates, timestamps, and observer switches survive restarts")
with tempfile.TemporaryDirectory() as tmp:
    exact_db = Path(tmp) / "exact.db"
    exact = Store(exact_db, flush_interval=0.01)
    check("exact-width database opens", exact.open(), True)
    exact.local_node = "!one-radio"
    for index, token in enumerate(("bc", "bc20", "bc20c2", "bc64"), start=1):
        exact.save_relay(token, index, {f"!origin-{index}"}, index, index + 1,
                         float(index * 2), index)
        exact.save_relay_edge(f"!origin-{index}", token, index)
    exact.add_packet(Packet(ts=10, from_id="!origin", to_id="^all",
                            portnum="RXLOG_APP", summary="rf", hops=1,
                            relay_node=0x00, relay_hash="00bc"))
    exact.set_meta(LAST_OBSERVER, "!one-radio")
    exact.close()

    exact = Store(exact_db, flush_interval=0.01)
    check("exact-width database reopens", exact.open(), True)
    exact.local_node = "!one-radio"
    exact_rows = exact.load_relays()
    exact_edges = exact.load_relay_edges()
    exact_packets = exact.recent_packets()
    check("one observer retains four distinct hash widths",
          sorted(row["relay_hash"] for row in exact_rows),
          ["bc", "bc20", "bc20c2", "bc64"])
    check("relay reads keep the legacy first-byte field",
          sorted(row["byte"] for row in exact_rows), [0xBC] * 4)
    check("one observer relay total is exact",
          sum(row["packets"] for row in exact_rows), 10)
    check("one observer retains four exact edges",
          sorted(token for _, token, _ in exact_edges),
          ["bc", "bc20", "bc20c2", "bc64"])
    check("packet rows persist a leading-zero full hash",
          exact_packets[-1]["relay_hash"], "00bc")
    exact_service = MeshService(exact)
    exact_service.restore()
    exact_service.persist_snapshot()
    exact_service.persist_snapshot()
    exact.close()
    exact = Store(exact_db)
    check("double-persisted database reopens", exact.open(), True)
    exact.local_node = "!one-radio"
    check("repeated snapshots do not inflate exact totals",
          sum(row["packets"] for row in exact.load_relays()), 10)
    exact.close()

    db = Path(tmp) / "mesh.db"
    store = Store(db, flush_interval=0.01)
    check("new database opens", store.open(), True)
    store.local_node = "!radio-a"
    store.save_relay("00bc", 5, {"!origin-a"}, 1, 2, 15.0, 3)
    store.save_relay_edge("!origin-a", "00bc", 5)
    store.set_meta(state_ts_key("!radio-a"), 222.0)
    store.local_node = "!radio-b"
    store.save_relay("bc64", 8, {"!origin-b"}, 3, 4, 32.0, 4)
    store.save_relay_edge("!origin-b", "bc64", 8)
    store.set_meta(state_ts_key("!radio-b"), 333.0)
    store.set_meta(LAST_OBSERVER, "!radio-b")
    store.close()

    restarted_store = Store(db, flush_interval=0.01)
    check("aggregate database reopens", restarted_store.open(), True)
    service = MeshService(restarted_store)
    service.restore()
    check("last observer aggregate restores",
          (sorted(service.state.relays), service.state.last_packet_ts), (["bc64"], 333.0))
    service.persist_snapshot()
    restarted_store.close()

    conn = sqlite3.connect(db)
    stamp = json.loads(conn.execute("SELECT value FROM meta WHERE key=?",
                                    (state_ts_key("!radio-b"),)).fetchone()[0])
    conn.execute("DELETE FROM packets")
    conn.commit()
    conn.close()
    check("quiet restart does not reset the fold watermark", stamp, 333.0)

    switched_store = Store(db, flush_interval=0.01)
    check("pruned database still opens", switched_store.open(), True)
    switched = MeshService(switched_store)
    switched.restore()
    switched.connected({"my_node_id": "!radio-a", "my_node_name": "A",
                        "protocol": "meshcore", "channels": [(0, "Public")]})
    check("switching observer restores only that radio's exact hashes",
          (sorted(switched.state.relays), switched.state.last_packet_ts),
          (["00bc"], 222.0))
    check("full-hash edge survives packet pruning",
          dict(switched.state.relay_edges), {("!origin-a", "00bc"): 5})
    switched_store.close()


print()
if failures:
    print(f"FAIL: {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("PASS")
