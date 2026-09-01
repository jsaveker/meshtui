"""Sensor digest bot: compose from heard telemetry, interval dedup."""

import os
import sys
import tempfile
import time

from meshtui.model import payload_bytes
from meshtui.sensorbot import SensorBot
from meshtui.service import MeshService
from meshtui.store import Store

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


service = MeshService(store=None)
state = service.state
state.protocol = "meshcore"
state.channels = [(0, "Public"), (4, "sensors")]
state.my_node_id = "!c0decafe"
state.my_node_name = "Tachyon Home"
now = time.time()

bot = SensorBot(service, channel="#sensors", minutes=30)
check("no telemetry yet -> nothing to post", bot.compose(), None)

# our own radio battery
me = state.upsert_node({"user": {"id": "!c0decafe", "longName": "Tachyon Home"},
                        "lastHeard": now})
me.is_self = True
me.voltage, me.battery = 4.10, 87
# a repeater with power telemetry (remote status)
rep = state.upsert_node({"user": {"id": "!bc20c203", "longName": "Santaluz Solar",
                                  "role": "REPEATER"}, "lastHeard": now - 60})
rep.voltage = 12.9
# an environment sensor, freshest of all
env = state.upsert_node({"user": {"id": "!aa000001", "longName": "Garden Node"},
                         "lastHeard": now})
env.env = {"temperature": 25.94, "relativeHumidity": 44.2}
env.env_ts = now + 1
# a chat node's phone battery must NOT be listed
phone = state.upsert_node({"user": {"id": "!dd000001", "longName": "Somebody",
                                    "role": "CHAT"}, "lastHeard": now})
phone.battery = 51

digest = bot.compose()
check("digest leads with the tag", digest.startswith("[sensors]"), True)
check("freshest entry first", "25.9C" in digest.split("|")[0], True)
check("environment readings formatted", "25.9C 44%" in digest, True)
check("own battery included", "4.10V 87%" in digest, True)
check("repeater power included", "12.9" in digest, True)
check("a stranger's phone battery is not broadcast", "51%" in digest, False)
check("digest fits the payload", payload_bytes(digest) <= 133, True)

sent = []
service.send_message = lambda text, dest: (sent.append((text, dest)),
                                           type("R", (), {"status": type("S", (), {"value": "queued"})()})())[1]
check("post_now sends to the named channel", bot.post_now(), True)
check("destination resolved to slot 4", sent[-1][1].index, 4)

missing = SensorBot(service, channel="#nope")
check("unknown channel refuses to post", missing.post_now(), False)

# interval memory survives a restart via the store
tmp = os.path.join(tempfile.mkdtemp(prefix="meshtui-sensorbot-"), "mesh.db")
store = Store(tmp, flush_interval=0.01)
check("store opens", store.open(), True)
service.store = store
check("never posted -> due", bot.due(now), True)
bot._remember(now)
time.sleep(0.3)  # set_meta is batched onto the flush thread
check("just posted -> not due", bot.due(now + 60), False)
check("due again after the interval", bot.due(now + 31 * 60), True)
store.close()
store2 = Store(tmp, flush_interval=0.01)
check("store reopens", store2.open(), True)
service.store = store2
check("last post survives a restart", bot.due(now + 60), False)
store2.close()

print()
if failures:
    print(f"FAIL: {failures}")
    sys.exit(1)
print("PASS")
