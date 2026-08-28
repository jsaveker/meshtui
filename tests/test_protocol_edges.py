"""Production-shaped transport edge cases that the synthetic UI tests miss."""

import asyncio
import sys
import types

from meshtui.app import MeshTUI
from meshtui.meshcore_link import MeshCoreLink, contact_to_node, key_to_id
from meshtui.model import DeliveryStatus, MESHCORE_MAX_PAYLOAD, PeerRef
from meshtui.radio import MeshtasticLink
from meshtui.state import MeshState
from meshtui.widgets.chat import ChatPane


failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class Settings:
    def __init__(self, name):
        self.name = name
        self.psk = b"\x01"


class Channel:
    def __init__(self, role, name):
        self.role = role
        self.settings = Settings(name)


print("Meshtastic sparse channels")
iface = types.SimpleNamespace(localNode=types.SimpleNamespace(channels=[
    Channel(1, "Primary"), Channel(0, ""), Channel(2, "Bots"),
]))
check("real hardware indices retained", MeshtasticLink._channel_names(iface),
      [(0, "Primary"), (2, "Bots")])

state = MeshState()
state.channels = ["Same", "Same"]
check("duplicate positional names keep their own slot", state.channel_name(1), "Same")


print("\nMeshCore production event shapes")
KEY = "2935ec595f468726c747752a62cb04060fc493da511f3c1fd8055b79106b1555"
emitted = []
link = MeshCoreLink(lambda kind, payload: emitted.append((kind, payload)))
link._on_channel_message(types.SimpleNamespace(payload={
    "channel_idx": 12, "text": "bot reply", "sender_timestamp": 1, "SNR": 4.5,
}))
chat = next(p for k, p in emitted if k == "chat")
packet = next(p for k, p in emitted if k == "packet")
check("anonymous channel frame does not invent a node id",
      chat.from_id, "channel:12:anonymous")
check("packet retains real channel", packet.channel, 12)
check("uppercase production SNR retained", packet.snr, 4.5)


print("\nLive contacts and partial updates")
emitted.clear()
link._on_new_contact(types.SimpleNamespace(payload={
    "public_key": KEY, "adv_name": "Mobile", "type": 1,
}))
check("new contact immediately becomes sendable", key_to_id(KEY) in link.contacts, True)
check("durable full key can address an unstored peer",
      link._destination_for(PeerRef("meshcore", "!ffffffff", KEY)), KEY)

st = MeshState()
node = st.upsert_node(contact_to_node({
    "public_key": KEY, "adv_name": "Home Repeater", "type": 2,
}))
st.upsert_node(contact_to_node({"public_key": KEY}))
check("key-only advert preserves name", node.name, "Home Repeater")
check("key-only advert preserves role", node.role, "REPEATER")


print("\nMeshCore ACK and limits")
emitted.clear()
link._pending_acks["deadbeef"] = "local-message-id"
link._on_ack(types.SimpleNamespace(payload={"code": "deadbeef", "trip_time": 123}))
receipt = next(p for k, p in emitted if k == "receipt")
check("hex ACK resolves local message id", receipt.message_id, "local-message-id")
check("hex ACK marks delivered", receipt.status, DeliveryStatus.DELIVERED)

too_long = link.send("x" * (MESHCORE_MAX_PAYLOAD + 1),
                     types.SimpleNamespace(protocol="meshcore", index=0), "long")
check("MeshCore over-limit send fails locally", too_long.status, DeliveryStatus.FAILED)


print("\nSafe rename")
calls = []
link.channel_secrets[5] = bytes.fromhex("00112233445566778899aabbccddeeff")
link.set_channel = lambda index, name, secret=None: calls.append((index, name, secret))
check("ordinary rename accepted", link.rename_channel(5, "Bots"), True)
check("rename passes existing key", calls[-1],
      (5, "Bots", bytes.fromhex("00112233445566778899aabbccddeeff")))
check("hashtag rename rejected rather than rekeying", link.rename_channel(5, "#bots"), False)


class Spy:
    def __init__(self):
        self.sent = []

    def send_text(self, text, dest="^all", channel=0):
        self.sent.append((text, dest, channel))
        return True, 1

    def stop(self):
        pass


async def all_view():
    print("\nAll activity send is fail-closed")
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(150, 40)):
        await asyncio.sleep(1)
        spy = Spy()
        app.link = spy
        app.state.connected = True
        app.state.protocol = "meshcore"
        app.state.channels = [(5, "#bots"), (12, "Ops")]
        app.query_one(ChatPane).set_channels(app.state)
        app.state.active_target = ("all",)
        accepted = app._submit_chat("must not leak to channel zero")
        check("send refused", accepted, False)
        check("nothing transmitted", spy.sent, [])


asyncio.run(all_view())

print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("PASS")
