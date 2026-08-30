"""The packet feed mixes packets and notice lines (errors, traceroute output).

A resize re-lays-out the feed; a notice row stored as a bare None used to crash
that path with 'NoneType has no attribute portnum'. Rows now carry either a
Packet or the notice's renderable.
"""

import asyncio
import sys

from meshtui.app import MeshTUI
from meshtui.model import Packet
from meshtui.widgets.packets import PacketFeed

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


async def main():
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(1.5)
        # Stop synthetic traffic so row counts are deterministic.
        if app.link is not None:
            app.link.stop()
        await asyncio.sleep(0.3)
        feed = app.query_one(PacketFeed)
        feed.clear_feed()

        # interleave packets and notices, as a real session does
        for i in range(5):
            feed.add(Packet(ts=1787871679.0 + i, from_id="!bc20c203", to_id="^all",
                            portnum="TEXT_MESSAGE_APP", summary=f"msg {i}",
                            channel=0), app.state)
        feed.write_notice("  traceroute to REP: 2 hops", "bold bright_cyan")
        feed.write_notice("      +6.2dB  -> SNTL", "green")
        feed.add(Packet(ts=1787871700.0, from_id="!00000000", to_id="^all",
                        portnum="RXLOG_APP", summary="rx", channel=0), app.state)

        check("rows mix packets and notices", len(feed._rows), 8)
        check("no None rows stored", any(r is None for r in feed._rows), False)

        # the crash: a resize re-renders every row
        feed.on_resize()
        await pilot.pause(0.1)
        check("survives resize with notices", len(feed._rows), 8)
        check("still no None rows after resize",
              any(r is None for r in feed._rows), False)
        check("notices preserved across resize",
              sum(1 for r in feed._rows if not isinstance(r, Packet)), 2)

        for _ in range(3):
            feed.on_resize()
        check("repeated resizes are stable", len(feed._rows), 8)

        # selecting a notice row must not be treated as a packet
        notice_row = next(i for i, r in enumerate(feed._rows) if not isinstance(r, Packet))
        feed.move_cursor(row=notice_row)
        await pilot.pause(0.1)
        check("notice row is not a selectable packet", feed.selected_packet(), None)

        # selecting a packet row still works
        pkt_row = next(i for i, r in enumerate(feed._rows) if isinstance(r, Packet))
        feed.move_cursor(row=pkt_row)
        await pilot.pause(0.1)
        check("packet row selectable", feed.selected_packet() is not None, True)

        # follow must track the newest row as packets arrive, and stop when
        # the user scrolls up (these lived in dead code once - regression)
        def mk(summary):
            return Packet(ts=1787871800.0, from_id="!bc20c203", to_id="^all",
                          portnum="TEXT_MESSAGE_APP", summary=summary, channel=0)
        feed.follow = True
        for i in range(30):
            feed._write_line(mk(f"follow packet {i}"), app.state)
        await pilot.pause(0.2)
        check("follow keeps the cursor on the newest row",
              feed.cursor_row, len(feed._rows) - 1)
        check("follow keeps the view scrolled to the end",
              feed.is_vertical_scroll_end, True)
        feed.action_cursor_up()
        anchored = feed.cursor_row
        feed._write_line(mk("while scrolled up"), app.state)
        await pilot.pause(0.1)
        check("scrolling up stops the feed from yanking", feed.cursor_row, anchored)

        # the 'no rf log' filter hides unattributed RF noise
        feed.cycle_filter(app.state)
        check("second filter is no rf log", feed.filter_name, "no rf log")
        rxlog_rows = sum(1 for r in feed._rows
                         if isinstance(r, Packet) and r.portnum == "RXLOG_APP")
        check("rf log rows are filtered out", rxlog_rows, 0)
        for _ in range(len(__import__('meshtui.widgets.packets',
                                      fromlist=['FILTERS']).FILTERS) - 1):
            feed.cycle_filter(app.state)
        check("filter cycles back to all", feed.filter_name, "all")

        # the row cap must actually trim (also once dead code)
        import meshtui.widgets.packets as packets_module
        real_max = packets_module.MAX_ROWS
        packets_module.MAX_ROWS = len(feed._rows) + 3
        try:
            for i in range(8):
                feed._write_line(mk(f"cap packet {i}"), app.state)
            check("the feed trims to its row cap",
                  len(feed._rows), packets_module.MAX_ROWS)
        finally:
            packets_module.MAX_ROWS = real_max

    if failures:
        print(f"\nFAIL: {len(failures)}: {', '.join(failures)}")
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))
