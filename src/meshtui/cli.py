"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .radio import find_serial_ports, find_wifi_nodes
from .store import Store, default_db_path


def _gateway_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshtui gateway",
        description="Run one unattended owner for the radio and durable outbound queue.",
    )
    parser.add_argument("-p", "--port", help="serial device (default: autodetect)")
    parser.add_argument("-H", "--host", help="radio TCP host[:port]")
    parser.add_argument("--protocol", choices=("auto", "meshtastic", "meshcore"),
                        default="auto")
    parser.add_argument("--demo", action="store_true", help="use the synthetic mesh")
    parser.add_argument("--db", help=f"database path (default: {default_db_path()})")
    parser.add_argument("--socket", help="local Unix socket path")
    parser.add_argument("--bot-channel", metavar="NAME_OR_SLOT",
                        help="enable @ai routing on this channel, e.g. '#bots' or 5")
    parser.add_argument("--pathbot", metavar="NAME_OR_SLOT",
                        help="answer !path on this channel with the route the "
                             "request traveled, e.g. '#bot' or 2")
    parser.add_argument("--testbot", metavar="NAME_OR_SLOT",
                        help="acknowledge test messages on this channel with the "
                             "hop count they arrived over, e.g. '#testing'")
    parser.add_argument("--ai-model", default="gpt-5-mini")
    parser.add_argument("--ai-endpoint", help="Responses-compatible API endpoint")
    parser.add_argument("--debug", action="store_true")
    return parser


def run_gateway(argv: list[str]) -> int:
    import signal
    from .gateway import build_gateway

    args = _gateway_parser().parse_args(argv)

    # systemd stops a service with SIGTERM; turn it into the same clean exit as
    # Ctrl-C so the radio and socket are released and the next start is clean.
    def _graceful(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    store = Store(args.db)
    if not store.open():
        print(store.error or "could not open gateway database", file=sys.stderr)
        return 1
    def _channel_arg(value: str | None) -> str | int | None:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    gateway = build_gateway(
        store=store, port=args.port, host=args.host, protocol=args.protocol,
        demo=args.demo, socket_path=args.socket,
        bot_channel=_channel_arg(args.bot_channel),
        pathbot_channel=_channel_arg(args.pathbot),
        testbot_channel=_channel_arg(args.testbot),
        ai_model=args.ai_model, ai_endpoint=args.ai_endpoint,
    )
    try:
        gateway.start()
        print(f"meshtui gateway listening on {gateway.socket_path}", flush=True)
        gateway.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"gateway failed: {exc}", file=sys.stderr)
        return 1
    finally:
        gateway.stop()
        store.close()
    return 0


def _send_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshtui send",
        description="Queue a message through the running local meshtui gateway.",
    )
    parser.add_argument("--socket", help="gateway Unix socket path")
    parser.add_argument("--protocol", choices=("meshtastic", "meshcore"))
    sub = parser.add_subparsers(dest="kind", required=True)
    dm = sub.add_parser("dm", help="send a direct message")
    dm.add_argument("--to", required=True, help="destination node id")
    dm.add_argument("--public-key", help="MeshCore peer's full public key (hex)")
    dm.add_argument("--wait", type=float, metavar="SECONDS",
                    help="wait for an end-to-end mesh acknowledgement")
    dm.add_argument("text", nargs="+")
    channel = sub.add_parser("channel", help="send to an exact channel name or slot")
    channel.add_argument("--channel", required=True, help="channel name or numeric slot")
    channel.add_argument("text", nargs="+")
    return parser


