"""The paths explorer must open, render its table, draw the selected route,
and close - against real observations injected into app state."""
import asyncio, sys, time

from meshtui.app import MeshTUI
from meshtui.pathcalc import PathObservation
from meshtui.widgets.paths import PathScreen

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)


async def main() -> int:
    app = MeshTUI(demo=True, store=None)
    async with app.run_test(size=(160, 45)) as pilot:
        await asyncio.sleep(1.5)
        st = app.state
        st.protocol = "meshcore"
        st.my_node_id = "!c0decafe"
        me = st.upsert_node({"user": {"id": "!c0decafe", "longName": "Base"},
                             "position": {"latitude": 0.30, "longitude": 0.30}})
        me.is_self = True
        st.upsert_node({"user": {"id": "!aa000001", "longName": "Far Node"},
                        "position": {"latitude": 0.00, "longitude": 0.10}})
        st.upsert_node({"user": {"id": "!4c000001", "longName": "Hilltop"},
                        "position": {"latitude": 0.20, "longitude": 0.20}})
        now = time.time()
        st.note_path(PathObservation(ts=now - 60, kind="advert", origin_id="!aa000001",
                                     origin_name="Far Node", path="4c", hops=1, snr=-4.0))
        st.note_path(PathObservation(ts=now - 30, kind="channel", origin_name="Far Node",
                                     path="4c", hops=1, snr=2.0, channel=2))
        st.note_path(PathObservation(ts=now - 10, kind="channel", origin_name="Mystery",
                                     hops=0))

        await pilot.press("v"); await pilot.pause(0.5)
        check("v opens the paths explorer", isinstance(app.screen, PathScreen), True)
        screen = app.screen
        table = screen.query_one("#paths-table")
        check("all observations are listed", table.row_count, 3)

        canvas_text = screen.query_one("#paths-canvas").render()
        check("newest first: the direct observation renders a no-draw notice",
              "not enough positioned" in str(canvas_text), True)

        await pilot.press("down"); await pilot.pause(0.3)
        canvas_text = str(screen.query_one("#paths-canvas").render())
        detail_text = str(screen.query_one("#paths-detail").render())
        check("a positioned path draws braille", any(0x2800 <= ord(c) <= 0x28FF
                                                     for c in canvas_text), True)
        check("the hop breakdown names the repeater", "Hilltop" in detail_text, True)
        check("distances are computed", "route ~" in detail_text
              and "direct ~" in detail_text, True)

        status = str(screen.query_one("#paths-status").render())
        check("header aggregates the dataset", "3 observations" in status
              and "busiest hop: Hilltop" in status, True)

        await pilot.press("escape"); await pilot.pause(0.3)
        check("escape closes the explorer", isinstance(app.screen, PathScreen), False)

    if failures:
        print(f"\nFAIL: {failures}")
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))
