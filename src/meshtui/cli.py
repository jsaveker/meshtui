"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .radio import find_serial_ports
from .store import Store, default_db_path


def run_audit(db_path: str | None) -> int:
    """Offline channel audit over a captured database.

    Only ever tries keys that Meshtastic publishes in its own source. A channel
    reported as unreadable is using a real random PSK, and nothing here changes
    that.
    """
    import base64
    import json
    import sqlite3
    from collections import Counter

    from . import crypto

    store = Store(db_path)
    if not store.path.exists():
        print(f"no database at {store.path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM packets WHERE portnum='ENCRYPTED'"))
    total = conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    conn.close()

    keys = list(crypto.published_keys())
    print(f"database   : {store.path}")
    print(f"packets    : {total} total, {len(rows)} your node could not decrypt")
    print(f"trying     : {len(keys)} published keys "
          f"(the default key and every single-byte shorthand)")

    channels: dict[int, dict] = {}
    for row in rows:
        try:
            raw = json.loads(row["raw"])
        except Exception:  # noqa: BLE001
            continue
        blob = raw.get("encrypted")
        if isinstance(blob, str):
            try:
                blob = base64.b64decode(blob)
            except Exception:  # noqa: BLE001
                continue
        chan = channels.setdefault(
            raw.get("channel"),
            {"packets": 0, "senders": Counter(), "opened": None, "ports": Counter()},
        )
        chan["packets"] += 1
        chan["senders"][row["from_id"]] += 1
        if chan["opened"] is None:
            got = crypto.try_keys(raw.get("id"), raw.get("from"), bytes(blob or b""), keys)
            if got is not None:
                chan["opened"] = got.key_label
                chan["ports"][got.portnum] += 1

    if not channels:
        print("\nno undecryptable traffic captured - nothing to audit")
        return 0

    print(f"\n{'hash':>5}  {'packets':>7}  {'senders':>7}  verdict")
    print("-" * 62)
    exposed = 0
    for hash_value, info in sorted(channels.items(), key=lambda kv: -kv[1]["packets"]):
        if info["opened"]:
            verdict = f"PUBLIC KEY ({info['opened']}) - readable by anyone"
            exposed += 1
        else:
            verdict = "no published key applies - real PSK"
        print(f"{hash_value!s:>5}  {info['packets']:>7}  {len(info['senders']):>7}  {verdict}")

    print()
    if exposed:
        print(f"{exposed} channel(s) are using a key published in Meshtastic's source.")
        print("Anyone running this command can read them. Tell whoever runs them.")
    else:
        print("Every channel here uses a real random PSK. None are readable.")
    print("\nNote: sender, channel hash, hop count and signal strength travel in the")
    print("clear on every packet regardless, so activity is visible without any key.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meshtui",
        description="Terminal dashboard for a Meshtastic mesh.",
    )
    parser.add_argument("-p", "--port", help="serial device (default: autodetect)")
    parser.add_argument("--demo", action="store_true", help="run against a synthetic mesh")
    parser.add_argument("--list-ports", action="store_true", help="show candidate serial ports")
    parser.add_argument("--debug", action="store_true", help="write debug log to meshtui.log")
    parser.add_argument("--db", help=f"database path (default: {default_db_path()})")
    parser.add_argument("--no-store", action="store_true", help="do not persist to disk")
    parser.add_argument("--stats", action="store_true", help="print database stats and exit")
    parser.add_argument(
        "--export", metavar="TABLE:FILE",
        help="export packets|messages|nodes to CSV and exit (e.g. packets:out.csv)",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="audit captured channels against Meshtastic's published keys and exit",
    )
    args = parser.parse_args(argv)

    if args.list_ports:
        ports = find_serial_ports()
        if not ports:
            print("no serial ports found")
            return 1
        for port in ports:
            print(port)
        return 0

    if args.audit:
        return run_audit(args.db)

    if args.stats or args.export:
        store = Store(args.db)
        if args.export:
            table, _, out = args.export.partition(":")
            if not out:
                print("--export needs TABLE:FILE, e.g. packets:out.csv", file=sys.stderr)
                return 2
            try:
                count = store.export_csv(table, out)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            print(f"wrote {count} rows to {out}")
            return 0
        info = store.stats()
        if not info:
            print(f"no database at {store.path}")
            return 1
        since = info.get("since")
        print(f"database : {store.path}")
        print(f"packets  : {info.get('packets', 0)}")
        print(f"messages : {info.get('messages', 0)}")
        print(f"nodes    : {info.get('nodes', 0)}")
        if since:
            age = (time.time() - since) / 86400
            print(f"since    : {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} "
                  f"({age:.1f} days)")
        return 0

    if args.debug:
        logging.basicConfig(
            filename="meshtui.log",
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    else:
        # The meshtastic library is chatty on stderr, which corrupts the TUI.
        logging.basicConfig(level=logging.CRITICAL, handlers=[logging.NullHandler()])

    store: Store | None = None
    if not args.no_store:
        store = Store(args.db)
        if not store.open():
            print(f"warning: {store.error}", file=sys.stderr)
            store = None

    from .app import MeshTUI

    try:
        MeshTUI(port=args.port, demo=args.demo, store=store).run()
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
