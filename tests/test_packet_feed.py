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

    if failures:
        print(f"\nFAIL: {len(failures)}: {', '.join(failures)}")
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))
