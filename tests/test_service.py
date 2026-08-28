"""Durable service and delivery-state field simulations (no radio required)."""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from meshtui.model import ChannelRef, DeliveryStatus, PeerRef, SendReceipt
from meshtui.service import MeshService
from meshtui.store import Store


failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class Link:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.sent = []

    def send(self, text, destination, message_id):
        self.sent.append((text, destination, message_id))
        outcome = self.outcomes.pop(0) if self.outcomes else (DeliveryStatus.SENT, len(self.sent))
        if isinstance(outcome, Exception):
            raise outcome
        status, protocol_id = outcome
        return SendReceipt(message_id, destination, status, protocol_id=protocol_id)

    def stop(self):
        pass


def connected(service, link, protocol="meshtastic"):
    service.attach_link(link)
    service.connected({
        "my_node_id": "!10000001", "my_node_name": "Home", "protocol": protocol,
        "channels": [(0, "Primary"), (12, "#bots")], "device": "fake://home",
    })


with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "mesh.db"
    store = Store(db, flush_interval=0.01)
    check("store opens", store.open(), True)

    print("offline intent survives a home-device restart")
    first = MeshService(store, retry_seconds=0.01)
    destination = PeerRef("meshtastic", "!20000002")
    queued = first.send_message("home to work", destination)
    check("offline message queued", queued.status, DeliveryStatus.QUEUED)
    check("one logical chat row in memory", len(first.state.chat), 1)
    message_id = queued.message_id
    due_before = first.outbox[message_id].next_attempt_ts
    first.process_outbox(time.time() + 1)
    check("offline polling keeps intent immediately due",
          first.outbox[message_id].next_attempt_ts, due_before)
    store.close()

    store = Store(db, flush_interval=0.01)
    check("store reopens", store.open(), True)
    restarted = MeshService(store, retry_seconds=0.01)
    restarted.restore()
    check("outbound intent restored", message_id in restarted.outbox, True)
    check("chat restored once", len(restarted.state.chat), 1)
    link = Link()
    connected(restarted, link)
    receipts = restarted.process_outbox(time.time() + 1)
    check("restart drains queue", receipts[-1].status, DeliveryStatus.SENT)
    protocol_id = receipts[-1].protocol_id
    delivered = restarted.ack_protocol(protocol_id)
    check("protocol ACK marks delivered", delivered.status, DeliveryStatus.DELIVERED)
    stale = restarted.apply_receipt(SendReceipt(
        message_id, destination, DeliveryStatus.FAILED, detail="late callback"))
    check("late callback cannot downgrade delivery", stale.status, DeliveryStatus.DELIVERED)
    check("retry did not duplicate chat", len(restarted.state.chat), 1)

    print("unavailable-node retry and stop gates")
    retry_link = Link([
        RuntimeError("radio unavailable"),
        (DeliveryStatus.SENT, 444),
    ])
    retry_service = MeshService(None, retry_seconds=0.01)
    connected(retry_service, retry_link)
    first_try = retry_service.send_message("retry me", PeerRef("meshtastic", "!30000003"))
    check("first unavailable attempt fails", first_try.status, DeliveryStatus.FAILED)
    outbound = retry_service.outbox[first_try.message_id]
    retry_service.process_outbox((outbound.next_attempt_ts or 0) + 0.1)
    check("second attempt sent", outbound.status, DeliveryStatus.SENT)
    check("exactly two radio attempts", len(retry_link.sent), 2)
    check("still one logical chat message", len(retry_service.state.chat), 1)
    retry_service.ack_protocol(444)
    retry_service.process_outbox(time.time() + 100)
    check("delivered item does not retry", len(retry_link.sent), 2)

    exhausted_link = Link([RuntimeError("down"), RuntimeError("down"),
                           RuntimeError("down")])
    exhausted = MeshService(None, retry_seconds=0.01)
    connected(exhausted, exhausted_link)
    terminal = exhausted.send_message("bounded retry", PeerRef("meshtastic", "!30000004"))
    terminal_item = exhausted.outbox[terminal.message_id]
    for _ in range(4):
        exhausted.process_outbox((terminal_item.next_attempt_ts or time.time()) + 0.1)
    check("retry stops at configured maximum", len(exhausted_link.sent), 3)
    check("exhausted failure is terminal", terminal_item.terminal, True)

    queued_link = Link([(DeliveryStatus.QUEUED, None)])
    recovering = MeshService(None, retry_seconds=0.01)
    connected(recovering, queued_link, "meshcore")
    async_queued = recovering.send_message("async submission", PeerRef("meshcore", "!30000005"))
    check("async submission has crash-recovery deadline",
          recovering.outbox[async_queued.message_id].next_attempt_ts is not None, True)

    expiring = MeshService(None)
    exp = expiring.send_message("expire me", PeerRef("meshcore", "!40000004"), ttl=0.01)
    expiring.process_outbox(time.time() + 1)
    check("offline intent expires", expiring.outbox[exp.message_id].status,
          DeliveryStatus.EXPIRED)

    print("sparse channel address remains a slot, not a list position")
    sparse_link = Link()
    sparse = MeshService(None)
    connected(sparse, sparse_link, "meshcore")
    channel_receipt = sparse.send_message("bot traffic", ChannelRef("meshcore", 12, "#bots"))
    sent_ref = sparse_link.sent[-1][1]
    check("slot 12 reaches the link", sent_ref.index, 12)
    check("channel acceptance is terminal", sparse.outbox[sparse_link.sent[-1][2]].terminal, True)
    check("broadcast ACK cannot imply end-to-end delivery",
          sparse.ack_protocol(channel_receipt.protocol_id), None)
    check("broadcast remains locally sent",
          sparse.outbox[channel_receipt.message_id].status, DeliveryStatus.SENT)
    channel_chat = next(msg for msg in sparse.state.chat
                        if msg.message_id == channel_receipt.message_id)
    check("broadcast chat does not show delivered", channel_chat.acked, False)
    store.close()

    store = Store(db, flush_interval=0.01)
    check("delivery database reopens", store.open(), True)
    historical = MeshService(store).delivery_snapshot(message_id)
    check("terminal delivery remains queryable after restart", historical["status"],
          DeliveryStatus.DELIVERED.value)
    store.close()

    print("pre-feature database migrates in place")
    old_db = Path(tmp) / "old.db"
    conn = sqlite3.connect(old_db)
    conn.execute("CREATE TABLE messages (rowid_ INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                 "from_id TEXT, to_id TEXT, channel INTEGER, text TEXT, outgoing INTEGER, "
                 "packet_id INTEGER, acked INTEGER)")
    conn.commit()
    conn.close()
    old = Store(old_db)
    check("old message table opens", old.open(), True)
    old.close()
    conn = sqlite3.connect(old_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    conn.close()
    check("message id column added", "message_id" in columns, True)
    check("delivery column added", "delivery_status" in columns, True)


print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("PASS")
