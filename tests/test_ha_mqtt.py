"""Home Assistant MQTT export and allowlisted companion-bot simulations."""

import json
import sys
import tempfile
from pathlib import Path

from meshtui.bot import TelemetryBot
from meshtui.ha_mqtt import HomeAssistantMQTT, MQTTConfig
from meshtui.model import (BROADCAST, ChatMessage, DeliveryStatus, Node, Packet,
                           PeerRef, SendReceipt, payload_bytes)
from meshtui.notifications import Notification
from meshtui.service import MeshService
from meshtui.store import Store


failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class PublishResult:
    rc = 0

    def wait_for_publish(self, timeout=None):
        return True


class FakeMQTT:
    def __init__(self):
        self.on_connect = None
        self.on_disconnect = None
        self.published = []
        self.subscribed = []
        self.auth = None
        self.will = None
        self.address = None
        self.tls = None
        self.disconnected = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive=60):
        self.address = (host, port, keepalive)

    def loop_start(self):
        self.on_connect(self, None, {}, 0, None)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return PublishResult()

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        pass


class Link:
    def __init__(self):
        self.sent = []

    def send(self, text, destination, message_id):
        self.sent.append((text, destination, message_id))
        return SendReceipt(message_id, destination, DeliveryStatus.SENT,
                           protocol_id=len(self.sent))

    def stop(self):
        pass


now = 2_000_000_000.0
service = MeshService(None)
service.state.protocol = "meshcore"
service.state.connected = True
service.state.my_node_id = "!10000001"
service.state.my_node_name = "Gateway"
service.state.radio_info = {"noise_floor": -114, "airtime": 3.5}
node = Node(
    num=0x20000002, node_id="!20000002", long_name="Ridge Sensor", short_name="RDGE",
    hw_model="portable", snr=7.25, rssi=-101, hops=2, battery=87, voltage=4.08,
    ch_util=12.5, air_util=1.75, uptime=12345, lat=0.25, lon=0.75, alt=210,
    last_heard=now - 30, packets=42,
)
node.snr_history.extend((3.0, 5.0, 7.25))
node.env = {"temperature": 23.5, "relativeHumidity": 48.0}
node.local_stats = {"numRx": 120, "airtime": 2.5}
service.state.nodes[node.node_id] = node


print("MQTT export is retained, discoverable, and private by default")
fake = FakeMQTT()
config = MQTTConfig(
    host="mqtt.example.invalid", port=1883, username="mesh",
    password="not-printed", gateway_id="field station", refresh_seconds=999,
)
bridge = HomeAssistantMQTT(service, config, client=fake, clock=lambda: now)
bridge.start()
check("broker address comes from configuration", fake.address,
      ("mqtt.example.invalid", 1883, 60))
check("credentials handed to client", fake.auth, ("mesh", "not-printed"))
check("password hidden from config repr", "not-printed" in repr(config), False)
check("last will is retained offline", fake.will,
      ("meshtui/field_station/availability", "offline", 0, True))

node_state_rows = [row for row in fake.published
                   if row[0].endswith("/nodes/20000002/state")]
check("node state published", bool(node_state_rows), True)
state = json.loads(node_state_rows[-1][1])
check("signal and telemetry normalized", (state["snr"], state["battery"],
                                            state["environment_temperature"]),
      (7.25, 87, 23.5))
check("age calculated at publish time", state["age_seconds"], 30.0)
check("coordinates excluded by default", "latitude" in state, False)
check("SNR history exported", state["snr_history"], [3.0, 5.0, 7.25])
check("all state/discovery publications retained",
      all(row[3] for row in fake.published), True)
topics = {row[0] for row in fake.published}
check("Home Assistant battery discovery exists",
      "homeassistant/sensor/meshtui_field_station_20000002_battery/config" in topics, True)
check("dynamic environment discovery exists",
      "homeassistant/sensor/meshtui_field_station_20000002_environment_temperature/config"
      in topics, True)
check("gateway radio discovery exists",
      "homeassistant/binary_sensor/meshtui_field_station_gateway_connected/config"
      in topics, True)
check("bridge status does not expose credentials",
      "not-printed" in json.dumps(bridge.status()), False)

packet = Packet(now, node.node_id, BROADCAST, "TELEMETRY_APP", "fresh", snr=8.0)
before = len(node_state_rows)
bridge.handle_event("packet", packet)
after = sum(row[0].endswith("/nodes/20000002/state") for row in fake.published)
check("live packet refreshes its node state", after, before + 1)
bridge.notify(Notification("Mesh node appeared", "Ridge is active",
                           "node_appeared", {"node_id": node.node_id}))
