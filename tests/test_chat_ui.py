"""Pop-out chat overlay and corner pane, driven by real key presses.

Covers the things most likely to break: switching channels stays in sync
between the two views, messages route to the right conversation, unread counts
track, and neither input can transmit on the wrong path.
"""

import asyncio
import sys
import time

from meshtui.app import MeshTUI
from meshtui.model import ChatMessage
from meshtui.widgets.chat import ChatPane
from meshtui.widgets.chat_overlay import ChatScreen

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class Spy:
    def __init__(self):
        self.sent = []
    def send_text(self, text, dest="^all", channel=0):
        self.sent.append((text, dest, channel)); return (True, 1)
    def stop(self): pass


async def main():
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(150, 40)) as pilot:
        await asyncio.sleep(2)
        st = app.state
        spy = Spy(); app.link = spy; st.connected = True
        st.channels = [(0, "Public"), (1, "#weather"), (5, "#ops")]
        st.protocol = "meshcore"
        app.query_one(ChatPane).set_channels(st)
        now = time.time()
        for ch, name, txt in ((1, "Solar", "storms rolling in"),
                              (5, "Ops", "repeater rebooted")):
            st.add_chat(ChatMessage(ts=now, from_id="!a" + str(ch), from_name=name,
                                    to_id="^all", text=txt, channel=ch))

        print("corner pane")
        st.active_target = ("channel", 1)
        app.query_one(ChatPane).rerender(st)
        check("corner shows channel label", app.query_one(ChatPane).active_target(),
              ("channel", 1))

        # a message on channel 5 while viewing channel 1 -> unread
        st.note_incoming(ChatMessage(ts=now, from_id="!x", from_name="Z",
                                     to_id="^all", text="ping", channel=5))
        check("unread tracked for other channel", st.unread.get(("channel", 5)), 1)

        print("\\nopen overlay with z")
        await pilot.press("z"); await pilot.pause(0.5)
        check("overlay opened", isinstance(app.screen, ChatScreen), True)
        overlay = app.screen
        check("overlay starts on the corner's channel", st.active_target, ("channel", 1))

        # sidebar has All + 3 channels
        check("sidebar target count", len(overlay._targets), 4)
        check("first is All activity", overlay._targets[0], ("all",))

        print("\\nswitching channel in the overlay")
        overlay._select_row(2)  # ("channel", 1) is row 2 (all, ch0, ch1)
        check("selecting row updates shared target", st.active_target, ("channel", 1))
        overlay._select_row(3)  # ("channel", 5)
        check("moved to ops channel", st.active_target, ("channel", 5))
        check("viewing a channel clears its unread", st.unread.get(("channel", 5)), 0)

        print("\\nsending from the overlay goes to the active channel")
        inp = overlay.query_one("#ov-input")
        inp.focus(); await pilot.pause(0.2)
        inp.value = "hello ops"
        await pilot.press("enter"); await pilot.pause(0.3)
        check("sent to channel 5", spy.sent[-1], ("hello ops", "^all", 5))

        print("\\nover-limit message is refused, nothing transmitted")
        before = len(spy.sent)
        inp.value = "x" * (app.max_payload + 20)
        await pilot.press("enter"); await pilot.pause(0.3)
        check("over-limit not sent from overlay", len(spy.sent), before)

        print("\\nscrolling back through history survives the refresh tick")
        for i in range(120):
            st.add_chat(ChatMessage(ts=now + i, from_id="!a5", from_name="Ops",
                                    to_id="^all", text=f"log line {i}", channel=5))
        overlay.render_conversation(); await pilot.pause(0.2)
        log = overlay.query_one("#ov-log")
        check("history overflows the viewport", log.is_vertical_scroll_end, True)
        await pilot.press("pageup"); await pilot.pause(0.2)
        check("pageup leaves the bottom", log.is_vertical_scroll_end, False)
        held = log.scroll_y
        await pilot.pause(2.0)  # two refresh ticks used to yank back to the end
        check("the tick keeps the reader's place", log.scroll_y, held)
        st.add_chat(ChatMessage(ts=now + 200, from_id="!a5", from_name="Ops",
                                to_id="^all", text="new arrival", channel=5))
        overlay.render_conversation(); await pilot.pause(0.3)
        check("a new message keeps the reader's place", log.scroll_y, held)
        for _ in range(30):
            await pilot.press("pagedown")
        await pilot.pause(0.2)
        check("pagedown returns to live-follow", log.is_vertical_scroll_end, True)

        print("\\n@mention autocomplete")
        st.upsert_node({"user": {"id": "!aa000001", "longName": "Pyratik_T1000"},
                        "lastHeard": time.time()})
        st.upsert_node({"user": {"id": "!aa000002", "longName": "Pyratik_Base"},
                        "lastHeard": time.time() - 60})
        st.add_chat(ChatMessage(ts=time.time(), from_id="channel:5:anonymous",
                                from_name="", to_id="^all",
                                text="🤷NBDY: chatty person", channel=5))
        inp.focus(); await pilot.pause(0.2)
        inp.value = ""
        await pilot.press("@", "p", "y", "r"); await pilot.pause(0.2)
        check("typing @partial shows candidates on the border",
              "Pyratik_T1000" in str(inp.border_subtitle), True)
        await pilot.press("tab"); await pilot.pause(0.2)
        check("tab completes the bracketed mention", inp.value, "@[Pyratik_T1000] ")
        await pilot.press("tab"); await pilot.pause(0.2)
        check("tab again cycles to the next candidate", inp.value, "@[Pyratik_Base] ")
        check("the active candidate is highlighted in the hints",
              "[reverse] Pyratik_Base [/reverse]" in str(inp.border_subtitle)
              and "⇧⇥ prev" in str(inp.border_subtitle), True)
        await pilot.press("shift+tab"); await pilot.pause(0.2)
        check("shift+tab cycles backwards", inp.value, "@[Pyratik_T1000] ")
        inp.value = "hello "
        inp.cursor_position = len(inp.value)
        await pilot.press("@", "n", "b", "d"); await pilot.pause(0.2)
        await pilot.press("tab"); await pilot.pause(0.2)
        check("substring match reaches emoji-led names mid-message",
              inp.value, "hello @[🤷NBDY] ")
        check("hints clear after completion... until the next @",
              inp._mention_query() is None, True)
        inp.value = ""

        print("\\nDM target resolution with two nodes sharing a short name")
        st.my_node_id = "!11110000"
        me2 = st.upsert_node({"user": {"id": "!11110000", "longName": "Field Base",
                                       "shortName": "Fiel"}, "lastHeard": time.time()})
        me2.is_self = True
        st.upsert_node({"user": {"id": "!22220000", "longName": "Field Mobile",
                                 "shortName": "Fiel"}, "lastHeard": time.time()})
        st.upsert_node({"user": {"id": "!33330000", "longName": "Field Mobile",
                                 "shortName": "Fiel"}})  # ghost: never heard
        node, rest, problem = app._resolve_target("Fiel hello there")
        check("shared short name resolves to the OTHER node, never self",
              (node and node.node_id, rest, problem), ("!22220000", "hello there", None))
        node, rest, problem = app._resolve_target("Field Mobile hello")
        check("multi-word names resolve, ghosts lose to the living",
              (node and node.node_id, rest), ("!22220000", "hello"))
        node, _, problem = app._resolve_target("Field Base yo")
        check("targeting the radio itself is refused with a reason",
              (node, "this radio" in (problem or "")), (None, True))
        st.upsert_node({"user": {"id": "!33330000"}, "lastHeard": time.time()})
        node, _, problem = app._resolve_target("Field Mobile hi")
        check("two living namesakes are reported as ambiguous with ids",
              (node, "!22220000" in (problem or "") and "!33330000" in (problem or "")),
              (None, True))

        print("\\ncorner and overlay stay in sync")
        await pilot.press("escape"); await pilot.pause(0.3)
        check("overlay closed", isinstance(app.screen, ChatScreen), False)
        check("corner reflects the channel chosen in the overlay",
              app.query_one(ChatPane).active_target(), ("channel", 5))

    if failures:
        print(f"\\nFAIL: {len(failures)}: {', '.join(failures)}")
        return 1
    print("\\nPASS")
    return 0


sys.exit(asyncio.run(main()))
