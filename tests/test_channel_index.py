"""A sparse channel at slot 12 must be addressed as 12, not as tab position 2."""
import asyncio
from meshtui.app import MeshTUI
from meshtui.widgets.chat import ChatPane

class Spy:
    def __init__(self): self.sent = []
    def send_text(self, text, dest="^all", channel=0):
        self.sent.append((text, dest, channel)); return (True, 1)
    def stop(self): pass

async def main():
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(150, 44)) as pilot:
        await asyncio.sleep(2)
        spy = Spy(); app.link = spy; app.state.connected = True
        chat = app.query_one(ChatPane)
        app.state.channels = [(0, "Public"), (5, "#austin"), (12, "Ops")]
        await chat.set_channels(app.state.channels)
        await pilot.pause(0.5)
        tabs = [t.id for t in chat.tabs.query("Tab")]
        print(f"tab ids: {tabs}")
        assert tabs == ["ch0", "ch5", "ch12"], tabs

        chat.tabs.active = "ch12"
        await pilot.pause(0.4)
        kind, target = chat.active_target()
        print(f"active target: {kind} {target}")
        inp = app.query_one("#chat-input"); inp.focus(); await pilot.pause(0.2)
        inp.value = "to ops"
        await pilot.press("enter"); await pilot.pause(0.4)
        print(f"transmitted: {spy.sent}")
        if not spy.sent or spy.sent[0][2] != 12:
            print(f"FAIL: sent to channel {spy.sent[0][2] if spy.sent else 'nothing'}, expected 12")
            return 1
        print("PASS: message addressed to real channel index 12")
        return 0
import sys; sys.exit(asyncio.run(main()))