event_rows = [row for row in fake.published if row[0].endswith("/events")]
check("HA automation event published", bool(event_rows), True)
check("HA automation event is not retained", event_rows[-1][3], False)
check("HA automation event is generic",
      json.loads(event_rows[-1][1])["event_type"], "node_appeared")
bridge.close()
check("clean shutdown publishes retained offline",
      fake.published[-1][:2], ("meshtui/field_station/availability", "offline"))
check("client disconnected", fake.disconnected, True)


print("coordinates require a separate explicit opt-in")
position_client = FakeMQTT()
position_bridge = HomeAssistantMQTT(
    service,
    MQTTConfig(host="broker", gateway_id="position-test", include_position=True,
               refresh_seconds=999),
    client=position_client, clock=lambda: now,
)
position_bridge.start()
position_state = json.loads(next(
    payload for topic, payload, _, _ in reversed(position_client.published)
    if topic.endswith("/nodes/20000002/state")))
check("opt-in includes position", (position_state["latitude"], position_state["longitude"]),
      (0.25, 0.75))
position_bridge.close()


print("generic MQTT event-out is explicit, normalized, and non-retained")
event_client = FakeMQTT()
event_bridge = HomeAssistantMQTT(
    service,
    MQTTConfig(host="broker", gateway_id="event-test", publish_events=True,
               refresh_seconds=999),
    client=event_client, clock=lambda: now,
)
event_bridge.start()
event_packet = Packet(
    now, node.node_id, BROADCAST, "TEXT_MESSAGE_APP", '"hello"', snr=6.0,
    raw={"payload": b"private-wire-bytes", "secret": "not-for-mqtt"})
event_bridge.handle_event("packet", event_packet)
packet_event = next(row for row in reversed(event_client.published)
                    if row[0].endswith("/events/packet"))
packet_json = json.loads(packet_event[1])
check("packet event is not retained", packet_event[3], False)
check("packet event carries normalized routing", (packet_json["from"], packet_json["port"]),
      (node.node_id, "TEXT_MESSAGE_APP"))
check("packet event excludes raw radio data",
      "not-for-mqtt" in packet_event[1] or "private-wire-bytes" in packet_event[1], False)
event_message = ChatMessage(
    ts=now, from_id=node.node_id, from_name=node.name,
    to_id=service.state.my_node_id, text="operator message", channel=-1)
event_bridge.handle_event("chat", event_message)
message_event = next(row for row in reversed(event_client.published)
                     if row[0].endswith("/events/message"))
check("message event is explicit and non-retained",
      (json.loads(message_event[1])["text"], message_event[3]),
      ("operator message", False))
event_bridge.handle_event("receipt", SendReceipt(
    "event-msg", PeerRef("meshcore", node.node_id), DeliveryStatus.DELIVERED))
receipt_event = next(row for row in reversed(event_client.published)
                     if row[0].endswith("/events/receipt"))
check("receipt event exposes delivery state",
      json.loads(receipt_event[1])["status"], "delivered")
event_bridge.close()


print("headless gateway snapshots restore the telemetry MQTT exports")
with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    database = Path(tmp) / "mesh.db"
    stored = Store(database, flush_interval=0.01)
    check("telemetry store opens", stored.open(), True)
    first = MeshService(stored)
    first.connected({
        "my_node_id": "!10000001", "my_node_name": "Gateway", "protocol": "meshtastic",
        "channels": [(0, "Primary")], "device": "fake://radio",
    })
    first.receive_node({
        "id": "!20000002", "num": 0x20000002,
        "user": {"id": "!20000002", "longName": "Stored sensor"},
        "deviceMetrics": {"batteryLevel": 76, "channelUtilization": 9.5,
                          "airUtilTx": 2.25, "uptimeSeconds": 9001},
    })
    first.receive_packet(Packet(
        now, "!20000002", BROADCAST, "TELEMETRY_APP", "stored telemetry",
        snr=4.5, hops=0,
        raw={"decoded": {"telemetry": {
            "environmentMetrics": {"temperature": 21.25},
            "localStats": {"numRx": 321},
        }}},
    ))
    first.persist_snapshot()
    stored.close()

    reopened = Store(database, flush_interval=0.01)
    check("telemetry store reopens", reopened.open(), True)
    restored = MeshService(reopened)
    restored.restore()
    restored_node = restored.state.nodes["!20000002"]
    check("environment telemetry restored", restored_node.env, {"temperature": 21.25})
    check("mesh statistics restored", restored_node.local_stats, {"numRx": 321.0})
    check("device telemetry restored",
          (restored_node.battery, restored_node.ch_util, restored_node.air_util,
           restored_node.uptime),
          (76, 9.5, 2.25, 9001))
    check("observer signal history restored",
          (restored_node.snr, restored_node.hops, list(restored_node.snr_history)),
          (4.5, 0, [4.5]))
    reopened.close()


