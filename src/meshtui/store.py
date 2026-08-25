"""SQLite persistence.

Every packet, message, and node fact is durable, so history survives restarts.
Writes are funnelled through a single background thread: sqlite3 connections
are not shareable across threads, and disk I/O must never stall the UI.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .model import ChatMessage, Node, Packet

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    rowid_      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    from_id     TEXT,
    to_id       TEXT,
    portnum     TEXT,
    channel     INTEGER,
    snr         REAL,
    rssi        INTEGER,
    hops        INTEGER,
    packet_id   INTEGER,
    summary     TEXT,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS packets_ts   ON packets(ts);
CREATE INDEX IF NOT EXISTS packets_from ON packets(from_id);
CREATE INDEX IF NOT EXISTS packets_port ON packets(portnum);

CREATE TABLE IF NOT EXISTS messages (
    rowid_      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    from_id     TEXT,
    to_id       TEXT,
    channel     INTEGER,
    text        TEXT,
    outgoing    INTEGER DEFAULT 0,
    packet_id   INTEGER,
    acked       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    num         INTEGER,
    long_name   TEXT,
    short_name  TEXT,
    hw_model    TEXT,
    role        TEXT,
    first_seen  REAL,
    last_heard  REAL,
    lat         REAL,
    lon         REAL,
    alt         INTEGER,
    battery     INTEGER,
    voltage     REAL,
    snr         REAL,
    hops        INTEGER,
    packets     INTEGER DEFAULT 0
);
"""


def default_db_path() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(root) / "meshtui" / "mesh.db"


