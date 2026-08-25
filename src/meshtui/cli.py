"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .radio import find_serial_ports
from .store import Store, default_db_path


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
    args = parser.parse_args(argv)

    if args.list_ports:
        ports = find_serial_ports()
        if not ports:
            print("no serial ports found")
            return 1
        for port in ports:
            print(port)
        return 0

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
