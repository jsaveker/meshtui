"""Headless single-owner gateway and its local Unix-socket client."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import socket
import socketserver
import stat
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .bot import BotRouter, OpenAIResponsesProvider, PathBot, TelemetryBot, TestBot
from .events import connected_info, event_from_wire, event_to_wire, node_record
from .meshcore_link import MeshCoreLink, probe_meshcore
from .model import ChannelRef, DeliveryStatus, DestinationRef, PeerRef, SendReceipt
from .radio import DemoLink, RadioLink, SerialLink, TCPLink, find_serial_ports
from .service import MeshService
from .store import Store
from .usbreset import try_usb_reset

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


# How long a subscribed connection may sit idle before the gateway writes a
# ping just to learn whether the client is still there.
STREAM_PING_SECONDS = 15.0


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65537)
        if not line or len(line) > 65536:
            response = {"ok": False, "error": "request must be one JSON line under 64 KiB"}
        else:
            try:
                request = json.loads(line.decode("utf-8"))
                if isinstance(request, dict) and request.get("command") == "subscribe":
                    self._stream(request)
                    return
                response = self.server.gateway.handle_request(request)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                response = {"ok": False, "error": str(exc)}
        try:
            self._write_line(response)
        except OSError:
            # The client hung up before reading the reply (a quitting TUI).
            # Not an error - and socketserver would print a full traceback.
            pass

    def _write_line(self, obj: dict[str, Any]) -> None:
        # default=repr matches Store.add_packet: raw radio dicts can carry
        # bytes, and a stream must never die over one unserializable field.
        self.wfile.write(
            (json.dumps(obj, separators=(",", ":"), default=repr) + "\n").encode("utf-8"))

    def _stream(self, request: dict[str, Any]) -> None:
        """Answer a subscribe: snapshot first, then live events until one side
        hangs up. ThreadingMixIn gives this loop its own thread."""
        gateway = self.server.gateway  # type: ignore[attr-defined]
        header, snapshot, events = gateway.subscribe(request)
        try:
            self._write_line(header)
            for item in snapshot:
                self._write_line(item)
            while True:
                try:
                    item = events.get(timeout=STREAM_PING_SECONDS)
                except queue.Empty:
                    # Idle: probe the connection so a vanished client is
                    # noticed even when the mesh is quiet.
                    self._write_line({"event": "ping"})
                    continue
                if item is None:  # server shutting down or client too slow
                    break
                self._write_line(item)
        except OSError:
            pass  # client went away; nothing to clean up but the registration
        finally:
            gateway.unsubscribe(events)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class Gateway:
    """Own one radio connection and expose safe local send operations."""

    def __init__(self, service: MeshService, link: RadioLink,
                 socket_path: Path | str | None = None,
                 bot_router: BotRouter | None = None,
                 path_bot: PathBot | None = None,
                 test_bot: TestBot | None = None,
                 map_uploader: Any | None = None,
                 weather_bot: Any | None = None,
                 sensor_bot: Any | None = None,
                 telemetry_bot: TelemetryBot | None = None,
                 event_sinks: list[Any] | None = None,
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
        self._subscribers: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self.path_bot = path_bot
        self.test_bot = test_bot
        self.map_uploader = map_uploader
        self.weather_bot = weather_bot
        self.sensor_bot = sensor_bot
        self.telemetry_bot = telemetry_bot
        self.event_sinks = list(event_sinks or ())
        self._sinks_started = False
        self.service.attach_link(link)
        self.service.add_listener(self._log_event)
        self.service.add_listener(self._broadcast)
        for observer in (bot_router, path_bot, test_bot, map_uploader, telemetry_bot,
                         *self.event_sinks):
            if observer is not None:
                self.service.add_listener(observer.handle_event)

    @staticmethod
    def _log_event(kind: str, payload: Any) -> None:
        if kind in ("error", "lost"):
            log.warning("%s", payload)
        elif kind == "status":
            log.info("%s", payload)
        elif kind == "connected":
            log.info("radio connected")

    # ------------------------------------------------------------ streaming

    SNAPSHOT_LIMIT = 1000

    def subscribe(self, request: dict[str, Any]) -> tuple[
            dict[str, Any], list[dict[str, Any]], queue.Queue]:
        """Register a stream client and build its catch-up snapshot.

        Both happen while holding the service lock, so between the last
        snapshot item and the first queued live event no notification can be
        missed or delivered twice.
        """
        def limit(key: str, fallback: int) -> int:
            try:
                return max(0, min(self.SNAPSHOT_LIMIT, int(request.get(key, fallback))))
            except (TypeError, ValueError):
                return fallback

        chat_limit = limit("chat", 200)
        packet_limit = limit("packets", 200)
        events: queue.Queue = queue.Queue(maxsize=self.SNAPSHOT_LIMIT)
        with self.service.lock:
            with self._subs_lock:
                self._subscribers.append(events)
            state = self.service.state
            header = {"ok": True, "connected": state.connected,
                      "protocol": state.protocol, "node_id": state.my_node_id,
                      "device": state.device_path}
            snapshot: list[dict[str, Any]] = []
            if state.connected:
                snapshot.append({"event": "connected", "payload": connected_info(state)})
            for node in state.nodes.values():
                snapshot.append({"event": "node", "payload": node_record(node)})
            for message in list(state.chat)[-chat_limit:] if chat_limit else []:
                item = event_to_wire("chat", message)
                if item is not None:
                    snapshot.append(item)
            for packet in list(state.packets)[-packet_limit:] if packet_limit else []:
                item = event_to_wire("packet", packet)
                if item is not None:
                    snapshot.append(item)
        return header, snapshot, events

    def unsubscribe(self, events: queue.Queue) -> None:
        with self._subs_lock:
            if events in self._subscribers:
                self._subscribers.remove(events)

    def _broadcast(self, kind: str, payload: Any) -> None:
        """Service listener: fan each event out to every subscribed client.

        Called under the service lock, so it must never block: a queue that
        fills up marks a client too slow to keep, and it gets hung up on.
        """
        with self._subs_lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return
        try:
            item = event_to_wire(kind, payload)
        except Exception:  # noqa: BLE001 - one odd payload must not kill the stream
            log.debug("unbroadcastable %s event", kind, exc_info=True)
            return
        if item is None:
            return
        for events in subscribers:
            try:
                events.put_nowait(item)
            except queue.Full:
                self.unsubscribe(events)
                self._hang_up(events)

    @staticmethod
    def _hang_up(events: queue.Queue) -> None:
        """Get a None to the front of a possibly-full queue so its writer exits."""
        try:
            events.get_nowait()
        except queue.Empty:
            pass
        try:
            events.put_nowait(None)
        except queue.Full:
            pass

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
        for sink in self.event_sinks:
            start = getattr(sink, "start", None)
            if start is not None:
                start()
        self._sinks_started = True
        self._link_thread = threading.Thread(
            target=self._radio_loop, name="gateway-radio", daemon=True)
        self._link_thread.start()
        self._retry_thread = threading.Thread(
            target=self._retry_loop, name="gateway-outbox", daemon=True)
        self._retry_thread.start()
        threading.Thread(target=self._watchdog_loop,
                         name="gateway-watchdog", daemon=True).start()
        if self.weather_bot is not None:
            self.weather_bot.start(self._stop)
        if self.sensor_bot is not None:
            self.sensor_bot.start(self._stop)

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
        last_snapshot = time.monotonic()
        while not self._stop.wait(1.0):
            self.service.process_outbox()
            now = time.monotonic()
            if now - last_snapshot >= 60.0:
                self.service.persist_snapshot()
                last_snapshot = now

    # How long the radio may stay disconnected before the watchdog resets its
    # USB device, and the poll cadence. Class attributes so tests can shrink
    # them.
    WATCHDOG_GRACE = 90.0
    WATCHDOG_POLL = 10.0

    def _watchdog_loop(self) -> None:
        """Reset the radio's USB device if the link stays dead too long.

        The reconnect loop handles failures that RETURN; this handles the one
        that doesn't - a serial open blocked inside a kernel syscall against a
        wedged device, which freezes the connect coroutine's event loop so no
        asyncio timeout can ever fire. A USB reset re-enumerates the device,
        which forces the stuck syscall to return, unsticking everything.
        """
        last_ok = time.time()
        while not self._stop.wait(self.WATCHDOG_POLL):
            if self.service.state.connected:
                last_ok = time.time()
                continue
            if time.time() - last_ok < self.WATCHDOG_GRACE:
                continue
            ok, detail = try_usb_reset(getattr(self.link, "port", None))
            self.service.handle_event("status" if ok else "error",
                                      f"watchdog: {detail}")
            last_ok = time.time()  # back off a full grace period either way

    def _radio_loop(self) -> None:
        """Keep reopening a failed or disconnected companion link."""
        wedged_attempts = 0
        reset_tried = False
        while not self._stop.is_set():
            self.link.start()
            while self.service.state.connected and not self._stop.wait(0.5):
                pass
            if self._stop.is_set():
                return
            self.service.state.connected = False
            self.link.stop()
            if getattr(self.link, "usb_wedged", False):
                wedged_attempts += 1
            else:
                wedged_attempts = 0
                reset_tried = False
            if wedged_attempts >= 2 and not reset_tried:
                # Two consecutive EPIPE opens: the radio's USB stack is truly
                # wedged, not transiently busy. One reset per wedge episode -
                # if it does not cure it, only a replug will, and repeating a
                # failing reset would just spam the log.
                reset_tried = True
                ok, detail = try_usb_reset(getattr(self.link, "port", None))
                self.service.handle_event("status" if ok else "error", detail)
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
        self.service.persist_snapshot()
        with self._subs_lock:
            subscribers, self._subscribers = list(self._subscribers), []
        for events in subscribers:
            self._hang_up(events)
        self.service.remove_listener(self._log_event)
        self.service.remove_listener(self._broadcast)
        if self.bot_router is not None:
            self.service.remove_listener(self.bot_router.handle_event)
            self.bot_router.close()
        if self.path_bot is not None:
            self.service.remove_listener(self.path_bot.handle_event)
            self.path_bot.close()
        if self.test_bot is not None:
            self.service.remove_listener(self.test_bot.handle_event)
            self.test_bot.close()
        if self.map_uploader is not None:
            self.service.remove_listener(self.map_uploader.handle_event)
            self.map_uploader.close()
        if self.telemetry_bot is not None:
            self.service.remove_listener(self.telemetry_bot.handle_event)
            self.telemetry_bot.close()
        for sink in self.event_sinks:
            self.service.remove_listener(sink.handle_event)
            if self._sinks_started:
                close = getattr(sink, "close", None)
                if close is not None:
                    close()
        self._sinks_started = False
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
            integrations = []
            for sink in self.event_sinks:
                status = getattr(sink, "status", None)
                if status is not None:
                    integrations.append(status())
            return {
                "ok": True, "connected": self.service.state.connected,
                "protocol": self.service.state.protocol,
                "node_id": self.service.state.my_node_id,
                "device": self.service.state.device_path,
                "outbox_pending": pending,
                "integrations": integrations,
            }
        if command == "paths":
            # History for the TUI's paths explorer: a gateway-attached client
            # has no database of its own, only this socket.
            try:
                limit = max(1, min(5000, int(request.get("limit") or 2000)))
            except (TypeError, ValueError):
                limit = 2000
            with self.service.lock:
                rows = [dataclasses.asdict(o) for o in self.service.state.paths[-limit:]]
            return {"ok": True, "paths": rows}
        if command == "delivery":
            message_id = str(request.get("message_id") or "").strip()
            if not message_id:
                return {"ok": False, "error": "delivery requires a message_id"}
            snapshot = self.service.delivery_snapshot(message_id)
            if snapshot is None:
                return {"ok": False, "error": f"unknown message id {message_id}"}
            return {"ok": True, **snapshot}
        if command == "trace":
            target = str(request.get("to") or "").strip()
            if not target:
                return {"ok": False, "error": "trace requires a target node id"}
            try:
                hop_limit = max(1, min(7, int(request.get("hop_limit") or 5)))
                self.link.request_traceroute(target, hop_limit)
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True}
        if command == "channel":
            # Channel slots live on the radio, and the gateway owns the radio -
            # a gateway-attached TUI edits them through here.
            if self.service.state.protocol != "meshcore":
                return {"ok": False, "error": "channel editing requires a MeshCore gateway"}
            action = str(request.get("action") or "").casefold()
            try:
                index = int(request.get("index"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "channel requires a slot index"}
            name = str(request.get("name") or "")
            try:
                if action == "set":
                    secret_hex = str(request.get("secret") or "")
                    secret = bytes.fromhex(secret_hex) if secret_hex else None
                    if secret is not None and len(secret) != 16:
                        raise ValueError("secret must be 32 hex characters (16 bytes)")
                    self.link.set_channel(index, name, secret)
                elif action == "rename":
                    if not self.link.rename_channel(index, name):
                        return {"ok": False, "error": f"slot {index} was not renamed"}
                elif action == "delete":
                    self.link.delete_channel(index)
                elif action == "key":
                    secret = getattr(self.link, "channel_secrets", {}).get(index)
                    if secret is None:
                        return {"ok": False, "error": f"no key known for slot {index}"}
                    return {"ok": True, "key": bytes(secret).hex()}
                else:
                    raise ValueError("channel action must be set, rename, delete, or key")
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True}
        if command == "scope":
            if self.service.state.protocol != "meshcore":
                return {"ok": False, "error": "flood scopes require a MeshCore gateway"}
            action = str(request.get("action") or "get").casefold()
            try:
                if action == "get":
                    self.link.request_flood_scope()
                elif action in ("session", "default"):
                    self.link.set_flood_scope(
                        str(request.get("scope") or ""), save_default=(action == "default"))
                elif action == "unscoped":
                    self.link.set_flood_scope("*", force_unscoped=True)
                else:
                    raise ValueError("scope action must be get, session, default, or unscoped")
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True}
        if command == "admin":
            if self.service.state.protocol != "meshcore":
                return {"ok": False, "error": "remote admin requires a MeshCore gateway"}
            action = str(request.get("action") or "").casefold()
            target = str(request.get("to") or "").strip()
            if not target:
                return {"ok": False, "error": "admin request requires a target node id"}
            try:
                if action == "login":
                    password = request.get("password")
                    if not isinstance(password, str) or not password:
                        raise ValueError("login requires a password")
                    self.link.login(target, password)
                elif action == "logout":
                    self.link.logout(target)
                elif action == "command":
                    text = str(request.get("text") or "").strip()
                    if not text or len(text.encode("utf-8")) > 200:
                        raise ValueError("admin command must be 1-200 UTF-8 bytes")
                    self.link.remote_command(target, text)
                elif action == "status":
                    self.link.request_status(target)
                elif action == "telemetry":
                    self.link.request_telemetry(target)
                elif action == "neighbours":
                    self.link.request_neighbours(target)
                else:
                    raise ValueError(
                        "admin action must be login, logout, command, status, telemetry, or neighbours")
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True}
        if command != "send":
            return {"ok": False, "error":
                    "supported commands: status, delivery, paths, trace, scope, admin, send, subscribe"}
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text must not be empty"}
        message_id = request.get("message_id")
        if message_id is not None:
            message_id = str(message_id)
            if not message_id or len(message_id) > 128 or message_id.split() != [message_id]:
                return {"ok": False, "error": "message_id must be one token under 129 chars"}
            existing = self.service.delivery_snapshot(message_id)
            if existing is not None:
                # A client retrying its own send must never queue a duplicate;
                # answer with where the original attempt already got to.
                return {
                    "ok": existing["status"] not in
                          (DeliveryStatus.FAILED.value, DeliveryStatus.EXPIRED.value),
                    "message_id": message_id,
                    "status": existing["status"],
                    "protocol_id": existing["protocol_id"],
                    "detail": existing["detail"],
                }
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
                route_mode = str(request.get("route_mode") or "auto").casefold()
                if route_mode not in ("auto", "flood", "direct"):
                    raise ValueError("route_mode must be auto, flood, or direct")
                if route_mode != "auto" and protocol != "meshcore":
                    raise ValueError("route_mode overrides are MeshCore only")
                hash_size = request.get("path_hash_size")
                if hash_size is not None:
                    try:
                        hash_size = int(hash_size)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("path_hash_size must be 1, 2, 3, or 4") from exc
                    if hash_size not in (1, 2, 3, 4):
                        raise ValueError("path_hash_size must be 1, 2, 3, or 4")
                destination = PeerRef(protocol, target, public_key, route_mode, hash_size)
            elif kind == "channel":
                destination = self._channel_ref(request.get("channel"), protocol)
            else:
                raise ValueError("send kind must be dm or channel")
            return receipt_dict(self.service.send_message(
                text.strip(), destination, message_id=message_id))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}


class GatewayLink(RadioLink):
    """A RadioLink that attaches to a running gateway instead of a radio.

    The gateway stays the only process that opens the serial/BLE/TCP link and
    the only writer of the database. This link subscribes to the gateway's
    event stream and replays it through the same emit(kind, payload) callback
    the real links use, so the TUI renders live traffic without owning the
    radio - it can start, quit, crash and reconnect freely.
    """

    DEDUP_MEMORY = 4000  # replayed-event keys remembered across reconnects

    def __init__(self, emit, socket_path: Path | str | None = None,
                 reconnect_seconds: float = 3.0) -> None:
        super().__init__(emit)
        self.socket_path = Path(socket_path or default_socket_path())
        self.reconnect_seconds = max(0.1, reconnect_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        # Every subscribe replays a snapshot, so a reconnect would re-deliver
        # recent history; these remember what was already emitted.
        self._sent_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_chat: OrderedDict[Any, None] = OrderedDict()
        self._seen_packets: OrderedDict[Any, None] = OrderedDict()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gateway-link",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.connected = False

    # -------------------------------------------------------------- stream

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._stream_once()
            except (OSError, ValueError, RuntimeError) as exc:
                if self._stop.is_set():
                    return
                self.connected = False
                self.emit("lost", f"gateway at {self.socket_path} unreachable: {exc}")
            if self._stop.wait(self.reconnect_seconds):
                return

    def _stream_once(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # The gateway pings idle streams every 15s, so triple that of
            # silence means the far side is gone, not just quiet.
            client.settimeout(3 * STREAM_PING_SECONDS)
            client.connect(str(self.socket_path))
            self._sock = client
            try:
                client.sendall(b'{"command":"subscribe"}\n')
                reader = client.makefile("rb")
                header = json.loads(reader.readline(65537).decode("utf-8"))
                if not isinstance(header, dict) or not header.get("ok"):
                    raise RuntimeError((header or {}).get("error", "subscribe refused")
                                       if isinstance(header, dict) else "invalid header")
                self.connected = True
                self.emit("status",
                          f"attached to gateway on {self.socket_path} "
                          f"({header.get('protocol')}, radio "
                          f"{'connected' if header.get('connected') else 'not connected'})")
                for line in reader:
                    if self._stop.is_set():
                        return
                    self._dispatch(json.loads(line.decode("utf-8")))
            finally:
                self._sock = None
        if not self._stop.is_set():
            raise RuntimeError("gateway closed the stream")

    def _dispatch(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        kind, payload = event_from_wire(obj)
        if kind == "ping":
            return
        if kind == "chat":
            key = payload.message_id or (round(payload.ts, 3), payload.from_id,
                                         payload.to_id, payload.channel, payload.text)
            duplicate = key in self._seen_chat
            self._remember(self._seen_chat, key)
            if payload.message_id and (duplicate or payload.message_id in self._sent_ids):
                # Either our own send echoed back or a message we already
                # rendered, updated: merge it, never append a second bubble.
                self.emit("chat_update", payload)
            elif not duplicate:
                self.emit("chat", payload)
            return
        if kind == "packet":
            key = (round(payload.ts, 3), payload.from_id, payload.to_id,
                   payload.portnum, payload.packet_id)
            if key in self._seen_packets:
                return
            self._remember(self._seen_packets, key)
        elif kind == "connected":
            self.connected = True
        elif kind == "lost":
            payload = f"gateway radio: {payload}"
        self.emit(kind, payload)

    def _remember(self, seen: OrderedDict, key: Any) -> None:
        seen[key] = None
        seen.move_to_end(key)
        while len(seen) > self.DEDUP_MEMORY:
            seen.popitem(last=False)

    # ---------------------------------------------------------------- send

    def send(self, text: str, destination: DestinationRef,
             message_id: str) -> SendReceipt:
        request: dict[str, Any] = {"command": "send", "text": text,
                                   "message_id": message_id}
        if isinstance(destination, PeerRef):
            request["kind"] = "dm"
            request["to"] = destination.node_id
            if destination.public_key:
                request["public_key"] = destination.public_key
            if destination.route_mode != "auto":
                request["route_mode"] = destination.route_mode
            if destination.path_hash_size is not None:
                request["path_hash_size"] = destination.path_hash_size
        else:
            request["kind"] = "channel"
            request["channel"] = destination.index
        self._remember(self._sent_ids, message_id)
        try:
            result = request_gateway(request, self.socket_path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            # Not terminal: the service keeps the message queued and this link
            # retries it once the gateway is back.
            return SendReceipt(message_id, destination, DeliveryStatus.QUEUED,
                               detail=f"gateway unreachable: {exc}")
        status = result.get("status")
        if status is None:
            return SendReceipt(message_id, destination, DeliveryStatus.FAILED,
                               detail=str(result.get("error") or "gateway refused the message"))
        try:
            parsed = DeliveryStatus(status)
        except ValueError:
            parsed = DeliveryStatus.FAILED
        return SendReceipt(message_id, destination, parsed,
                           protocol_id=result.get("protocol_id"),
                           detail=str(result.get("detail") or ""))

    def request_traceroute(self, dest: str, hop_limit: int = 5) -> None:
        self._control({"command": "trace", "to": dest, "hop_limit": hop_limit},
                      "trace request")

    def request_flood_scope(self) -> None:
        self._control({"command": "scope", "action": "get"}, "scope request")

    def set_flood_scope(self, scope: str, *, save_default: bool = False,
                        force_unscoped: bool = False) -> None:
        action = "unscoped" if force_unscoped else ("default" if save_default else "session")
        self._control({"command": "scope", "action": action, "scope": scope},
                      "scope change")

    def login(self, node_id: str, password: str) -> None:
        self._control({"command": "admin", "action": "login", "to": node_id,
                       "password": password}, "login request")

    def logout(self, node_id: str) -> None:
        self._control({"command": "admin", "action": "logout", "to": node_id},
                      "logout request")

    def remote_command(self, node_id: str, command: str) -> None:
        self._control({"command": "admin", "action": "command", "to": node_id,
                       "text": command}, "admin command")

    def request_status(self, node_id: str) -> None:
        self._control({"command": "admin", "action": "status", "to": node_id},
                      "status request")

    def request_telemetry(self, node_id: str) -> None:
        self._control({"command": "admin", "action": "telemetry", "to": node_id},
                      "telemetry request")

    def request_neighbours(self, node_id: str) -> None:
        self._control({"command": "admin", "action": "neighbours", "to": node_id},
                      "neighbours request")

    # --- channel slots: edited on the gateway's radio over the socket ---

    def set_channel(self, index: int, name: str, secret: bytes | None = None) -> None:
        self._control({"command": "channel", "action": "set", "index": index,
                       "name": name, "secret": secret.hex() if secret else ""},
                      f"channel {index}")

    def rename_channel(self, index: int, name: str) -> bool:
        return self._control({"command": "channel", "action": "rename",
                              "index": index, "name": name}, f"rename channel {index}")

    def delete_channel(self, index: int) -> None:
        self._control({"command": "channel", "action": "delete", "index": index},
                      f"clear channel {index}")

    def channel_key(self, index: int) -> str | None:
        """The slot's 16-byte key as hex, for sharing with another node."""
        try:
            result = request_gateway({"command": "channel", "action": "key",
                                      "index": index}, self.socket_path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            self.emit("error", f"gateway channel key failed: {exc}")
            return None
        if not result.get("ok"):
            self.emit("error", str(result.get("error") or "gateway refused channel key"))
            return None
        return str(result.get("key") or "") or None

    def _control(self, request: dict[str, Any], label: str) -> bool:
        try:
            result = request_gateway(request, self.socket_path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            self.emit("error", f"gateway {label} failed: {exc}")
            return False
        if not result.get("ok"):
            self.emit("error", str(result.get("error") or f"gateway refused {label}"))
            return False
        return True


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
                  pathbot_channel: str | int | None = None,
                  testbot_channel: str | int | list[str | int] | None = None,
                  testbot_location: str = "",
                  telemetry_bot_nodes: list[str] | tuple[str, ...] = (),
                  telemetry_bot_trigger: str = "!mesh",
                  telemetry_bot_position: bool = False,
                  mqtt_config: Any | None = None,
                  mqtt_client: Any | None = None,
                  plugin_dir: Path | str | None = None,
                  notify_nodes: list[str] | tuple[str, ...] = (),
                  notify_trace_failures: bool = False,
                  notify_desktop: bool = False,
                  ntfy_topic: str | None = None,
                  ntfy_url: str = "https://ntfy.sh",
                  ntfy_token: str | None = None,
                  notify_active_seconds: float = 900.0,
                  map_upload: bool = False,
                  weatherbot_channel: str | int | None = None,
                  sensorbot_channel: str | int | None = None,
                  sensorbot_minutes: float = 60.0,
                  weatherbot_times: str = "07:00,12:00,18:00",
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
    path_bot = PathBot(service, channel=pathbot_channel) if pathbot_channel is not None else None
    test_bot = (TestBot(service, channel=testbot_channel, location=testbot_location)
                if testbot_channel is not None else None)
    telemetry_bot = (TelemetryBot(
        service, allowed_nodes=telemetry_bot_nodes, trigger=telemetry_bot_trigger,
        include_position=telemetry_bot_position)
        if telemetry_bot_nodes else None)
    event_sinks: list[Any] = []
    mqtt_sink = None
    if mqtt_config is not None:
        from .ha_mqtt import HomeAssistantMQTT
        mqtt_sink = HomeAssistantMQTT(service, mqtt_config, client=mqtt_client)
        event_sinks.append(mqtt_sink)
    if plugin_dir is not None:
        from .plugins import PluginManager
        event_sinks.append(PluginManager(service, plugin_dir or None))
    if notify_nodes or notify_trace_failures:
        from .notifications import (DesktopNotifier, NotificationBus,
                                    NtfyNotifier)
        notifiers = []
        if notify_desktop:
            notifiers.append(DesktopNotifier())
        if ntfy_topic:
            notifiers.append(NtfyNotifier(ntfy_topic, ntfy_url, ntfy_token))
        if mqtt_sink is not None:
            notifiers.append(mqtt_sink)
        if not notifiers:
            raise ValueError("notification rules need --notify-desktop, --ntfy-topic, "
                             "or --mqtt-host")
        event_sinks.append(NotificationBus(
            service, named_nodes=notify_nodes,
            trace_failures=notify_trace_failures, notifiers=notifiers,
            active_seconds=notify_active_seconds))
    map_uploader = None
    if map_upload:
        from .mapupload import MapUploader
        # The uploader signs with the radio's identity, which the link only
        # fetches when asked to.
        link.export_identity = True
        map_uploader = MapUploader(service, link)
    weather_bot = None
    if weatherbot_channel is not None:
        from .weatherbot import WeatherBot
        weather_bot = WeatherBot(service, channel=weatherbot_channel,
                                 times=weatherbot_times, location=testbot_location)
    sensor_bot = None
    if sensorbot_channel is not None:
        from .sensorbot import SensorBot
        sensor_bot = SensorBot(service, channel=sensorbot_channel,
                               minutes=sensorbot_minutes)
    return Gateway(
        service, link, socket_path,
        bot_router=router,
        path_bot=path_bot,
        test_bot=test_bot,
        map_uploader=map_uploader,
        weather_bot=weather_bot,
        sensor_bot=sensor_bot,
        telemetry_bot=telemetry_bot,
        event_sinks=event_sinks,
    )
