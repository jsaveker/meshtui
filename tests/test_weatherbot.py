"""The scheduled weather bot: format, schedule windows, dedup, routing.

The report must fit one LoRa payload, post only inside a slot's window (a
gateway that was down at 07:00 must not post stale morning weather at 09:30),
and never double-post a slot across restarts.
"""
import sys, time, types

from meshtui.model import payload_bytes
from meshtui.radio import protocol_payload_limit
from meshtui.state import MeshState
from meshtui.weatherbot import WeatherBot, parse_times

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

OPEN_METEO_SAMPLE = {
    "current": {"temperature_2m": 94.3, "apparent_temperature": 101.2,
                "relative_humidity_2m": 41, "weather_code": 2,
                "wind_speed_10m": 8.4, "wind_direction_10m": 170},
    "daily": {"temperature_2m_max": [98.6], "temperature_2m_min": [74.8],
              "precipitation_probability_max": [20]},
}


class FakeStore:
    enabled = True
    def __init__(self): self.meta = {}
    def get_meta(self, key, default=None): return self.meta.get(key, default)
    def set_meta(self, key, value): self.meta[key] = value


class FakeService:
    def __init__(self):
        self.state = MeshState()
        self.state.protocol = "meshcore"
        self.state.channels = [(0, "Public"), (13, "#wx")]
        self.state.my_node_id = "!c0decafe"
        self.state.my_node_name = "Base Station"
        me = self.state.upsert_node({"user": {"id": "!c0decafe"},
                                     "position": {"latitude": 0.30, "longitude": 0.30}})
        me.is_self = True
        self.store = FakeStore()
        self.sent = []
    def send_message(self, text, destination, **kw):
        self.sent.append((text, destination))
        return types.SimpleNamespace(status=types.SimpleNamespace(value="sent"))


service = FakeService()
bot = WeatherBot(service, channel="#wx", location="Field Site")

# ------------------------------------------------------------------ compose
report = bot.compose(OPEN_METEO_SAMPLE)
check("report speaks the channel's dialect",
      report, "[Field Site] 94F (feels 101F), Humidity 41%, Wind S 8mph, "
              "partly cloudy | Hi 99F Lo 75F, rain 20%")
check("report fits one LoRa payload",
      payload_bytes(report) <= protocol_payload_limit("meshcore"), True)
mild = dict(OPEN_METEO_SAMPLE, current=dict(OPEN_METEO_SAMPLE["current"],
                                            apparent_temperature=95.0))
check("feels-like is omitted when it matches the temperature",
      bot.compose(mild).startswith("[Field Site] 94F, Humidity"), True)

# ----------------------------------------------------------------- schedule
check("times parse and sort", parse_times("18:00, 07:00,12:30,junk,25:00"),
      [7 * 60, 12 * 60 + 30, 18 * 60])
def at(hh, mm):
    return time.struct_time((2026, 8, 31, hh, mm, 0, 0, 243, 1))
check("inside a slot window -> that slot",
      bot.due_slot(at(7, 5)), "2026-08-31/07:00")
check("late for the slot -> no post (stale weather is noise)",
      bot.due_slot(at(9, 30)), None)
check("just before a slot -> not yet", bot.due_slot(at(6, 59)), None)

# --------------------------------------------------------- post + dedup
bot.fetch = lambda lat, lon: OPEN_METEO_SAMPLE
check("post_now sends to the configured channel",
      (bot.post_now(), service.sent[-1][1].index), (True, 13))
service.store.set_meta("weatherbot_last_slot", "2026-08-31/07:00")
check("a remembered slot is not reposted",
      bot._already_posted("2026-08-31/07:00"), True)
check("the next slot is fresh",
      bot._already_posted("2026-08-31/12:00"), False)

# ------------------------------------------------------------ guard rails
service.state.nodes.clear()
count = len(service.sent)
check("no station position means no post", bot.post_now(), False)
check("nothing was sent without a position", len(service.sent), count)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
