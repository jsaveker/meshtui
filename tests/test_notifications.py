"""Named-node reappearance and trace-failure notification rules."""

import time

from meshtui.model import BROADCAST, Node, Packet
from meshtui.notifications import NotificationBus, NtfyNotifier
from meshtui.service import MeshService

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


class Capture:
    def __init__(self):
        self.items = []

    def notify(self, notification):
        self.items.append(notification)


now = [2000.0]
service = MeshService(None)
capture = Capture()
bus = NotificationBus(service, named_nodes=["walker*"], trace_failures=True,
                      notifiers=[capture], active_seconds=300,
                      clock=lambda: now[0])
bus.start()
node = Node(num=0xAA000001, node_id="!aa000001", long_name="Walker One",
            last_heard=now[0], snr=2.0, hops=3)
service.state.nodes[node.node_id] = node
bus.handle_event("node", node)
time.sleep(0.05)
check("first matching appearance notifies", capture.items[-1].kind, "node_appeared")

bus.handle_event("packet", Packet(now[0] + 1, node.node_id, BROADCAST,
                                   "TEXT_MESSAGE_APP", "still here"))
time.sleep(0.05)
check("active duplicate is suppressed", len(capture.items), 1)

now[0] += 301
node.last_heard = now[0]
bus.handle_event("node", node)
time.sleep(0.05)
check("reappearance after window notifies", len(capture.items), 2)

bus.handle_event("error", "traceroute timed out for Walker One")
time.sleep(0.05)
check("trace failure notifies", capture.items[-1].kind, "trace_failed")
check("bus status exposes no secrets", bus.status()["sent"], 3)
bus.close()

try:
    NtfyNotifier("bad/topic")
    invalid = False
except ValueError:
    invalid = True
check("ntfy topic is one safe segment", invalid, True)

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
