"""Protocol preferences, four-pane workspace, palette, and route selection."""

import asyncio
import json
import os
import sys
import tempfile
import time

from meshtui.app import MeshTUI
from meshtui.model import Packet
from meshtui.preferences import OperatorPreferences
from meshtui.widgets.operator import PacketWorkbench, RoutePane
from meshtui.widgets.palette import CommandPalette

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


async def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="meshtui-operator-")
    prefs_path = os.path.join(tmpdir, "preferences.json")
    prefs = OperatorPreferences(prefs_path)
    prefs.update("meshcore", layout="route", theme="night-vision")
    prefs.update("meshtastic", layout="radio", theme="high-contrast")
    check("preferences are protocol scoped", prefs.get("meshcore")["layout"], "route")
    check("other protocol keeps its own layout", prefs.get("meshtastic")["layout"], "radio")
    check("preferences file is private", oct(os.stat(prefs_path).st_mode & 0o777), "0o600")
    check("stored file has no station-specific schema",
          sorted(json.load(open(prefs_path))["protocols"]), ["meshcore", "meshtastic"])

    app = MeshTUI(demo=True, store=None, protocol="meshcore",
                  preferences_path=prefs_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause(0.5)
        # DemoLink identifies as Meshtastic; set the protocol under test after
        # its synthetic connection event, as a real MeshCore link would do.
        app.state.protocol = "meshcore"
        app._apply_preferences()
        workspace = app.query_one("#workspace")
        check("four-pane workspace is mounted", len(list(workspace.children)), 4)
        check("packet workbench is visible", bool(app.query_one(PacketWorkbench)), True)
        check("route pane is visible", bool(app.query_one(RoutePane)), True)
        check("meshcore layout restored", workspace.has_class("layout-route"), True)
        check("meshcore theme restored", app.theme, "night-vision")

        # A real MeshCore-shaped path drives the byte and route panes.
        st = app.state
        st.protocol = "meshcore"
        st.my_node_id = "!c0decafe"
        me = st.upsert_node({"user": {"id": "!c0decafe", "longName": "Base"},
                             "position": {"latitude": 0.30, "longitude": 0.30}})
        me.is_self = True
        st.upsert_node({"user": {"id": "!aa000001", "longName": "Walker"},
                        "position": {"latitude": 0.00, "longitude": 0.10}})
        st.upsert_node({"user": {"id": "!4c000001", "longName": "Hilltop",
                                  "role": "REPEATER"},
                        "position": {"latitude": 0.20, "longitude": 0.20}})
        packet = Packet(
            ts=time.time(), from_id="!aa000001", to_id="^all",
            portnum="RXLOG_APP", summary="Walker: hello", hops=1, snr=3.5,
            raw={"payload_typename": "ADVERT", "adv_key": "aa", "adv_name": "Walker",
                 "path": "4c", "path_len": 1, "path_hash_size": 1,
                 "snr": 3.5, "payload": b"hello"},
        )
        app._on_packet(packet)
        await pilot.pause(0.3)
        route = app.query_one(RoutePane)
        route.show_packet(packet)
        await pilot.pause(0.2)
        check("route displays path hash badge",
              "[1B path hash]" in str(route.query_one("#route-chain").render()), True)
        check("route resolves the repeater",
              "Hilltop" in str(route.query_one("#route-chain").render()), True)
        check("hop click surface exposes prefix candidates",
              "Hilltop" in str(route.query_one("#route-prefix").render()), True)
        check("positioned route draws a braille map polyline",
              any(0x2800 <= ord(c) <= 0x28FF
                  for c in str(route.query_one("#route-canvas").render())), True)

        trace = Packet(
            ts=time.time(), from_id="!aa000001", to_id="!c0decafe",
            portnum="TRACEROUTE_APP", summary="round trip",
            raw={"from": 0xAA000001, "to": 0xC0DECAFE,
                 "decoded": {"traceroute": {
                     "route": [0x4C000001], "snrTowards": [8, 4],
                     "routeBack": [0x4C000001], "snrBack": [-8, 12]}}},
        )
        route.show_packet(trace)
        await pilot.pause(0.2)
        trace_text = str(route.query_one("#route-chain").render())
        check("round-trip trace renders both directions",
              "round-trip trace" in trace_text and "return" in trace_text, True)
        check("trace route pane shows per-hop SNR",
              "+2.0dB" in trace_text and "-2.0dB" in trace_text, True)

        # `/` belongs to the operator palette; `z` remains the fast chat key.
        await pilot.press("slash")
        await pilot.pause(0.2)
        check("slash opens command palette", isinstance(app.screen, CommandPalette), True)
        field = app.screen.query_one("#palette-input")
        field.value = "layout balanced"
        await pilot.press("enter")
        await pilot.pause(0.2)
        check("palette changes layout", workspace.has_class("layout-balanced"), True)
        check("palette persists the active protocol",
              OperatorPreferences(prefs_path).get("meshcore")["layout"], "balanced")

        await pilot.press("slash")
        await pilot.pause(0.2)
        field = app.screen.query_one("#palette-input")
        field.value = "node Hilltop"
        await pilot.press("enter")
        await pilot.pause(0.2)
        check("palette jumps to a matching node", app.query_one("#nodes").selected_node_id(),
              "!4c000001")

    if failures:
        print("\nFAIL:", failures)
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))
