"""Named-node and trace-failure notifications for gateway operators."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .model import Node, Packet
from .service import MeshService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class Notifier(Protocol):
    def notify(self, notification: Notification) -> None: ...


class DesktopNotifier:
    """Best-effort local desktop notifications without a shell."""

    def notify(self, notification: Notification) -> None:
        try:
            if sys.platform == "darwin":
                script = 'display notification "' + self._apple(notification.message) + \
                         '" with title "' + self._apple(notification.title) + '"'
                subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform.startswith("linux"):
                subprocess.run(["notify-send", notification.title, notification.message],
                               check=False, timeout=5, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            else:
                log.info("notification: %s - %s", notification.title,
                         notification.message)
        except (OSError, subprocess.SubprocessError):
            log.debug("desktop notification failed", exc_info=True)

    @staticmethod
    def _apple(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class NtfyNotifier:
    def __init__(self, topic: str, base_url: str = "https://ntfy.sh",
                 token: str | None = None) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("ntfy URL must be http(s)")
        if not topic.strip() or any(char in topic for char in ("/", "?", "#")):
            raise ValueError("ntfy topic must be one non-empty path segment")
        self.url = base_url.rstrip("/") + "/" + urllib.parse.quote(topic.strip(), safe="")
        self.token = token

    def notify(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title,
            "Tags": "satellite,warning" if notification.kind == "trace_failed" else
                    "satellite,white_check_mark",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url, data=notification.message.encode("utf-8"), headers=headers,
            method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            response.read(256)


class NotificationBus:
    """Convert state transitions into deduplicated notifications off-thread."""

    def __init__(self, service: MeshService, *, named_nodes: list[str] | tuple[str, ...] = (),
                 trace_failures: bool = False, notifiers: list[Notifier] | None = None,
                 active_seconds: float = 900.0,
                 clock: Callable[[], float] = time.time) -> None:
        if active_seconds <= 0:
            raise ValueError("notification active window must be greater than zero")
        self.service = service
        self.named_nodes = tuple(value.strip().casefold() for value in named_nodes
                                 if value.strip())
        self.trace_failures = trace_failures
        self.notifiers = list(notifiers or ())
        self.active_seconds = active_seconds
        self.clock = clock
        self._last_seen: dict[str, float] = {}
        self._queue: queue.Queue[Notification | None] = queue.Queue(maxsize=200)
        self._worker: threading.Thread | None = None
        self.sent = 0
        self.errors: list[str] = []

    def status(self) -> dict[str, Any]:
        return {
            "kind": "notifications",
            "named_nodes": list(self.named_nodes),
            "trace_failures": self.trace_failures,
            "destinations": [type(notifier).__name__ for notifier in self.notifiers],
            "sent": self.sent,
            "errors": list(self.errors[-10:]),
        }

    def start(self) -> None:
        if self._worker is not None:
            return
        with self.service.lock:
            for node in self.service.state.nodes.values():
                if node.last_heard is not None:
                    self._last_seen[node.node_id] = node.last_heard
        self._worker = threading.Thread(target=self._run, name="meshtui-notifications",
                                        daemon=True)
        self._worker.start()

    def close(self) -> None:
        if self._worker is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=3.0)
        self._worker = None

    def handle_event(self, kind: str, payload: Any) -> None:
        if self.named_nodes:
            node = None
            if kind in ("node", "mc_contact") and isinstance(payload, Node):
                node = payload
            elif kind == "packet" and isinstance(payload, Packet):
                node = self.service.state.nodes.get(payload.from_id)
            if node is not None:
                self._node_seen(node)
        if self.trace_failures and kind in ("error", "lost"):
            message = str(payload)
            folded = message.casefold()
            if any(word in folded for word in ("trace", "traceroute", "path discovery")):
                self._enqueue(Notification(
                    title="Mesh trace failed", message=message,
                    kind="trace_failed", data={"detail": message}))

    def _matches(self, node: Node) -> bool:
        values = (node.node_id.casefold(), node.name.casefold(),
                  node.long_name.casefold(), node.short_name.casefold())
        return any(fnmatch.fnmatchcase(value, pattern)
                   for pattern in self.named_nodes for value in values)

    def _node_seen(self, node: Node) -> None:
        if not self._matches(node):
            return
        now = self.clock()
        previous = self._last_seen.get(node.node_id)
        self._last_seen[node.node_id] = node.last_heard or now
        if previous is not None and now - previous <= self.active_seconds:
            return
        self._enqueue(Notification(
            title="Mesh node appeared",
            message=f"{node.name} ({node.node_id}) is active on the mesh",
            kind="node_appeared",
            data={"node_id": node.node_id, "name": node.name,
                  "snr": node.snr, "hops": node.hops}))

    def _enqueue(self, notification: Notification) -> None:
        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            self.errors.append("notification queue full")

    def _run(self) -> None:
        while True:
            notification = self._queue.get()
            if notification is None:
                return
            for notifier in self.notifiers:
                try:
                    notifier.notify(notification)
                    self.sent += 1
                except Exception as exc:  # noqa: BLE001 - delivery isolation
                    detail = f"{type(notifier).__name__}: {exc}"
                    self.errors.append(detail)
                    log.warning("notification delivery failed: %s", detail)