print("companion bot is DM-only, allowlisted, local, and bounded")
bot_service = MeshService(None)
link = Link()
bot_service.attach_link(link)
bot_service.connected({
    "my_node_id": "!10000001", "my_node_name": "Gateway", "protocol": "meshcore",
    "channels": [(0, "Public")], "device": "fake://radio",
})
bot_service.state.nodes[node.node_id] = node
bot = TelemetryBot(bot_service, allowed_nodes=["!30000003"], cooldown_seconds=0)
allowed = ChatMessage(
    ts=now, from_id="!30000003", from_name="Companion", to_id="!10000001",
    text="!mesh node ridge", channel=-1, packet_id=501,
)
receipts = bot.route(allowed)
check("allowed command produces a receipt", bool(receipts), True)
answer = " ".join(item[0] for item in link.sent)
check("reply targets the companion", link.sent[-1][1].node_id, "!30000003")
check("reply contains stored telemetry", all(piece in answer for piece in
      ("Ridge Sensor", "SNR 7.25dB", "battery 87%", "temperature 23.5")), True)
check("bot position is private by default", "0.25000" in answer, False)
check("reply fits MeshCore frames",
      all(len(item[0].encode("utf-8")) <= 133 for item in link.sent), True)
check("duplicate mesh packet is suppressed", bot.route(allowed), [])

denied = ChatMessage(
    ts=now, from_id="!bad00001", from_name="Stranger", to_id="!10000001",
    text="!mesh nodes", channel=-1, packet_id=502,
)
check("unlisted peer is ignored", bot.route(denied), [])
channel = ChatMessage(
    ts=now, from_id="!30000003", from_name="Companion", to_id=BROADCAST,
    text="!mesh nodes", channel=0, packet_id=503,
)
check("channel command is ignored", bot.route(channel), [])
ordinary_dm = ChatMessage(
    ts=now, from_id="!30000003", from_name="Companion", to_id="!10000001",
    text="hello human", channel=-1, packet_id=504,
)
check("ordinary DM is left for the human", bot.route(ordinary_dm), [])
bot.close()


print("\ninbound MQTT sends: allowlist-gated bridge to the radio")


class _Receipt:
    class _Status:
        value = "queued"
    status = _Status()


sent_mesh = []
service.state.channels = [(0, "Public"), (14, "sensors")]
service.send_message = lambda text, dest: (sent_mesh.append((text, dest)) or _Receipt())
fake_send = FakeMQTT()
send_config = MQTTConfig(host="mqtt.example.invalid", gateway_id="field station",
                         refresh_seconds=999, send_channels=("#Sensors",),
                         send_min_seconds=2.0)
check("allowlist names are normalized", send_config.send_channels, ("sensors",))
send_clock = [now]
send_bridge = HomeAssistantMQTT(service, send_config, client=fake_send,
                                clock=lambda: send_clock[0])
send_bridge.start()
check("send topic subscribed", fake_send.subscribed,
      [("meshtui/field_station/send", 0)])
check("json payload sends",
      send_bridge.handle_send('{"channel": "sensors", "text": "door open"}'), True)
check("message reached the service", sent_mesh[-1][0], "door open")
check("resolved to the sensors slot", sent_mesh[-1][1].index, 14)
send_clock[0] += 3
check("bare text goes to the first allowlisted channel",
      send_bridge.handle_send("washer done"), True)
check("bare text sent", sent_mesh[-1][0], "washer done")
send_clock[0] += 0.5
check("faster than the interval is dropped", send_bridge.handle_send("spam"), False)
send_clock[0] += 3
check("non-allowlisted channel refused",
      send_bridge.handle_send('{"channel": "Public", "text": "x"}'), False)
check("empty text refused", send_bridge.handle_send("   "), False)
send_clock[0] += 3
check("oversize payload truncated, not refused",
      send_bridge.handle_send("x" * 500), True)
check("truncated to the protocol limit", payload_bytes(sent_mesh[-1][0]) <= 133, True)
fake_quiet = FakeMQTT()
quiet_bridge = HomeAssistantMQTT(
    service, MQTTConfig(host="mqtt.example.invalid", refresh_seconds=999),
    client=fake_quiet, clock=lambda: now)
quiet_bridge.start()
check("no allowlist -> nothing subscribed, radio cannot be keyed",
      fake_quiet.subscribed, [])
send_bridge.close()
quiet_bridge.close()

print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("PASS")
