"""The !path bot and the path-observation pipeline behind it.

A path observation must be derived from RF-logged adverts and decoded channel
messages, folded into one record when both sightings describe the same packet,
persisted, and answered on the bot channel in the dialect the mesh's other
pathbots speak - with the good-neighbor rules (one reply per request, cooldown,
never answering another bot) actually enforced.
"""
import json, os, sys, tempfile, time, types

from meshtui.bot import PathBot
from meshtui.model import Packet
from meshtui.pathcalc import PathObservation, analyze, bot_reply, obs_from_packet, split_sender
from meshtui.service import MeshService
from meshtui.state import MeshState
from meshtui.store import Store

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

NOW = time.time()

# ------------------------------------------------------------- derivation
adv = Packet(ts=NOW, from_id="!abababab", to_id="^all", portnum="RXLOG_APP",
             summary="advert (rf)",
             raw={"payload_typename": "ADVERT", "adv_key": "ab" * 32,
                  "adv_name": "Hilltop", "path": "1a4c", "snr": -3.0, "rssi": -90})
obs = obs_from_packet(adv)
check("advert sighting derives an observation",
      (obs.kind, obs.origin_id, obs.origin_name, obs.hops, obs.path),
      ("advert", "!abababab", "Hilltop", 2, "1a4c"))

msg = Packet(ts=NOW, from_id="channel:2:anonymous", to_id="^all", channel=2,
             portnum="TEXT_MESSAGE_APP", summary='"FieldScout: !path"',
             raw={"type": "CHAN", "text": "FieldScout: !path", "path": "a1",
                  "path_len": 1, "SNR": 4.5})
obs = obs_from_packet(msg)
check("decoded channel message derives an observation",
      (obs.kind, obs.origin_name, obs.hops, obs.path, obs.snr, obs.channel),
      ("channel", "FieldScout", 1, "a1", 4.5, 2))

direct = obs_from_packet(Packet(ts=NOW, from_id="channel:2:anonymous", to_id="^all",
                                channel=2, portnum="TEXT_MESSAGE_APP", summary="x",
                                raw={"type": "CHAN", "text": "A: !path", "path_len": 0}))
check("zero-hop message derives a direct observation", direct.hops, 0)
check("meshtastic-shaped packets derive nothing",
      obs_from_packet(Packet(ts=NOW, from_id="!x", to_id="^all",
                             portnum="TEXT_MESSAGE_APP", summary="x",
                             raw={"decoded": {}})), None)
check("sender parsing tolerates plain text", split_sender("no prefix here"),
      ("", "no prefix here"))

# ------------------------------------------------------ dedup-merge in state
state = MeshState()
state.protocol = "meshcore"
rf = PathObservation(ts=NOW, kind="channel", path="a1", hops=1, snr=4.5)
decoded = PathObservation(ts=NOW, kind="channel", path="a1", hops=1,
                          origin_name="FieldScout", channel=2)
_, first_new = state.note_path(rf)
merged, second_new = state.note_path(decoded)
check("same packet's two sightings fold into one record",
      (first_new, second_new, len(state.paths)), (True, False, 1))
check("the merge keeps the richer fields",
      (merged.origin_name, merged.channel, merged.snr), ("FieldScout", 2, 4.5))

# The RF log reports at heard-time; the decoded message follows on the next
# poll seconds later. Complementary sightings across that gap must still fold.
late = PathObservation(ts=NOW + 2.4, kind="channel", path="", hops=2,
                       origin_name="LateNews", channel=2)
rf_first = PathObservation(ts=NOW, kind="channel", path="aabb", hops=2, snr=1.0)
state2 = MeshState()
state2.note_path(rf_first)
merged2, is_new2 = state2.note_path(late)
check("a decoded message folds into its seconds-earlier RF sighting",
      (is_new2, merged2.origin_name, merged2.path), (False, "LateNews", "aabb"))
distinct = PathObservation(ts=NOW + 2.0, kind="channel", path="ccdd", hops=2,
                           origin_name="Other")
_, distinct_new = state2.note_path(distinct)
check("a different packet in the window stays separate", distinct_new, True)

