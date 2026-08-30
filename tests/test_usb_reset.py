"""A wedged radio must be told apart from a missing one, and the gateway must
answer a wedge with one USB reset per episode.

The wedge signature is EPIPE on open while the tty node still exists (firmware
stalling control requests); a pulled cable removes the node instead. The old
code lumped every OSError into "unplug and replug", which sent the user down
the reboot path when only a device reset could help.
"""
import sys, tempfile, threading, types

from meshtui.meshcore_link import MeshCoreLink
from meshtui import gateway as gateway_mod
from meshtui import usbreset

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

# --------------------------------------------------- wedge classification
with tempfile.NamedTemporaryFile(prefix="fake-ttyACM") as tty:
    link = MeshCoreLink(lambda k, p: None, port=tty.name)
    check("EPIPE + node present -> wedged",
          link._is_usb_wedge(BrokenPipeError(32, "Broken pipe")), True)
    check("other OSError -> not wedged",
          link._is_usb_wedge(OSError(5, "I/O error")), False)
    link.usb_wedged = True
    check("wedged message names a USB reset",
          "USB reset" in link._connect_error(BrokenPipeError(32, "Broken pipe")), True)
    link.usb_wedged = False
    check("non-wedged message stays generic",
          "could not connect" in link._connect_error(OSError(5, "I/O error")), True)

link = MeshCoreLink(lambda k, p: None, port="/dev/does-not-exist-meshtui")
check("EPIPE + node gone -> not wedged (cable pulled)",
      link._is_usb_wedge(BrokenPipeError(32, "Broken pipe")), False)

link = MeshCoreLink(lambda k, p: None, host="10.0.0.1:4403")
check("TCP link never classifies as wedged",
      link._is_usb_wedge(BrokenPipeError(32, "Broken pipe")), False)

# ------------------------------------------- silent radio at connect time
import asyncio, tempfile

with tempfile.NamedTemporaryFile(prefix="fake-ttyACM") as tty:
    events = []
    link = MeshCoreLink(lambda k, p: events.append((k, p)), port=tty.name)
    async def _never():
        await asyncio.sleep(3600)
    link._connect = lambda: _never()
    real_wait_for = asyncio.wait_for
    asyncio.wait_for = lambda coro, timeout: real_wait_for(coro, timeout=0.05)
    try:
        asyncio.run(link._run())
    finally:
        asyncio.wait_for = real_wait_for
    check("a silent radio times out instead of hanging the gateway",
          any(k == "error" and "did not answer" in p for k, p in events), True)
    check("a silent radio counts as wedged (so the USB reset engages)",
          link.usb_wedged, True)

# --------------------------------------------------- tty -> usbfs mapping
check("unknown tty maps to no usbfs node",
      usbreset.usb_device_node("/dev/ttyNOPE99"), None)
check("reset without a port is refused",
      usbreset.try_usb_reset(None)[0], False)

# --------------------------------------------------- gateway reset policy
class WedgedLink:
    """start() fails like a wedged radio; counts resets the gateway asks for."""
    port = "/dev/ttyFAKE0"
    usb_wedged = True
    def start(self): pass
    def stop(self): pass

class FakeService:
    def __init__(self):
        self.state = types.SimpleNamespace(connected=False)
        self.events = []
    def handle_event(self, kind, payload):
        self.events.append((kind, str(payload)))

resets = []
def fake_reset(port):
    resets.append(port)
    return False, "no permission (test)"

gw = gateway_mod.Gateway.__new__(gateway_mod.Gateway)
gw.service = FakeService()
gw.link = WedgedLink()
gw.reconnect_seconds = 0.01
gw._stop = threading.Event()

real_reset, gateway_mod.try_usb_reset = gateway_mod.try_usb_reset, fake_reset
try:
    runner = threading.Thread(target=gw._radio_loop, daemon=True)
    runner.start()
    # Let several wedged connect cycles elapse, then stop the loop.
    for _ in range(200):
        if len(gw.service.events) >= 12:
            break
        threading.Event().wait(0.01)
    gw._stop.set()
    runner.join(timeout=2.0)
finally:
    gateway_mod.try_usb_reset = real_reset

check("reset waits for two wedged attempts, then fires once per episode",
      resets, ["/dev/ttyFAKE0"])
check("reset failure is surfaced as an error event",
      any(k == "error" and "no permission" in p for k, p in gw.service.events), True)

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