def run_send(argv: list[str]) -> int:
    from .gateway import request_gateway

    args = _send_parser().parse_args(argv)
    request: dict[str, object] = {
        "command": "send", "kind": args.kind, "text": " ".join(args.text),
    }
    if args.protocol:
        request["protocol"] = args.protocol
    if args.kind == "dm":
        request["to"] = args.to
        if args.public_key:
            request["public_key"] = args.public_key
    else:
        request["channel"] = args.channel
    socket_path = Path(args.socket) if args.socket else None
    try:
        result = request_gateway(request, socket_path)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"could not reach meshtui gateway: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        return 1
    wait_seconds = getattr(args, "wait", None)
    if wait_seconds is None:
        return 0
    if wait_seconds <= 0:
        print("--wait must be greater than zero", file=sys.stderr)
        return 2
    message_id = result.get("message_id")
    deadline = time.monotonic() + wait_seconds
    last_status = result.get("status")
    while True:
        try:
            delivery = request_gateway(
                {"command": "delivery", "message_id": message_id}, socket_path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"lost gateway while waiting for delivery: {exc}", file=sys.stderr)
            return 1
        if not delivery.get("ok"):
            print(json.dumps(delivery, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        status = delivery.get("status")
        if status != last_status:
            print(json.dumps(delivery, indent=2, sort_keys=True))
            last_status = status
        if status == "delivered":
            return 0
        if delivery.get("terminal"):
            return 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"timed out after {wait_seconds:g}s waiting for {message_id}; "
                  f"last status was {status}", file=sys.stderr)
            return 3
        time.sleep(min(0.5, remaining))


def run_gateway_status(argv: list[str]) -> int:
    from .gateway import request_gateway

    parser = argparse.ArgumentParser(prog="meshtui gateway-status")
    parser.add_argument("--socket")
    args = parser.parse_args(argv)
    try:
        result = request_gateway(
            {"command": "status"}, Path(args.socket) if args.socket else None)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"could not reach meshtui gateway: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gateway":
        return run_gateway(argv[1:])
    if argv and argv[0] == "send":
        return run_send(argv[1:])
    if argv and argv[0] == "gateway-status":
        return run_gateway_status(argv[1:])
    parser = argparse.ArgumentParser(
        prog="meshtui",
        description="Terminal dashboard and gateway for Meshtastic and MeshCore meshes.",
        epilog=("unattended commands: meshtui gateway, meshtui gateway-status, "
                "meshtui send dm, meshtui send channel"),
    )
    parser.add_argument("-p", "--port", help="serial device (default: autodetect)")
    parser.add_argument(
        "-H", "--host", metavar="HOST[:PORT]",
        help="connect over WiFi instead of USB, e.g. 192.168.1.42 or "
             "meshtastic.local (default port 4403)",
    )
    parser.add_argument("--demo", action="store_true", help="run against a synthetic mesh")
    parser.add_argument(
        "--gateway", nargs="?", const="", metavar="SOCKET",
        help="attach to a running 'meshtui gateway' over its Unix socket instead of "
             "opening a radio (with no SOCKET, uses the default socket path)",
    )
    parser.add_argument(
        "--protocol", choices=("auto", "meshtastic", "meshcore"), default="auto",
        help="mesh protocol to speak (default: auto - probes the radio)",
    )
    parser.add_argument("--list-ports", action="store_true",
                        help="show candidate serial ports and any nodes found on WiFi")
    parser.add_argument("--debug", action="store_true", help="write debug log to meshtui.log")
    parser.add_argument("--db", help=f"database path (default: {default_db_path()})")
    parser.add_argument("--no-store", action="store_true", help="do not persist to disk")
    parser.add_argument(
        "--restore-limit", type=int, default=3000, metavar="N",
        help="replay the last N stored packets on startup to rebuild sparklines, "
             "sensor readings and relay stats (0 disables; default 3000)",
    )
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
        print("serial:")
        for port in ports:
            print(f"  {port}")
        if not ports:
            print("  (none)")
        print("wifi (mDNS):")
        nodes = find_wifi_nodes()
        for hostname, address, label in nodes:
            print(f"  {address:<16} {hostname:<28} {label}")
            print(f"      meshtui --host {address}")
        if not nodes:
            print("  (none found - a node only appears here once WiFi is enabled on it)")
        return 0 if (ports or nodes) else 1

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
    if args.gateway is not None:
        # The gateway is the only writer of the database and the only owner of
        # the radio; this process gets everything over the socket instead.
        if args.port or args.host or args.demo:
            print("--gateway replaces the radio; drop --port/--host/--demo",
                  file=sys.stderr)
            return 2
        if args.db or args.no_store:
            print("note: --gateway ignores --db/--no-store; the gateway owns the database",
                  file=sys.stderr)
    elif not args.no_store:
        store = Store(args.db)
        if not store.open():
            print(f"warning: {store.error}", file=sys.stderr)
            store = None

    from .app import MeshTUI

    try:
        MeshTUI(port=args.port, demo=args.demo, store=store,
                restore_limit=max(0, args.restore_limit), host=args.host,
                protocol=args.protocol, gateway=args.gateway).run()
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
