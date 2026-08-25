"""Connect to a real node over serial and report what the app sees."""

import asyncio, sys, time

from meshtui.app import MeshTUI

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


async def main() -> int:
    app = MeshTUI(port=None, demo=False)
    async with app.run_test(size=(150, 44)) as pilot:
        deadline = time.time() + SECONDS
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if app.state.connected and time.time() > deadline - SECONDS + 25:
                break

        s = app.state
        print("=" * 70)
        print(f"connected : {s.connected}")
        print(f"device    : {s.device_path}")
        print(f"my node   : {s.my_node_name!r}  {s.my_node_id}")
        print(f"firmware  : {s.firmware!r}")
        print(f"channels  : {s.channels}")
        print(f"nodes     : {len(s.nodes)}")
        print(f"packets   : {s.stats.total}  ({s.stats.rate_per_min():.1f}/min)")
        print(f"port mix  : {dict(s.stats.by_port)}")
        print("-" * 70)
        for n in s.sorted_nodes("heard")[:15]:
            age = "-" if n.last_heard is None else f"{int(time.time() - n.last_heard)}s"
            print(f"  {n.node_id}  {n.short_name:<5} {n.long_name[:22]:<22} "
                  f"hw={n.hw_model:<12} snr={n.snr} hops={n.hops} "
                  f"bat={n.battery} age={age} pkts={n.packets}")
        print("-" * 70)
        for p in list(s.packets)[-15:]:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(p.ts))} "
                  f"{p.portnum:<20} {p.from_id} -> {p.to_id}  snr={p.snr} :: {p.summary[:60]}")
        print("=" * 70)
        return 0 if s.connected else 1


sys.exit(asyncio.run(main()))
