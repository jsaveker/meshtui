"""Home gateway, local mobile-DM injection, and bot routing simulations."""

import os
import json
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from meshtui.bot import BotRouter, OpenAIResponsesProvider, split_mesh_text
from meshtui.cli import run_send
from meshtui.gateway import Gateway, request_gateway
from meshtui.model import BROADCAST, ChatMessage, DeliveryStatus, SendReceipt
from meshtui.service import MeshService
from meshtui.store import Store


failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class HomeLink:
    def __init__(self, service):
        self.service = service
        self.sent = []
        self.stopped = False
        self.starts = 0

    def start(self):
        self.starts += 1
        self.service.handle_event("connected", {
            "my_node_id": "!10000001", "my_node_name": "Home Gateway",
            "protocol": "meshtastic", "device": "fake://home",
            "channels": [(0, "Primary"), (12, "#bots")],
        })

    def send(self, text, destination, message_id):
        self.sent.append((text, destination, message_id))
        protocol_id = 9000 + len(self.sent)
        if text == "field wait":
            timer = threading.Timer(
                0.1, lambda: self.service.handle_event("ack", protocol_id))
            timer.daemon = True
            timer.start()
        return SendReceipt(message_id, destination, DeliveryStatus.SENT,
                           protocol_id=protocol_id)

    def stop(self):
        self.stopped = True


class Provider:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def generate(self, prompt, *, sender, conversation):
        self.prompts.append((prompt, sender, conversation))
        return self.answer


class FakeHTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({
            "output": [{"content": [{"type": "output_text", "text": "safe reply"}]}],
        }).encode("utf-8")


captured_request = []


def fake_urlopen(request, timeout):
    captured_request.append((request, timeout))
    return FakeHTTPResponse()


print("OpenAI adapter is stateless and has no tools")
with patch("urllib.request.urlopen", fake_urlopen):
    answer = OpenAIResponsesProvider(api_key="test-only").generate(
        "hello", sender="!20000002", conversation="#bots")
body = json.loads(captured_request[0][0].data)
check("provider response parsed", answer, "safe reply")
check("provider sends an empty tool set", body["tools"], [])
check("provider forbids tool choice", body["tool_choice"], "none")
check("provider disables response storage", body["store"], False)


with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    print("home gateway owns the radio; local CLI injects the work-mobile DM")
    root = Path(tmp)
    store = Store(root / "mesh.db", flush_interval=0.01)
    check("store opens", store.open(), True)
    service = MeshService(store, retry_seconds=0.01)
    link = HomeLink(service)
    socket_path = root / "gateway.sock"
    gateway = Gateway(service, link, socket_path, reconnect_seconds=0.1)
    gateway.start()
    server_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    server_thread.start()
    for _ in range(50):
        if service.state.connected:
            break
        time.sleep(0.01)
    status = request_gateway({"command": "status"}, socket_path)
    check("gateway reports connected", status["connected"], True)
    check("socket is owner-only", stat.S_IMODE(os.stat(socket_path).st_mode), 0o600)
    service.handle_event("lost", "forced field-test disconnect")
    for _ in range(50):
        if link.starts >= 2 and service.state.connected:
            break
        time.sleep(0.02)
    check("gateway reopens a lost radio link", link.starts >= 2, True)
    wrong_protocol = request_gateway({
        "command": "send", "kind": "dm", "to": "!20000002", "text": "wrong",
        "protocol": "meshcore",
    }, socket_path)
    check("gateway rejects a protocol mismatch", wrong_protocol["ok"], False)
    result = request_gateway({
        "command": "send", "kind": "dm", "to": "!20000002",
        "text": "message from my home device",
    }, socket_path)
    check("local DM accepted", result["status"], "sent")
    check("home radio targets mobile node", link.sent[-1][1].node_id, "!20000002")
    sent_state = request_gateway({
        "command": "delivery", "message_id": result["message_id"],
    }, socket_path)
    check("local acceptance is not terminal delivery", sent_state["terminal"], False)
    service.handle_event("ack", result["protocol_id"])
    delivered_state = request_gateway({
        "command": "delivery", "message_id": result["message_id"],
    }, socket_path)
    check("gateway exposes end-to-end delivery", delivered_state["status"], "delivered")
    cli_code = run_send([
        "--socket", str(socket_path), "dm", "--to", "!20000002",
        "second", "message", "through", "the", "CLI",
    ])
    check("meshtui send dm command succeeds", cli_code, 0)
    check("CLI text is joined exactly", link.sent[-1][0], "second message through the CLI")
    wait_code = run_send([
        "--socket", str(socket_path), "dm", "--to", "!20000002",
        "--wait", "2", "field", "wait",
    ])
    check("send dm --wait observes the mesh ACK", wait_code, 0)

    channel_result = request_gateway({
        "command": "send", "kind": "channel", "channel": "#bots",
        "text": "bot channel injection",
    }, socket_path)
    check("named sparse channel accepted", channel_result["status"], "sent")
    check("named sparse channel resolves to slot 12", link.sent[-1][1].index, 12)

    print("bot is opt-in, duplicate-safe, tool-free, and RF-bounded")
    provider = Provider("weather " * 120 + "🌦️")
    router = BotRouter(service, provider, channel="#bots", cooldown_seconds=0)
    incoming = ChatMessage(
        ts=1000.125, from_id="!20000002", from_name="Work Mobile",
        to_id=BROADCAST, text="@ai summarize the weather", channel=12,
        packet_id=777,
    )
    before = len(link.sent)
    replies = router.route(incoming)
    check("provider invoked once", len(provider.prompts), 1)
    check("response bounded to three packets", len(replies) <= 3, True)
    bot_frames = link.sent[before:]
    check("all bot frames use real slot 12", {item[1].index for item in bot_frames}, {12})
    check("all bot frames fit Meshtastic", all(len(item[0].encode("utf-8")) <= 233
                                                for item in bot_frames), True)
    check("duplicate packet suppressed", router.route(incoming), [])
    off_channel = ChatMessage(
        ts=1001, from_id="!20000002", from_name="Work Mobile", to_id=BROADCAST,
        text="@ai do not answer here", channel=0, packet_id=778,
    )
    check("other channels ignored", router.route(off_channel), [])

    dm = ChatMessage(
        ts=1002, from_id="!20000002", from_name="Work Mobile", to_id="!10000001",
        text="@ai direct answer", channel=-1, packet_id=779,
    )
    dm_replies = router.route(dm)
    check("DM reply targets its sender", link.sent[-1][1].node_id, "!20000002")
    check("DM produced replies", bool(dm_replies), True)
    router.close()

    meshcore_chunks = split_mesh_text("x" * 500 + "🌦️", 133)
    check("MeshCore chunks bounded", len(meshcore_chunks) <= 3, True)
    check("MeshCore chunks byte-safe", all(len(chunk.encode("utf-8")) <= 133
                                            for chunk in meshcore_chunks), True)

    gateway.stop()
    server_thread.join(timeout=2)
    store.close()
    check("gateway removes only its socket", socket_path.exists(), False)

    print("duplicate suppression persists across restart")
    store = Store(root / "mesh.db", flush_interval=0.01)
    store.open()
    restored = MeshService(store)
    restored.state.my_node_id = "!10000001"
    restored.state.protocol = "meshtastic"
    restored.state.channels = [(12, "#bots")]
    second_provider = Provider("should not run")
    second_router = BotRouter(restored, second_provider, channel="#bots")
    check("replayed packet remains suppressed", second_router.route(incoming), [])
    check("provider not called after restart", second_provider.prompts, [])
    second_router.close()
    store.close()


print()
if failures:
    print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("PASS")
