"""Headless single-owner gateway and its local Unix-socket client."""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path
from typing import Any

from .bot import BotRouter, OpenAIResponsesProvider
from .meshcore_link import MeshCoreLink, probe_meshcore
from .model import ChannelRef, DeliveryStatus, PeerRef, SendReceipt
from .radio import DemoLink, RadioLink, SerialLink, TCPLink, find_serial_ports
from .service import MeshService
from .store import Store

log = logging.getLogger(__name__)


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / f"meshtui-{os.getuid()}.sock"


def receipt_dict(receipt: SendReceipt) -> dict[str, Any]:
    return {
        "ok": receipt.status not in (DeliveryStatus.FAILED, DeliveryStatus.EXPIRED),
        "message_id": receipt.message_id,
        "status": receipt.status.value,
        "protocol_id": receipt.protocol_id,
        "detail": receipt.detail,
    }


def request_gateway(request: dict[str, Any], socket_path: Path | str | None = None,
                    timeout: float = 5.0) -> dict[str, Any]:
    path = str(socket_path or default_socket_path())
    data = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > 65536:
        raise ValueError("gateway request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        client.sendall(data)
        reader = client.makefile("rb")
        line = reader.readline(65537)
    if not line or len(line) > 65536:
        raise RuntimeError("invalid response from gateway")
    result = json.loads(line.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("invalid response from gateway")
    return result


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65537)
        if not line or len(line) > 65536:
            response = {"ok": False, "error": "request must be one JSON line under 64 KiB"}
        else:
            try:
                request = json.loads(line.decode("utf-8"))
                response = self.server.gateway.handle_request(request)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class Gateway:
    """Own one radio connection and expose safe local send operations."""

    def __init__(self, service: MeshService, link: RadioLink,
                 socket_path: Path | str | None = None,
                 bot_router: BotRouter | None = None,
                 reconnect_seconds: float = 5.0) -> None:
        self.service = service
        self.link = link
        self.socket_path = Path(socket_path or default_socket_path())
        self.bot_router = bot_router
        self.reconnect_seconds = max(0.1, reconnect_seconds)
        self._server: _UnixServer | None = None
        self._link_thread: threading.Thread | None = None
        self._retry_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._serving = threading.Event()
        self._owns_socket = False
        self.service.attach_link(link)
        self.service.add_listener(self._log_event)
        if bot_router is not None:
            self.service.add_listener(bot_router.handle_event)

    @staticmethod
    def _log_event(kind: str, payload: Any) -> None:
        if kind in ("error", "lost"):
            log.warning("%s", payload)
        elif kind == "status":
            log.info("%s", payload)
        elif kind == "connected":
            log.info("radio connected")

    def _prepare_socket(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.socket_path.exists():
            return
        metadata = self.socket_path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError(f"refusing to replace unowned path {self.socket_path}")
        try:
            request_gateway({"command": "status"}, self.socket_path, timeout=0.5)
        except ConnectionRefusedError:
            self.socket_path.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot inspect existing gateway socket: {exc}") from exc
        else:
            raise RuntimeError(f"another meshtui gateway owns {self.socket_path}")

    def start(self) -> None:
        self._prepare_socket()
        self.service.restore()
        server = _UnixServer(str(self.socket_path), _RequestHandler)
        self._owns_socket = True
        server.gateway = self  # type: ignore[attr-defined]
        os.chmod(self.socket_path, 0o600)
        self._server = server
        self._link_thread = threading.Thread(
            target=self._radio_loop, name="gateway-radio", daemon=True)
        self._link_thread.start()
        self._retry_thread = threading.Thread(
            target=self._retry_loop, name="gateway-outbox", daemon=True)
        self._retry_thread.start()

    def serve_forever(self) -> None:
        if self._server is None:
            self.start()
        assert self._server is not None
        self._serving.set()
        try:
            self._server.serve_forever(poll_interval=0.5)
        finally:
            self._serving.clear()

    def _retry_loop(self) -> None:
        while not self._stop.wait(1.0):
            self.service.process_outbox()

    def _radio_loop(self) -> None:
        """Keep reopening a failed or disconnected companion link."""
        while not self._stop.is_set():
            self.link.start()
            while self.service.state.connected and not self._stop.wait(0.5):
                pass
            if self._stop.is_set():
                return
            self.service.state.connected = False
            self.link.stop()
            self.service.handle_event(
                "status", f"radio unavailable; reconnecting in {self.reconnect_seconds:g}s")
            if self._stop.wait(self.reconnect_seconds):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            if self._serving.is_set():
                self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.link.stop()
        if self._link_thread is not None:
            self._link_thread.join(timeout=3.0)
            self._link_thread = None
        if self._retry_thread is not None:
            self._retry_thread.join(timeout=2.0)
            self._retry_thread = None
        self.service.remove_listener(self._log_event)
        if self.bot_router is not None:
            self.service.remove_listener(self.bot_router.handle_event)
            self.bot_router.close()
        if self._owns_socket:
            try:
                if self.socket_path.exists():
                    self.socket_path.unlink()
            except OSError:
                pass
            self._owns_socket = False

    def _channel_ref(self, selector: Any, protocol: str) -> ChannelRef:
        try:
            index = int(selector)
        except (TypeError, ValueError):
            wanted = str(selector or "").lstrip("#").casefold()
            for position, item in enumerate(self.service.state.channels):
                if isinstance(item, tuple):
                    slot, name = int(item[0]), str(item[1])
                else:
                    slot, name = position, str(item)
                if name.lstrip("#").casefold() == wanted:
                    return ChannelRef(protocol, slot, name)
            raise ValueError(f"unknown channel {selector!r}; use its numeric slot while offline")
        if not 0 <= index <= 255:
            raise ValueError("channel slot must be between 0 and 255")
        return ChannelRef(protocol, index, self.service.state.channel_name(index))

    def handle_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        command = request.get("command")
        if command == "status":
            pending = sum(not item.terminal for item in self.service.outbox.values())
            return {
                "ok": True, "connected": self.service.state.connected,
                "protocol": self.service.state.protocol,
                "node_id": self.service.state.my_node_id,
                "device": self.service.state.device_path,
                "outbox_pending": pending,
            }
        if command == "delivery":
            message_id = str(request.get("message_id") or "").strip()
            if not message_id:
                return {"ok": False, "error": "delivery requires a message_id"}
            snapshot = self.service.delivery_snapshot(message_id)
            if snapshot is None:
                return {"ok": False, "error": f"unknown message id {message_id}"}
            return {"ok": True, **snapshot}
        if command != "send":
            return {"ok": False, "error": "supported commands: status, delivery, send"}
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text must not be empty"}
        configured_protocol = self.service.state.protocol
        requested_protocol = request.get("protocol")
        if requested_protocol and str(requested_protocol) != configured_protocol:
            return {"ok": False, "error":
                    f"gateway owns a {configured_protocol} link, not {requested_protocol}"}
        protocol = configured_protocol
        kind = request.get("kind")
        try:
            if kind == "dm":
                target = str(request.get("to") or "").strip()
                if not target:
                    raise ValueError("a DM requires a target node id")
                public_key = request.get("public_key")
                if public_key is not None:
                    if protocol != "meshcore":
                        raise ValueError("a full public key is only valid for MeshCore")
                    public_key = str(public_key).lower().removeprefix("0x")
                    try:
                        decoded = bytes.fromhex(public_key)
                    except ValueError as exc:
                        raise ValueError("MeshCore public key must be 64 hex characters") from exc
                    if len(decoded) != 32:
                        raise ValueError("MeshCore public key must be 64 hex characters")
                    expected = f"!{public_key[:8]}"
                    if target.casefold() != expected:
                        raise ValueError(f"node id does not match public key prefix {expected}")
                destination = PeerRef(protocol, target, public_key)
            elif kind == "channel":
                destination = self._channel_ref(request.get("channel"), protocol)
            else:
                raise ValueError("send kind must be dm or channel")
            return receipt_dict(self.service.send_message(text.strip(), destination))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}


def choose_gateway_link(emit, *, port: str | None = None, host: str | None = None,
                        protocol: str = "auto", demo: bool = False) -> tuple[RadioLink, str]:
    if demo:
        return DemoLink(emit), "meshtastic"
    selected = protocol
    serial_port = port
    if selected == "auto" and not host:
        serial_port = serial_port or next(iter(find_serial_ports()), None)
        selected = "meshcore" if serial_port and probe_meshcore(serial_port) else "meshtastic"
    elif selected == "auto":
        selected = "meshtastic"
    if selected == "meshcore":
        return MeshCoreLink(emit, port=serial_port or next(iter(find_serial_ports()), None),
                            host=host), selected
    if host:
        return TCPLink(emit, host), selected
    return SerialLink(emit, serial_port), selected


def build_gateway(*, store: Store, port: str | None = None, host: str | None = None,
                  protocol: str = "auto", demo: bool = False,
                  socket_path: Path | str | None = None, bot_channel: str | int | None = None,
                  ai_model: str = "gpt-5-mini", ai_endpoint: str | None = None) -> Gateway:
    service = MeshService(store)
    link, selected = choose_gateway_link(
        service.handle_event, port=port, host=host, protocol=protocol, demo=demo)
    service.state.protocol = selected
    router = None
    if bot_channel is not None:
        provider = OpenAIResponsesProvider(model=ai_model)
        if ai_endpoint:
            provider.endpoint = ai_endpoint
        router = BotRouter(service, provider, channel=bot_channel)
    return Gateway(service, link, socket_path, router)