# ------------------------------------------------------------ analysis/reply
state.upsert_node({"user": {"id": "!abababab", "longName": "FieldScout"},
                   "position": {"latitude": 0.00, "longitude": 0.10}})
state.upsert_node({"user": {"id": "!a1112233", "longName": "Hilltop Repeater"},
                   "position": {"latitude": 0.20, "longitude": 0.20}})
state.my_node_id = "!c0decafe"
me = state.upsert_node({"user": {"id": "!c0decafe", "longName": "Base Station"},
                        "position": {"latitude": 0.30, "longitude": 0.30}})
me.is_self = True

analysis = analyze(state, merged)
check("the hop resolves to the repeater", analysis.hops[0].label, "Hilltop Repeater")
check("route sums origin->hop->me",
      analysis.route_km is not None and analysis.route_km > analysis.direct_km, True)

# A lone repeater is CONFIDENT even when chat nodes share its byte, and an
# ambiguous or absurd hop must weaken the estimate, not inflate it.
state.upsert_node({"user": {"id": "!a1ffff01", "longName": "Chatty",
                            "role": "CHAT"}})
state.upsert_node({"user": {"id": "!a1112233", "longName": "Hilltop Repeater",
                            "role": "REPEATER"}})
confident = analyze(state, merged)
check("a lone repeater stays confident despite byte-sharing chat nodes",
      (confident.hops[0].ambiguous, confident.resolved), (False, 1))
state.upsert_node({"user": {"id": "!a1445566", "longName": "Far Repeater",
                            "role": "REPEATER"},
                   "position": {"latitude": 48.0, "longitude": 2.0}})
contested = analyze(state, merged)
check("a second repeater on the byte makes the hop ambiguous",
      contested.hops[0].ambiguous, True)
check("an ambiguous hop is excluded from the route",
      contested.route_km is None or contested.route_km < 100, True)

state.upsert_node({"user": {"id": "!99000001", "longName": "Paris Repeater",
                            "role": "REPEATER"},
                   "position": {"latitude": 48.85, "longitude": 2.35}})
absurd = analyze(state, PathObservation(ts=NOW, kind="advert", origin_id="!abababab",
                                        path="99", hops=1))
check("an impossible leg yields no route instead of a continental one",
      absurd.route_km, None)
check("a plausible origin keeps its direct figure",
      absurd.direct_km is not None and absurd.direct_km < 100, True)
overseas = analyze(state, PathObservation(ts=NOW, kind="advert",
                                          origin_id="!99000001", path="a1", hops=1))
check("an origin claiming to be an ocean away yields no direct figure",
      overseas.direct_km, None)

reply = bot_reply(state, merged, "FieldScout")
check("reply speaks the pathbot dialect",
      reply.startswith("@[FieldScout] [1h] a1 route: ~") and "direct: ~" in reply, True)
check("zero hops answers 'direct'",
      bot_reply(state, PathObservation(ts=NOW, kind="channel", hops=0), "A"),
      "@[A] direct (no path)")
check("missing observation is answered honestly",
      bot_reply(state, None, "A"), "@[A] heard you, but no path data for that message")

# ------------------------------------------------------- two-byte path hashes
from meshtui.meshcore_link import _split_path

check("hop hashes split by the width the hop count implies",
      PathObservation(ts=NOW, kind="channel", path="9ef9500e", hops=2).hop_bytes(),
      ["9ef9", "500e"])
check("one-byte meshes still split per byte",
      PathObservation(ts=NOW, kind="channel", path="aabbcc", hops=3).hop_bytes(),
      ["aa", "bb", "cc"])
check("_split_path derives the same widths",
      (_split_path("9ef9500e", 2), _split_path("aabbcc", 3)),
      (["9ef9", "500e"], ["aa", "bb", "cc"]))

# Synthetic three-hop, three-byte hashes (nine bytes total).
TRI_PATH = "111111222222bcdeca"
check("three-byte hashes split into six-char groups",
      PathObservation(ts=NOW, kind="channel", path=TRI_PATH, hops=3).hop_bytes(),
      ["111111", "222222", "bcdeca"])
