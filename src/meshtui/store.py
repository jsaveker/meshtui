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


def _json_list(raw: Any) -> list:
    try:
        value = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: Any) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}

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

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS relays (
    byte        INTEGER PRIMARY KEY,
    packets     INTEGER DEFAULT 0,
    origins     TEXT,
    first_seen  REAL,
    last_seen   REAL,
    snr_sum     REAL DEFAULT 0,
    snr_n       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS relay_edges (
    origin      TEXT,
    relay_byte  INTEGER,
    packets     INTEGER DEFAULT 0,
    PRIMARY KEY (origin, relay_byte)
);

CREATE TABLE IF NOT EXISTS foreign_channels (
    hash        INTEGER PRIMARY KEY,
    packets     INTEGER DEFAULT 0,
    senders     TEXT,
    ports       TEXT,
    first_seen  REAL,
    last_seen   REAL,
    snr_min     REAL,
    snr_max     REAL,
    hops_min    INTEGER,
    hops_max    INTEGER,
    key_label   TEXT,
    sample      TEXT
);

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


# Derived per-node state, added to the nodes table by migration so an existing
# database keeps working. Without these, pruning the packets table would lose
# sparklines and sensor readings permanently.
NODE_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("snr_history", "TEXT"),
    ("env", "TEXT"),
    ("env_ts", "REAL"),
    ("local_stats", "TEXT"),
    ("local_stats_ts", "REAL"),
    ("speed", "REAL"),
    ("heading", "REAL"),
    ("sats", "INTEGER"),
    ("location_source", "TEXT"),
    ("precision_bits", "INTEGER"),
    ("track", "TEXT"),
]

