"""Headless smoke test: run the demo mesh through the real app and assert state."""

import asyncio, os, sys, tempfile

from meshtui.app import MeshTUI
from meshtui.store import Store
from meshtui.widgets.chat import ChatPane
from meshtui.widgets.nodes import NodeTable
from meshtui.widgets.packets import PacketFeed


async def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="meshtui-smoke-")
    db = os.path.join(tmpdir, "mesh.db")
    store = Store(db, flush_interval=0.3)
    assert store.open(), store.error

    app = MeshTUI(demo=True, store=store)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        for _ in range(60):
            await pilot.pause(0.15)
            if app.state.stats.total >= 12 and app.state.chat:
                break

        problems = []
        if not app.state.connected:
            problems.append("never connected")
        if len(app.state.nodes) < 7:
            problems.append(f"only {len(app.state.nodes)} nodes")
        if app.state.stats.total < 5:
            problems.append(f"only {app.state.stats.total} packets")
        if not app.state.chat:
            problems.append("no chat messages")

        # exercise every keybinding
        for key in ("p", "p", "f", "f", "f", "s", "s", "s", "s", "s", "ctrl+l"):
            await pilot.press(key)
            await pilot.pause()

        # node detail modal
        app.query_one(NodeTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        if len(app.screen_stack) < 2:
            problems.append("node detail modal did not open")
        await pilot.press("escape")
        await pilot.pause()

        # send on a channel
        await pilot.press("slash")
        await pilot.pause()
        chat_input = app.query_one("#chat-input")
        chat_input.value = "hello from the smoke test"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause(0.2)
            if any(m.outgoing and m.acked for m in app.state.chat):
                break
        sent = [m for m in app.state.chat if m.outgoing]
        if not sent:
            problems.append("outgoing message not recorded")
        elif not any(m.acked for m in sent):
            problems.append("outgoing message never acked")

        # slash commands
        for cmd in ("/help", "/nodes", "/bogus", "/dm FLD yo there", "/trace RIDG", "/clear"):
            chat_input.value = cmd
            await pilot.press("enter")
            await pilot.pause(0.2)
        if not any(m.outgoing and m.is_dm for m in app.state.chat):
            problems.append("/dm did not send a direct message")

        # channel tab switching
        chat = app.query_one(ChatPane)
        for tab_id in ("ch1", "ch2", "ch0"):
            chat.tabs.active = tab_id
            await pilot.pause(0.2)

        # --- escape out of the chat input ---
        await pilot.press("slash"); await pilot.pause()
        if type(app.focused).__name__ != "ChatInput":
            problems.append("'/' did not focus the chat input")
        await pilot.press("escape"); await pilot.pause()
        if type(app.focused).__name__ == "ChatInput":
            problems.append("escape did not leave the chat input")

        # --- help overlay ---
        await pilot.press("question_mark"); await pilot.pause(0.3)
        if type(app.screen).__name__ != "HelpScreen":
            problems.append("'?' did not open help")
        await pilot.press("escape"); await pilot.pause(0.3)
        if type(app.screen).__name__ == "HelpScreen":
            problems.append("escape did not close help")

        # --- packet inspector ---
        feed = app.query_one("#packets")
        feed.focus()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        if not any(type(s).__name__ == "PacketInspector" for s in app.screen_stack):
            problems.append("packet inspector did not open")
        await pilot.press("escape")
        await pilot.pause()

        # --- feed follow / scrollback ---
        await pilot.press("up")
        await pilot.pause()
        if feed.follow:
            problems.append("scrolling up did not disable follow")
        await pilot.press("G")
        await pilot.pause()
        if not feed.follow:
            problems.append("G did not re-enable follow")

        # --- sparklines ---
        sparked = [n for n in app.state.nodes.values() if n.snr_history]
        if not sparked:
            problems.append("no node accumulated SNR history")

        # --- published-key decryption + audit screen ---
        import base64 as _b64, time as _t
        from meshtui import crypto as _crypto
        from meshtui.radio import flatten as _flatten
        from meshtastic.protobuf import mesh_pb2 as _m, portnums_pb2 as _pn

        _data = _m.Data(portnum=_pn.PortNum.TEXT_MESSAGE_APP, payload=b"audit probe")
        _pid, _node = 0x1234abcd, 0x33001111
        _ct = _crypto.decrypt(_crypto.expand_psk(bytes([4])), _pid, _node,
                              _data.SerializeToString())
        _pkt = _flatten({"from": _node, "to": 0xFFFFFFFF, "fromId": "!33001111",
                         "toId": "^all", "id": _pid, "channel": 77, "rxTime": _t.time(),
                         "encrypted": _b64.b64encode(_ct).decode()})
        if _pkt.decrypted_with != "simple3":
            problems.append(f"published-key decrypt failed: {_pkt.decrypted_with}")
        if "audit probe" not in _pkt.summary:
            problems.append("decrypted payload not surfaced in the summary")
        app.state.add_packet(_pkt)
        if not app.state.foreign_channels.get(77, None):
            problems.append("foreign channel not tracked")
        elif app.state.foreign_channels[77].key_label != "simple3":
            problems.append("foreign channel not flagged as using a published key")

        # a real random PSK must stay shut
        _strong = _crypto.decrypt(os.urandom(32), _pid, _node, _data.SerializeToString())
        _shut = _flatten({"from": _node, "to": 0xFFFFFFFF, "fromId": "!33001111",
                          "toId": "^all", "id": _pid, "channel": 78, "rxTime": _t.time(),
                          "encrypted": _b64.b64encode(_strong).decode()})
        if _shut.decrypted_with is not None or _shut.portnum != "ENCRYPTED":
            problems.append("a random-PSK packet was wrongly reported as decrypted")

        await pilot.press("a"); await pilot.pause(0.5)
        if type(app.screen).__name__ != "AuditScreen":
            problems.append("'a' did not open the audit screen")
        else:
            app.screen.view.render_report()
        await pilot.press("escape"); await pilot.pause(0.3)
        if type(app.screen).__name__ == "AuditScreen":
            problems.append("escape did not close the audit screen")

        # --- braille map ---
        await pilot.press("m")
        await pilot.pause(0.5)
        mapscreen = next((s for s in app.screen_stack
                          if type(s).__name__ == "MapScreen"), None)
        if mapscreen is None:
            problems.append("map screen did not open")
        else:
            # Count with default settings (rings + links on) BEFORE the
            # toggle sweep, otherwise `r`/`i` turn off most of what is drawn.
            rendered = mapscreen.view.render()
            braille = sum(1 for ch in rendered.plain if 0x2800 <= ord(ch) <= 0x28FF)
            labels = sum(1 for n in app.state.nodes.values()
                         if n.label[:6] in rendered.plain)
            if braille < 50:
                problems.append(f"map drew only {braille} braille cells")
            if labels < 3:
                problems.append(f"map labelled only {labels} nodes")
            print(f"map: {braille} braille cells, {labels} labels, "
                  f"status={mapscreen.view.status!r}")

            for key in ("plus", "minus", "up", "down", "left", "right", "c", "r", "i", "f"):
                await pilot.press(key)
                await pilot.pause(0.05)
            if mapscreen.view.show_rings or mapscreen.view.show_links:
                problems.append("map r/i toggles did not take effect")
            await pilot.press("escape")
            await pilot.pause()

        stats = app.state.stats
        print(f"nodes={len(app.state.nodes)} packets={stats.total} "
              f"chat={len(app.state.chat)} rate={stats.rate_per_min():.1f}/min "
              f"ports={dict(stats.by_port)}")
        print(f"me={app.state.my_node_name!r} channels={app.state.channels}")

        # --- persistence: close the app, verify the DB, reopen and restore ---
        await pilot.pause(1.0)

    store.close()
    info = Store(db).stats()
    print(f"db: {info}")
    if info.get("packets", 0) < 5:
        problems.append(f"only {info.get('packets')} packets persisted")
    if info.get("messages", 0) < 1:
        problems.append("no messages persisted")
    if info.get("nodes", 0) < 7:
        problems.append(f"only {info.get('nodes')} nodes persisted")

    store2 = Store(db, flush_interval=0.3)
    assert store2.open()
    app2 = MeshTUI(demo=True, store=store2)
    async with app2.run_test(size=(160, 48)) as pilot2:
        await pilot2.pause(0.5)
        if len(app2.state.nodes) < 7:
            problems.append("nodes not restored from db")
        if not app2.state.chat:
            problems.append("chat not restored from db")
        print(f"restored: {len(app2.state.nodes)} nodes, {len(app2.state.chat)} messages")
    store2.close()

    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("PASS")
    return 0


sys.exit(asyncio.run(main()))