check("_split_path handles 3-byte hashes",
      _split_path(TRI_PATH, 3), ["111111", "222222", "bcdeca"])
tri_state = MeshState()
tri_state.protocol = "meshcore"
tri_state.upsert_node({"user": {"id": "!bcdecafe", "longName": "Ridge Solar Repeater",
                                "role": "REPEATER"}})
tri = analyze(tri_state, PathObservation(ts=NOW, kind="channel", path=TRI_PATH, hops=3))
check("a 3-byte hash resolves by its six-char prefix",
      (tri.hops[2].label, tri.hops[2].ambiguous), ("Ridge Solar Repeater", False))

wide = obs_from_packet(Packet(ts=NOW, from_id="channel:2:anonymous", to_id="^all",
                              channel=2, portnum="TEXT_MESSAGE_APP", summary="x",
                              raw={"type": "CHAN", "text": "Far: !path",
                                   "path": "9ef9500e", "path_len": 2, "SNR": 3.0}))
check("frame path_len is the hop count, not the byte count", wide.hops, 2)

wide_state = MeshState()
wide_state.protocol = "meshcore"
wide_state.upsert_node({"user": {"id": "!9ef9aaaa", "longName": "Precise Rpt",
                                 "role": "REPEATER"}})
wide_state.upsert_node({"user": {"id": "!9e000001", "longName": "Byte Twin",
                                 "role": "REPEATER"}})
wide_analysis = analyze(wide_state, wide)
check("a 2-byte hash resolves past 1-byte collisions",
      (wide_analysis.hops[0].label, wide_analysis.hops[0].ambiguous),
      ("Precise Rpt", False))
check("the reply prints whole hashes",
      bot_reply(wide_state, wide, "Far").startswith("@[Far] [2h] 9ef9,500e"), True)

# -------------------------------------------------------------- map links
from meshtui.pathcalc import geojson_url, route_geojson

map_analysis = analyze(state, merged)
geo = route_geojson(map_analysis)
kinds = [f["geometry"]["type"] for f in geo["features"]]
check("geojson has one line plus a point per positioned node",
      (kinds[0], kinds.count("Point")), ("LineString", len(map_analysis.points())))
line = geo["features"][0]["geometry"]["coordinates"]
check("coordinates are lon,lat in travel order",
      (line[0], line[-1]), ([0.1, 0.0], [0.3, 0.3]))
url = geojson_url(geo)
check("map url is a base64 data URI (percent-encoding breaks geojson.io)",
      url.startswith("https://geojson.io/#data=data:application/json;base64,")
      and "%" not in url and " " not in url, True)
import base64 as _b64
decoded = json.loads(_b64.b64decode(url.split(";base64,", 1)[1]))
check("the base64 payload round-trips exactly", decoded, geo)
check("stops carry simplestyle titles and numbered symbols",
      (decoded["features"][1]["properties"].get("title", "").startswith("1. "),
       decoded["features"][1]["properties"].get("marker-symbol")), (True, "1"))
check("no route means no map",
      route_geojson(analyze(state, PathObservation(ts=NOW, kind="channel", hops=0))),
      None)

# ---------------------------------------------------------------- the bot
tmp = tempfile.mkdtemp(prefix="meshtui-pathbot-")
store = Store(os.path.join(tmp, "mesh.db"), flush_interval=0.1)
assert store.open(), store.error
service = MeshService(store)
service.state.protocol = "meshcore"
service.state.channels = [(0, "Public"), (2, "#bot")]
service.state.my_node_id = "!c0decafe"
sent = []
service.send_message = lambda text, dest, **kw: sent.append((text, dest)) or types.SimpleNamespace()

bot = PathBot(service, channel="#bot")
bot.WAIT_STEPS = 1
bot.WAIT_STEP_SECONDS = 0.0
bot._map_link = lambda analysis: "https://da.gd/test1"

def channel_msg(text, ts=None, channel=2):
    from meshtui.model import ChatMessage
    return ChatMessage(ts=ts or time.time(), from_id=f"channel:{channel}:anonymous",
                       from_name="", to_id="^all", text=text, channel=channel)