# Key in the meta table holding the newest packet timestamp already folded into
# the persisted aggregates, so a replay can resume from there without
# double-counting.
STATE_TS = "state_ts"


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
            self._migrate(conn)
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

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add any derived-state columns an older database is missing."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
        for name, sqltype in NODE_EXTRA_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {sqltype}")
        conn.commit()

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

    def save_node_derived(self, n: Node) -> None:
        """Persist the state that is folded in from packets rather than sent
        as node records. Only non-empty values are written, so a session that
        has not heard from a node cannot erase what we already know."""
        values: dict[str, Any] = {}
        if n.snr_history:
            values["snr_history"] = json.dumps(list(n.snr_history))
        if n.env:
            values["env"] = json.dumps(n.env)
            values["env_ts"] = n.env_ts
        if n.local_stats:
            values["local_stats"] = json.dumps(n.local_stats)
            values["local_stats_ts"] = n.local_stats_ts
        if n.track:
            values["track"] = json.dumps([list(p) for p in n.track])
        for attr, column in (("speed_mps", "speed"), ("heading_deg", "heading"),
                             ("sats", "sats"), ("location_source", "location_source"),
                             ("precision_bits", "precision_bits")):
            value = getattr(n, attr)
            if value not in (None, ""):
                values[column] = value
        if not values:
            return
        assignments = ", ".join(f"{k}=?" for k in values)
        self._put(f"UPDATE nodes SET {assignments} WHERE node_id=?",
                  (*values.values(), n.node_id))

    def save_relay(self, byte: int, packets: int, origins: Iterable[str],
                   first_seen: float, last_seen: float, snr_sum: float, snr_n: int) -> None:
        self._put(
            "INSERT INTO relays (byte, packets, origins, first_seen, last_seen, snr_sum,"
            " snr_n) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(byte) DO UPDATE SET packets=excluded.packets,"
            " origins=excluded.origins,"
            " first_seen=MIN(relays.first_seen, excluded.first_seen),"
            " last_seen=MAX(relays.last_seen, excluded.last_seen),"
            " snr_sum=excluded.snr_sum, snr_n=excluded.snr_n",
            (byte, packets, json.dumps(sorted(origins)), first_seen, last_seen,
             snr_sum, snr_n),
        )

    def save_relay_edge(self, origin: str, byte: int, packets: int) -> None:
        self._put(
            "INSERT INTO relay_edges (origin, relay_byte, packets) VALUES (?,?,?)"
            " ON CONFLICT(origin, relay_byte) DO UPDATE SET packets=excluded.packets",
            (origin, byte, packets),
        )

    def save_foreign_channel(self, ch: Any) -> None:
        self._put(
            "INSERT INTO foreign_channels (hash, packets, senders, ports, first_seen,"
            " last_seen, snr_min, snr_max, hops_min, hops_max, key_label, sample)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(hash) DO UPDATE SET packets=excluded.packets,"
            " senders=excluded.senders, ports=excluded.ports,"
            " first_seen=MIN(foreign_channels.first_seen, excluded.first_seen),"
            " last_seen=MAX(foreign_channels.last_seen, excluded.last_seen),"
            " snr_min=excluded.snr_min, snr_max=excluded.snr_max,"
            " hops_min=excluded.hops_min, hops_max=excluded.hops_max,"
            " key_label=COALESCE(excluded.key_label, foreign_channels.key_label),"
            " sample=COALESCE(excluded.sample, foreign_channels.sample)",
            (ch.hash, ch.packets, json.dumps(sorted(ch.senders)),
             json.dumps(dict(ch.ports)), ch.first_seen, ch.last_seen,
             ch.snr_min, ch.snr_max, ch.hops_min, ch.hops_max, ch.key_label, ch.sample),
        )

    def set_meta(self, key: str, value: Any) -> None:
        self._put(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
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

    def recent_packets(self, limit: int = 3000) -> list[dict[str, Any]]:
        """The most recent raw packets, oldest first, for rebuilding state.

        Derived state - SNR history, sensor readings, relay counts - is not
        stored per node; it is rebuilt by replaying these through the same
        code path live packets take.
        """
        rows = self._read(
            "SELECT raw FROM (SELECT rowid_, raw FROM packets ORDER BY rowid_ DESC"
            " LIMIT ?) ORDER BY rowid_ ASC",
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(json.loads(row["raw"]))
            except Exception:  # noqa: BLE001 - skip anything unparseable
                continue
        return out

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
            keys = r.keys()
            record["_derived"] = {
                "snr_history": _json_list(r["snr_history"]) if "snr_history" in keys else [],
                "env": _json_dict(r["env"]) if "env" in keys else {},
                "env_ts": r["env_ts"] if "env_ts" in keys else None,
                "local_stats": _json_dict(r["local_stats"]) if "local_stats" in keys else {},
                "local_stats_ts": r["local_stats_ts"] if "local_stats_ts" in keys else None,
                "track": _json_list(r["track"]) if "track" in keys else [],
                "speed_mps": r["speed"] if "speed" in keys else None,
                "heading_deg": r["heading"] if "heading" in keys else None,
                "sats": r["sats"] if "sats" in keys else None,
                "location_source": (r["location_source"] or "") if "location_source" in keys else "",
                "precision_bits": r["precision_bits"] if "precision_bits" in keys else None,
            }
            out.append(record)
        return out

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._read("SELECT value FROM meta WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except Exception:  # noqa: BLE001
            return default

    def load_relays(self) -> list[dict[str, Any]]:
        return [
            {"byte": r["byte"], "packets": r["packets"] or 0,
             "origins": _json_list(r["origins"]), "first_seen": r["first_seen"] or 0.0,
             "last_seen": r["last_seen"] or 0.0, "snr_sum": r["snr_sum"] or 0.0,
             "snr_n": r["snr_n"] or 0}
            for r in self._read("SELECT * FROM relays")
        ]

    def load_relay_edges(self) -> list[tuple[str, int, int]]:
        return [(r["origin"], r["relay_byte"], r["packets"] or 0)
                for r in self._read("SELECT * FROM relay_edges")]

    def load_foreign_channels(self) -> list[dict[str, Any]]:
        out = []
        for r in self._read("SELECT * FROM foreign_channels"):
            record = dict(r)
            record["senders"] = set(_json_list(r["senders"]))
            record["ports"] = _json_dict(r["ports"])
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
