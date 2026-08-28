"""A sparse channel at slot 12 must be addressed as 12, not by its position.

MeshCore channel slots are not contiguous. The guarantee: selecting a channel
that lives at slot 12 and sending must transmit on channel 12.
"""
import asyncio, sys
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
        app.state.channels = [(0, "Public"), (5, "#austin"), (12, "Ops")]
        chat = app.query_one(ChatPane)
        chat.set_channels(app.state)

        ok = chat.goto_channel(12, app.state)
        print(f"goto slot 12: {ok}, active={chat.active_target()}")
        assert ok and chat.active_target() == ("channel", 12), chat.active_target()

        inp = app.query_one("#chat-input"); inp.focus(); await pilot.pause(0.2)
        inp.value = "to ops"
        await pilot.press("enter"); await pilot.pause(0.4)
        print(f"transmitted: {spy.sent}")
        if not spy.sent or spy.sent[0][2] != 12:
            print(f"FAIL: sent to channel {spy.sent[0][2] if spy.sent else None}, expected 12")
            return 1
        print("PASS: message addressed to real channel index 12")
        return 0


sys.exit(asyncio.run(main()))