# the request's own packet lands first (as it does live)
service.receive_packet(Packet(ts=time.time(), from_id="channel:2:anonymous",
                              to_id="^all", channel=2, portnum="TEXT_MESSAGE_APP",
                              summary="x", raw={"type": "CHAN",
                                                "text": "FieldScout: !path",
                                                "path": "a1", "path_len": 1,
                                                "SNR": 4.5}))
bot.route(channel_msg("FieldScout: !path"))
check("one reply per request", len(sent), 1)
check("reply is addressed and routed",
      sent[0][0].startswith("@[FieldScout] [1h] a1") and sent[0][1].index == 2, True)
check("the map link rides along when it fits",
      sent[0][0].endswith(", https://da.gd/test1"), True)

# the real shortener call, with the network mocked out
import io, urllib.request
real_urlopen = urllib.request.urlopen
captured = {}
class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen(request, timeout=None):
    captured["url"] = request.full_url
    return FakeResponse(b"https://da.gd/xyz9\n")
urllib.request.urlopen = fake_urlopen
try:
    bot2 = PathBot(service, channel="#bot")
    link = bot2._map_link(analyze(state, merged))
    bot2.close()
finally:
    urllib.request.urlopen = real_urlopen
check("shortener reply becomes the link", link, "https://da.gd/xyz9")
check("the shortener is asked for a geojson.io url",
      "geojson.io" in urllib.parse.unquote(captured["url"]), True)

bot.route(channel_msg("FieldScout: !path"))
check("cooldown blocks an immediate repeat", len(sent), 1)
bot.route(channel_msg("OtherPathBot: @[FieldScout] [1h] a1 route: ~1mi"))
check("another bot's reply is never answered", len(sent), 1)
bot.route(channel_msg("Someone: !path", channel=0))
check("other channels are ignored", len(sent), 1)
bot.route(channel_msg("StaleSender: !path", ts=time.time() - 600))
check("stale backlog requests are ignored", len(sent), 1)

# the mesh's other calling conventions
count = len(sent)
service.receive_packet(Packet(ts=time.time(), from_id="channel:2:anonymous",
                              to_id="^all", channel=2, portnum="TEXT_MESSAGE_APP",
                              summary="x", raw={"type": "CHAN",
                                                "text": "Ave Maritza: Pathbot f",
                                                "path": "82", "path_len": 1,
                                                "SNR": 2.0}))
bot.route(channel_msg("Ave Maritza: Pathbot f"))
check("'Pathbot f' is answered", len(sent), count + 1)
service.receive_packet(Packet(ts=time.time(), from_id="channel:2:anonymous",
                              to_id="^all", channel=2, portnum="TEXT_MESSAGE_APP",
                              summary="x", raw={"type": "CHAN", "text": "Shap: Path",
                                                "path": "82", "path_len": 1,
                                                "SNR": 2.0}))
bot.route(channel_msg("Shap: Path"))
check("bare 'Path' is answered", len(sent), count + 2)
bot.route(channel_msg("Chatty: path finding on this mesh is neat"))
check("a sentence starting with 'path' is not a summons", len(sent), count + 2)
bot.close()

# ------------------------------------------------- payload-budget ladder
from meshtui.model import payload_bytes
from meshtui.radio import protocol_payload_limit

LIMIT = protocol_payload_limit("meshcore")
service.state.channels = [(0, "Public"), (2, "#bot")]
ladder_bot = PathBot(service, channel="#bot")
ladder_bot._map_link = lambda analysis: "https://da.gd/mapzz"
short_obs = PathObservation(ts=time.time(), kind="channel", path="4c82",
                            hops=2, origin_name="Near", channel=2)
long_obs = PathObservation(ts=time.time(), kind="channel",
                           path="ab" * 40, hops=40, origin_name="Far", channel=2)
fits = ladder_bot._compose_reply("@[Near] [2h]", short_obs, LIMIT)
check("a short reply keeps everything including the link",
      fits.endswith(", https://da.gd/mapzz") and payload_bytes(fits) <= LIMIT, True)
