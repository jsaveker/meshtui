"""Gateway event streaming and the attached-TUI GatewayLink."""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

from meshtui.events import event_from_wire, event_to_wire
from meshtui.gateway import Gateway, GatewayLink, request_gateway
from meshtui.model import (BROADCAST, ChannelRef, ChatMessage, DeliveryStatus,
                           Packet, PeerRef, SendReceipt)
from meshtui.service import MeshService

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FieldLink:
    """Fake radio: connects instantly, remembers what it was asked to send."""

    def __init__(self, service):
        self.service = service
        self.sent = []
        self.stopped = False

    def start(self):
        self.service.handle_event("connected", {
            "my_node_id": "!10000001", "my_node_name": "Home Gateway",
            "protocol": "meshtastic", "device": "fake://home",
            "channels": [(0, "Primary"), (12, "#bots")],
        })

    def send(self, text, destination, message_id):
        self.sent.append((text, destination, message_id))
        return SendReceipt(message_id, destination, DeliveryStatus.SENT,
                           protocol_id=9000 + len(self.sent))

    def stop(self):
        self.stopped = True


print("wire encoding survives the round trip")
packet = Packet(ts=1000.5, from_id="!20000002", to_id=BROADCAST, portnum="TEXT_MESSAGE_APP",
                summary='"hi"', snr=4.5, packet_id=77, raw={"from": 0x20000002})
kind, back = event_from_wire(json.loads(json.dumps(event_to_wire("packet", packet))))
check("packet kind", kind, "packet")
check("packet fields", (back.ts, back.from_id, back.packet_id, back.raw),
      (1000.5, "!20000002", 77, {"from": 0x20000002}))

message = ChatMessage(ts=1001.0, from_id="!10000001", from_name="you", to_id=BROADCAST,
                      text="on air", channel=0, outgoing=True, message_id="m1",
                      delivery_status="sent", repeated_by={"rpt-a", "rpt-b"})
kind, back = event_from_wire(json.loads(json.dumps(event_to_wire("chat", message))))
check("chat kind", kind, "chat")
check("chat fields", (back.message_id, back.text, back.repeated_by),
      ("m1", "on air", {"rpt-a", "rpt-b"}))

receipt = SendReceipt("m1", PeerRef("meshtastic", "!20000002", None),
                      DeliveryStatus.DELIVERED, protocol_id=9001, detail="acked")
kind, back = event_from_wire(json.loads(json.dumps(event_to_wire("receipt", receipt))))
check("receipt kind", kind, "receipt")
check("receipt fields", (back.message_id, back.status, back.destination),
      ("m1", DeliveryStatus.DELIVERED, PeerRef("meshtastic", "!20000002", None)))
receipt = SendReceipt("m2", ChannelRef("meshtastic", 12, "#bots"), DeliveryStatus.SENT)
kind, back = event_from_wire(json.loads(json.dumps(event_to_wire("receipt", receipt))))
check("channel destination survives", back.destination, ChannelRef("meshtastic", 12, "#bots"))

kind, back = event_from_wire(json.loads(json.dumps(
    event_to_wire("mc_channels", [(0, "Public"), (3, "ops")]))))
check("mc_channels tuples restored", back, [(0, "Public"), (3, "ops")])
kind, back = event_from_wire(json.loads(json.dumps(
    event_to_wire("mc_login", ("!30000003", True)))))
check("mc_login unpacks as a pair", back, ("!30000003", True))
check("ack events are not broadcast", event_to_wire("ack", 9001), None)
check("mc_repeat events are not broadcast", event_to_wire("mc_repeat", message), None)