class Store:
    """Append-only-ish log of mesh activity, written on a worker thread."""

    def __init__(self, path: Path | str | None = None, flush_interval: float = 2.0) -> None:
        self.path = Path(path) if path else default_db_path()
        self.flush_interval = flush_interval
        self._queue: queue.Queue[tuple[str, tuple] | None] = queue.Queue(maxsize=10000)
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self.enabled = False
        self.error: str | None = None

    # ------------------------------------------------------------ lifecycle

    def open(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.executescript(SCHEMA)
            # WAL keeps readers (e.g. an external sqlite3 shell) from blocking us.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not open {self.path}: {exc}"
            log.warning(self.error, exc_info=True)
            return False
        self._conn = conn
        self.enabled = True
        self._thread = threading.Thread(target=self._run, name="store", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        if not self.enabled:
            return
        self.enabled = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # -------------------------------------------------------------- writing

    def _put(self, sql: str, params: tuple) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((sql, params))
        except queue.Full:
            # Dropping history beats stalling the radio thread.
            log.warning("store queue full, dropping row")

    def _run(self) -> None:
        assert self._conn is not None
        pending = 0
        last_flush = time.time()
        while True:
            try:
                item = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                item = ...  # sentinel meaning "nothing to do, maybe flush"
            if item is None:
                break
            if item is not ...:
                sql, params = item
                try:
                    self._conn.execute(sql, params)
                    pending += 1
                except Exception:  # noqa: BLE001
                    log.warning("store write failed", exc_info=True)
            now = time.time()
            if pending and (pending >= 200 or now - last_flush >= self.flush_interval):
                try:
                    self._conn.commit()
                except Exception:  # noqa: BLE001
                    log.warning("store commit failed", exc_info=True)
                pending = 0
                last_flush = now
        try:
            self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def add_packet(self, p: Packet) -> None:
        try:
            raw = json.dumps(p.raw, default=repr)
        except Exception:  # noqa: BLE001
            raw = "{}"
        self._put(
            "INSERT INTO packets (ts, from_id, to_id, portnum, channel, snr, rssi, hops,"
            " packet_id, summary, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p.ts, p.from_id, p.to_id, p.portnum, p.channel, p.snr, p.rssi, p.hops,
             p.packet_id, p.summary, raw),
        )

    def add_message(self, m: ChatMessage) -> None:
        self._put(
            "INSERT INTO messages (ts, from_id, to_id, channel, text, outgoing, packet_id,"
            " acked) VALUES (?,?,?,?,?,?,?,?)",
            (m.ts, m.from_id, m.to_id, m.channel, m.text, int(m.outgoing), m.packet_id,
             int(m.acked)),
        )

    def ack_message(self, packet_id: int) -> None:
        self._put("UPDATE messages SET acked=1 WHERE packet_id=?", (packet_id,))

    def save_node(self, n: Node) -> None:
        self._put(
            "INSERT INTO nodes (node_id, num, long_name, short_name, hw_model, role,"
            " first_seen, last_heard, lat, lon, alt, battery, voltage, snr, hops, packets)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(node_id) DO UPDATE SET"
            "  num=excluded.num,"
            "  long_name=CASE WHEN excluded.long_name!='' THEN excluded.long_name"
            "            ELSE nodes.long_name END,"
            "  short_name=CASE WHEN excluded.short_name!='' THEN excluded.short_name"
            "             ELSE nodes.short_name END,"
            "  hw_model=CASE WHEN excluded.hw_model!='' THEN excluded.hw_model"
            "           ELSE nodes.hw_model END,"
            "  role=excluded.role,"
            "  first_seen=MIN(nodes.first_seen, excluded.first_seen),"
            "  last_heard=MAX(COALESCE(nodes.last_heard,0), COALESCE(excluded.last_heard,0)),"
            "  lat=COALESCE(excluded.lat, nodes.lat),"
            "  lon=COALESCE(excluded.lon, nodes.lon),"
            "  alt=COALESCE(excluded.alt, nodes.alt),"
            "  battery=COALESCE(excluded.battery, nodes.battery),"
            "  voltage=COALESCE(excluded.voltage, nodes.voltage),"
            "  snr=COALESCE(excluded.snr, nodes.snr),"
            "  hops=COALESCE(excluded.hops, nodes.hops),"
            "  packets=MAX(nodes.packets, excluded.packets)",
            (n.node_id, n.num, n.long_name, n.short_name, n.hw_model, n.role,
             n.first_seen, n.last_heard, n.lat, n.lon, n.alt, n.battery, n.voltage,
             n.snr, n.hops, n.packets),
        )

    # -------------------------------------------------------------- reading

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Reads use their own short-lived connection, off the writer thread."""
        try:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                return list(conn.execute(sql, params))
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.warning("store read failed", exc_info=True)
            return []

    def recent_messages(self, limit: int = 400) -> list[ChatMessage]:
        rows = self._read(
            "SELECT * FROM (SELECT * FROM messages ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC",
            (limit,),
        )
        return [
            ChatMessage(
                ts=r["ts"], from_id=r["from_id"] or "", from_name="",
                to_id=r["to_id"] or "", text=r["text"] or "", channel=r["channel"] or 0,
                outgoing=bool(r["outgoing"]), packet_id=r["packet_id"],
                acked=bool(r["acked"]),
            )
            for r in rows
        ]

    def known_nodes(self) -> list[dict[str, Any]]:
        """Node records shaped like meshtastic NodeDB entries, for upsert_node."""
        out: list[dict[str, Any]] = []
        for r in self._read("SELECT * FROM nodes"):
            record: dict[str, Any] = {
                "num": r["num"],
                "user": {
                    "id": r["node_id"],
                    "longName": r["long_name"] or "",
                    "shortName": r["short_name"] or "",
                    "hwModel": r["hw_model"] or "",
                },
                "snr": r["snr"],
                "hopsAway": r["hops"],
                "lastHeard": r["last_heard"],
            }
            if r["lat"] is not None and r["lon"] is not None:
                record["position"] = {"latitude": r["lat"], "longitude": r["lon"],
                                      "altitude": r["alt"]}
            if r["battery"] is not None:
                record["deviceMetrics"] = {"batteryLevel": r["battery"],
                                           "voltage": r["voltage"]}
            record["_first_seen"] = r["first_seen"]
            record["_packets"] = r["packets"] or 0
            out.append(record)
        return out

    def stats(self) -> dict[str, Any]:
        rows = self._read(
            "SELECT (SELECT COUNT(*) FROM packets) AS packets,"
            " (SELECT COUNT(*) FROM messages) AS messages,"
            " (SELECT COUNT(*) FROM nodes) AS nodes,"
            " (SELECT MIN(ts) FROM packets) AS since"
        )
        return dict(rows[0]) if rows else {}

    def export_csv(self, table: str, out: str) -> int:
        import csv

        if table not in ("packets", "messages", "nodes"):
            raise ValueError(f"unknown table {table!r}")
        rows = self._read(f"SELECT * FROM {table} ORDER BY rowid")
        if not rows:
            return 0
        with open(out, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(list(row))
        return len(rows)