long_reply = ladder_bot._compose_reply("@[Far] [40h]", long_obs, LIMIT)
check("a 40-hop reply fits the payload", payload_bytes(long_reply) <= LIMIT, True)
check("degradation drops whole pieces, never truncating mid-hash",
      "…" not in long_reply and not long_reply.endswith(","), True)
check("the degraded reply still leads with the hop count",
      long_reply.startswith("@[Far] [40h]"), True)
check("the map link outlives the hop hashes",
      long_reply.endswith(", https://da.gd/mapzz")
      and "ab,ab" not in long_reply, True)
no_link_bot = PathBot(service, channel="#bot")
no_link_bot._map_link = lambda analysis: None
bare = no_link_bot._compose_reply("@[Far] [40h]", long_obs, LIMIT)
check("without a link, degradation still lands on a fitting variant",
      payload_bytes(bare) <= LIMIT and bare.startswith("@[Far] [40h]"), True)
no_link_bot.close()
ladder_bot.close()

# ---------------------------------------------------------------- test bot
from meshtui.bot import TestBot

service.state.channels = [(0, "Public"), (2, "#bot"), (10, "#testing")]
service.state.my_node_name = "Base Station"
tester = TestBot(service, channel="#testing")
tester.WAIT_STEPS = 1
tester.WAIT_STEP_SECONDS = 0.0
sent.clear()

def heard(name, hops, snr=None, path=""):
    service.state.note_path(PathObservation(ts=time.time(), kind="channel",
                                            origin_name=name, path=path,
                                            hops=hops, snr=snr, channel=10))

tester._map_link = lambda analysis: "https://da.gd/mapx"
heard("MobileOne", 2, path="a1b2")
tester.route(channel_msg("MobileOne: testing fw", channel=10))
check("a test receipt carries the pathbot's journey detail",
      sent[-1][0],
      "@[MobileOne] 2 hops to Base Station: a1,b2 (0/2), https://da.gd/mapx")
check("the receipt goes to the testing channel", sent[-1][1].index, 10)

heard("PocketOne", 0, snr=9.2)
tester.route(channel_msg("PocketOne: Test", channel=10))
check("a direct test reports direct with its SNR",
      sent[-1][0], "@[PocketOne] direct to Base Station (+9.2dB)")

located = TestBot(service, channel="#testing", location="Field Site")
located.WAIT_STEPS = 1
located.WAIT_STEP_SECONDS = 0.0
located._map_link = lambda analysis: None
heard("Roamer", 3)
located.route(channel_msg("Roamer: radio check", channel=10))
check("a configured place name rides in the receipt",
      sent[-1][0], "@[Roamer] 3 hops to Base Station (Field Site)")
located.close()

before_len = len(sent)
tester.route(channel_msg("MobileOne: thanks!", channel=10))
check("chatter gets no robot reply", len(sent), before_len)
tester.route(channel_msg("OtherStation: @[PocketOne] 4 hops to RemoteBase", channel=10))
check("another station's receipt gets no reply", len(sent), before_len)
tester.route(channel_msg('X: 😀 reacted to "Test" @[PocketOne]', channel=10))
check("reactions get no reply", len(sent), before_len)
tester.route(channel_msg("Someone: test", channel=2))
check("tests on other channels are ignored", len(sent), before_len)
heard("Quiet", 3)
tester.route(channel_msg("Quiet: test", ts=time.time() - 600, channel=10))
check("stale test messages are ignored", len(sent), before_len)
tester.close()

# ------------------------------------------------------------- persistence
time.sleep(0.5)  # the store flushes on a background cadence
persisted = store.recent_paths()
check("observations are persisted", len(persisted) >= 1, True)
check("a persisted row survives the round trip",
      any((o.origin_name, o.path, o.hops) == ("FieldScout", "a1", 1)
          for o in persisted), True)
store.close()

# -------------------------------------------------------- gateway command
from meshtui.gateway import Gateway
gw = Gateway.__new__(Gateway)
gw.service = service
result = gw.handle_request({"command": "paths", "limit": 10})
check("gateway serves path history",
      result["ok"] and any(r["origin_name"] == "FieldScout"
                           for r in result["paths"]), True)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
