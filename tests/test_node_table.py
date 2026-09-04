"""Rendered node rows keep their fixed metric columns with Unicode names."""

import asyncio
import re
import time

from textual.app import App, ComposeResult

from meshtui.model import Node
from meshtui.state import MeshState
from meshtui.widgets.nodes import NodeTable

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


class NodeTableApp(App[None]):
    CSS = "NodeTable { width: 80; height: 12; }"

    def compose(self) -> ComposeResult:
        yield NodeTable(id="nodes")


async def main() -> int:
    app = NodeTableApp()
    async with app.run_test(size=(80, 12)) as pilot:
        now = time.time()
        state = MeshState()
        state.nodes["plain"] = Node(
            1, "plain", short_name="SAFE", long_name="Plain Radio",
            hops=4, packets=2, last_heard=now - 3600,
        )
        state.nodes["symbol"] = Node(
            2, "symbol", short_name="K5DG", long_name="K5DG-1⚡",
            hops=4, packets=2, last_heard=now - 3600,
        )

        table = app.query_one(NodeTable)
        table.sort_key = "name"
        table.render_state(state)
        await pilot.pause(0.2)

        lines = [strip.text for strip in app.screen._compositor.render_strips()]
        plain_row = next(line for line in lines if "Plain Radi" in line)
        symbol_row = next(line for line in lines if "K5DG-1" in line)
        check("rendered compact row substitutes the unstable symbol",
              "K5DG-1*" in symbol_row and "⚡" not in symbol_row, True)

        tail = re.compile(r"4\s+-\s+2\s+1h")
        plain_tail = tail.search(plain_row)
        symbol_tail = tail.search(symbol_row)
        check("Hop/Bat/Pkt/Age start at identical rendered offsets",
              (plain_tail.start() if plain_tail else None,
               symbol_tail.start() if symbol_tail else None),
              (plain_tail.start(), plain_tail.start()) if plain_tail else (None, None))

    if failures:
        print("\nFAIL:", failures)
        return 1
    print("\nPASS")
    return 0


raise SystemExit(asyncio.run(main()))
