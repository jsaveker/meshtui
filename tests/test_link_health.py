"""The link must treat a non-responding radio as dead so reconnect can kick in.

_drain_messages returns True when the radio answers (even 'no more') and False
when a command errors; the run loop counts consecutive False results and drops
the connection, which is what lets the gateway's reconnect loop take over
instead of the link spinning on a dead port forever.
"""
import asyncio, sys, types
from meshtui.meshcore_link import MeshCoreLink

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got==want else 'FAIL'} {n}")
    if got != want: failures.append(n)

class OKmc:
    class commands:
        @staticmethod
        async def get_msg(timeout=3):
            from meshcore import EventType
            return types.SimpleNamespace(type=EventType.NO_MORE_MSGS, payload=None)

class DeadMc:
    class commands:
        @staticmethod
        async def get_msg(timeout=3):
            raise OSError("device not responding")

link = MeshCoreLink(lambda k, p: None)
link.mc = OKmc()
check("healthy radio -> drain True", asyncio.run(link._drain_messages()), True)
link.mc = DeadMc()
check("dead radio -> drain False", asyncio.run(link._drain_messages()), False)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