with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    print("subscribe streams a snapshot, then live events")
    socket_path = Path(tmp) / "gateway.sock"
    service = MeshService(None, retry_seconds=0.01)
    link = FieldLink(service)
    gateway = Gateway(service, link, socket_path, reconnect_seconds=0.1)
    gateway.start()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    wait_for(lambda: service.state.connected)
    service.handle_event("node", {"num": 0x20000002, "user": {
        "id": "!20000002", "longName": "Field Mobile", "shortName": "FLD"}})
    service.handle_event("chat", ChatMessage(
        ts=time.time(), from_id="!20000002", from_name="", to_id="!10000001",
        text="history line", channel=-1))

    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(3.0)
    raw.connect(str(socket_path))
    raw.sendall(b'{"command":"subscribe"}\n')
    reader = raw.makefile("rb")
    header = json.loads(reader.readline())
    check("subscribe header ok", (header["ok"], header["connected"], header["node_id"]),
          (True, True, "!10000001"))
    snapshot = {}
    got_connected = None
    # connected + 2 nodes (self and Field Mobile) + 1 chat message
    for _ in range(4):
        event = json.loads(reader.readline())
        snapshot.setdefault(event["event"], []).append(event["payload"])
    check("snapshot shape", {k: len(v) for k, v in sorted(snapshot.items())},
          {"chat": 1, "connected": 1, "node": 2})
    check("snapshot channels", snapshot["connected"][0]["channels"],
          [[0, "Primary"], [12, "#bots"]])
    check("snapshot node upserts by id",
          snapshot["node"][-1]["user"]["id"] in ("!10000001", "!20000002"), True)
    check("snapshot chat text", snapshot["chat"][0]["text"], "history line")

    service.handle_event("chat", ChatMessage(
        ts=time.time(), from_id="!20000002", from_name="", to_id="!10000001",
        text="live line", channel=-1))
    event = json.loads(reader.readline())
    check("live chat streamed", (event["event"], event["payload"]["text"]),
          ("chat", "live line"))
    raw.close()

    print("GatewayLink turns the stream back into emit() calls")
    events = []
    seen = threading.Condition()

    def emit(kind, payload):
        with seen:
            events.append((kind, payload))
            seen.notify_all()

    def kinds():
        return [k for k, _ in events]

    tui_link = GatewayLink(emit, socket_path, reconnect_seconds=0.1)
    tui_link.start()
    wait_for(lambda: "chat" in kinds() and "connected" in kinds())
    check("client got connected", "connected" in kinds(), True)
    check("client got the node table",
          any(k == "node" and p["user"]["id"] == "!20000002" for k, p in events), True)
    check("client replayed chat history",
          [p.text for k, p in events if k == "chat"], ["history line", "live line"])

    service.handle_event("packet", packet)
    wait_for(lambda: "packet" in kinds())
    check("client got the live packet",
          [p.packet_id for k, p in events if k == "packet"], [77])

    print("sending through the link is idempotent and echoes as an update, not a duplicate")
    destination = ChannelRef("meshtastic", 12, "#bots")
    first = tui_link.send("hello from the couch", destination, "tui-msg-1")
    check("send accepted", (first.status, first.message_id),
          (DeliveryStatus.SENT, "tui-msg-1"))
    check("radio saw exactly one transmission", [s[0] for s in link.sent],
          ["hello from the couch"])
    again = tui_link.send("hello from the couch", destination, "tui-msg-1")
    check("resend does not retransmit", len(link.sent), 1)
    check("resend reports existing status", again.status, DeliveryStatus.SENT)
    check("gateway recorded the chat once",
          [m.text for m in service.state.chat if m.message_id == "tui-msg-1"],
          ["hello from the couch"])
    wait_for(lambda: any(k == "chat_update" and p.message_id == "tui-msg-1"
                         for k, p in events))
    check("own send echoed as chat_update",
          any(k == "chat_update" and p.message_id == "tui-msg-1" for k, p in events), True)
    check("own send never echoed as new chat",
          any(k == "chat" and p.message_id == "tui-msg-1" for k, p in events), False)

    print("another client's send arrives as chat; a reconnect replays nothing twice")
    result = request_gateway({"command": "send", "kind": "channel", "channel": 12,
                              "text": "sent from the phone"}, socket_path)
    check("cli-style send ok", result["ok"], True)
    wait_for(lambda: any(k == "chat" and p.text == "sent from the phone" for k, p in events))
    check("other client's message rendered",
          any(k == "chat" and p.text == "sent from the phone" for k, p in events), True)

    print("a client that hangs up before reading its reply is not an error")
    import struct
    rude = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    rude.connect(str(socket_path))
    # SO_LINGER 0 -> RST on close, so the gateway's reply write fails hard.
    rude.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    rude.sendall(b'{"command": "paths"}\n')
    rude.close()
    time.sleep(0.3)
    result = request_gateway({"command": "send", "kind": "channel", "channel": 12,
                              "text": "after the rude client"}, socket_path)
    check("gateway still answers after a client hangup", result["ok"], True)

    tui_link.stop()
    before_chat = [p.text for k, p in events if k == "chat"]
    before_packets = [p.packet_id for k, p in events if k == "packet"]
    tui_link.start()
    wait_for(lambda: kinds().count("connected") >= 2)
    time.sleep(0.2)  # let the snapshot replay finish
    check("reconnect adds no duplicate chat",
          [p.text for k, p in events if k == "chat"], before_chat)
    check("reconnect adds no duplicate packets",
          [p.packet_id for k, p in events if k == "packet"], before_packets)
    tui_link.stop()

    gateway.stop()
    check("radio released", link.stopped, True)
    check("socket removed", socket_path.exists(), False)

# --- plain `meshtui` attaches to a live gateway instead of fighting it
# for the serial port (dual access has wedged the radio's USB stack) ---
import argparse
import socket as socket_mod
from unittest import mock

from meshtui import cli as cli_mod
from meshtui import gateway as gateway_mod


def _args(**overrides):
    base = {"gateway": None, "port": None, "host": None, "demo": False}
    base.update(overrides)
    return argparse.Namespace(**base)


probe_path = Path(tempfile.mkdtemp(prefix="meshtui-attach-")) / "gw.sock"
with mock.patch.object(gateway_mod, "default_socket_path", lambda: probe_path):
    check("no socket file -> open the radio directly",
          cli_mod.should_auto_attach(_args()), False)
    probe_path.touch()
    check("a stale socket nobody answers -> open the radio directly",
          cli_mod.should_auto_attach(_args()), False)
    probe_path.unlink()
    server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    server.bind(str(probe_path))
    server.listen(1)
    check("a live gateway socket -> attach to it",
          cli_mod.should_auto_attach(_args()), True)
    check("an explicit --port still wins",
          cli_mod.should_auto_attach(_args(port="/dev/ttyACM0")), False)
    check("an explicit --gateway is left alone",
          cli_mod.should_auto_attach(_args(gateway="")), False)
    check("--demo still wins", cli_mod.should_auto_attach(_args(demo=True)), False)
    server.close()

print()
if failures:
    print(f"{len(failures)} failure(s): {failures}")
    raise SystemExit(1)
print("all gateway stream checks passed")
