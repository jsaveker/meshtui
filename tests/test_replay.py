"""A bounded SQLite window replays as a read-only, time-rebased ghost link."""

import os
import struct
import tempfile
import time
from pathlib import Path

from meshtui.model import BROADCAST, ChannelRef, ChatMessage, DeliveryStatus, Packet
from meshtui.replay import (ReplayLink, build_pcap_replay_gateway,
                            build_replay_gateway)
from meshtui.store import Store

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


events = []
packet = Packet(ts=100.0, from_id="!aa000001", to_id=BROADCAST,
                portnum="RXLOG_APP", summary="captured", hops=2,
                raw={"protocol": "meshcore"})
message = ChatMessage(ts=101.0, from_id="!aa000001", from_name="Walker",
                      to_id=BROADCAST, text="hello", channel=2)
link = ReplayLink(lambda kind, payload: events.append((kind, payload)),
                  [packet], [message], [], protocol="meshcore", speed=1000)
started = time.time()
link.start()
time.sleep(0.05)
check("ghost emits connected first", events[0][0], "connected")
check("packet is replayed", any(kind == "packet" for kind, _ in events), True)
check("chat is replayed", any(kind == "chat" for kind, _ in events), True)
replayed = next(payload for kind, payload in events if kind == "packet")
check("old capture is rebased to now", replayed.ts >= started, True)
receipt = link.send("no", ChannelRef("meshcore", 2), "m1")
check("ghost rejects sends", receipt.status, DeliveryStatus.FAILED)
link.stop()

tmpdir = tempfile.mkdtemp(prefix="meshtui-replay-")
path = os.path.join(tmpdir, "mesh.db")
store = Store(path, flush_interval=0.01)
check("capture store opens", store.open(), True)
store.add_packet(packet)
store.add_message(message)
store.close()

source = Store(path)
check("window includes packet", len(source.replay_packets(99, 100.5)), 1)
check("window excludes later chat", len(source.replay_messages(99, 100.5)), 0)
gateway = build_replay_gateway(path, speed=1000, protocol="auto")
check("protocol inferred for ghost", gateway.service.state.protocol, "meshcore")
check("ghost service has no writable store", gateway.service.store, None)


print("classic PCAP and PCAPNG decode supported mesh frames")
from meshtastic.protobuf import mesh_pb2, portnums_pb2


def pcap(path, payload):
    header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 147)
    record = struct.pack("<IIII", 1234, 500000, len(payload), len(payload)) + payload
    Path(path).write_bytes(header + record)


mesh = mesh_pb2.MeshPacket()
setattr(mesh, "from", 0xAA000001)
mesh.to = 0xFFFFFFFF
mesh.id = 77
mesh.channel = 2
mesh.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
mesh.decoded.payload = b"pcap hello"
wrapper = mesh_pb2.FromRadio()
wrapper.packet.CopyFrom(mesh)
serial = b"\x94\xc3" + len(wrapper.SerializeToString()).to_bytes(2, "big") \
    + wrapper.SerializeToString()
classic = os.path.join(tmpdir, "mesh.pcap")
pcap(classic, serial)
pcap_gateway = build_pcap_replay_gateway(classic, speed=1000, protocol="auto")
check("PCAP protocol inferred", pcap_gateway.service.state.protocol, "meshtastic")
check("serial-framed FromRadio decoded", pcap_gateway.link.packets[0].summary,
      '"pcap hello"')
check("PCAP text becomes ghost chat", pcap_gateway.link.messages[0].text, "pcap hello")
check("PCAP source is identified", pcap_gateway.link.source, "pcap")


def block(block_type, body):
    body += b"\0" * ((-len(body)) % 4)
    length = 12 + len(body)
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


section = block(0x0A0D0D0A, b"\x4d\x3c\x2b\x1a" + struct.pack("<HHq", 1, 0, -1))
interface = block(1, struct.pack("<HHI", 147, 0, 65535))
raw_wrapper = wrapper.SerializeToString()
ticks = 2_000_250_000
enhanced = block(6, struct.pack(
    "<IIIII", 0, ticks >> 32, ticks & 0xFFFFFFFF,
    len(raw_wrapper), len(raw_wrapper)) + raw_wrapper)
pcapng = os.path.join(tmpdir, "mesh.pcapng")
Path(pcapng).write_bytes(section + interface + enhanced)
pcapng_gateway = build_pcap_replay_gateway(pcapng, speed=1000, protocol="meshtastic")
check("PCAPNG FromRadio decoded", pcapng_gateway.link.packets[0].packet_id, 77)
check("PCAPNG timestamp decoded", pcapng_gateway.link.packets[0].ts, 2000.25)


room_key = bytes.fromhex("feedface" + "01" * 28)
meshcore_advert = (bytes([0x11, 0x00]) + room_key
                   + (1234).to_bytes(4, "little") + bytes(64)
                   + bytes([0x83]) + b"Training Room")
meshcore_capture = os.path.join(tmpdir, "meshcore.pcap")
pcap(meshcore_capture, meshcore_advert)
meshcore_gateway = build_pcap_replay_gateway(
    meshcore_capture, speed=1000, protocol="meshcore")
check("raw MeshCore PCAP decoded", meshcore_gateway.service.state.protocol, "meshcore")
check("MeshCore advert becomes a node", meshcore_gateway.link.nodes[0]["user"]["longName"],
      "Training Room")

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
