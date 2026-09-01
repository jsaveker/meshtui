"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
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
    parser.add_argument(
        "--plugins", nargs="?", const="", metavar="DIR",
        help="load trusted Python plugins (default dir: ~/.config/meshtui/plugins)")
    parser.add_argument("--bot-channel", metavar="NAME_OR_SLOT",
                        help="enable @ai routing on this channel, e.g. '#bots' or 5")
    parser.add_argument("--pathbot", metavar="NAME_OR_SLOT",
                        help="answer !path on this channel with the route the "
                             "request traveled, e.g. '#bot' or 2")
    parser.add_argument("--testbot", metavar="NAME_OR_SLOT", action="append",
                        help="acknowledge test messages on this channel with the "
                             "hop count they arrived over, e.g. '#testing'; "
                             "repeat the flag to serve several channels")
    parser.add_argument("--testbot-location", metavar="PLACE", default="",
                        help="place name appended to testbot receipts, e.g. "
                             "'Field Site' -> '3 hops to Base Station "
                             "(Field Site)'")
    parser.add_argument("--weatherbot", metavar="NAME_OR_SLOT",
                        help="post the weather to this channel on a schedule, "
                             "e.g. '#wx'")
    parser.add_argument("--weatherbot-times", default="07:00,12:00,18:00",
                        metavar="HH:MM,...",
                        help="local posting times (default 07:00,12:00,18:00)")
    parser.add_argument("--sensorbot", metavar="NAME_OR_SLOT",
                        help="post a digest of heard sensor/battery telemetry "
                             "to this channel, e.g. '#sensors'")
    parser.add_argument("--sensorbot-minutes", type=float, default=60.0,
                        metavar="N", help="minutes between sensor digests "
                                          "(default 60)")
    parser.add_argument("--map-upload", action="store_true",
                        help="upload heard repeater/room-server adverts to "
                             "map.meshcore.io (needs firmware with private-key "
                             "export)")
    parser.add_argument("--ai-model", default="gpt-5-mini")
    parser.add_argument("--ai-endpoint", help="Responses-compatible API endpoint")
    telemetry = parser.add_argument_group("companion telemetry bot")
    telemetry.add_argument(
        "--telemetry-bot-allow", metavar="NODE_ID", action="append", default=[],
        help="allow this node to DM the local telemetry bot (repeatable; enables the bot)")
    telemetry.add_argument(
        "--telemetry-bot-trigger", default="!mesh", metavar="WORD",
        help="required first word in an allowed DM (default: !mesh)")
    telemetry.add_argument(
        "--telemetry-bot-position", action="store_true",
        help="allow the bot to include coordinates in replies (off by default)")
    mqtt = parser.add_argument_group("MQTT and Home Assistant discovery")
    mqtt.add_argument("--mqtt-host", metavar="HOST",
                      help="publish mesh telemetry to this MQTT broker")
    mqtt.add_argument("--mqtt-port", type=int, default=1883, metavar="PORT")
    mqtt.add_argument("--mqtt-username", metavar="USER")
    mqtt.add_argument(
        "--mqtt-password-env", metavar="ENV_VAR",
        help="read the MQTT password from this environment variable")
    mqtt.add_argument("--mqtt-tls", action="store_true", help="enable MQTT TLS")
    mqtt.add_argument("--mqtt-ca", metavar="FILE", help="CA certificate for MQTT TLS")
    mqtt.add_argument("--mqtt-prefix", default="meshtui", metavar="TOPIC",
                      help="MQTT state topic root (default: meshtui)")
    mqtt.add_argument("--ha-discovery-prefix", default="homeassistant", metavar="TOPIC",
                      help="Home Assistant discovery root (default: homeassistant)")
    mqtt.add_argument("--mqtt-gateway-id", metavar="ID",
                      help="stable gateway identifier (default: local hostname)")
    mqtt.add_argument("--mqtt-include-position", action="store_true",
                      help="publish node coordinates (off by default)")
    mqtt.add_argument("--mqtt-events", action="store_true",
                      help="publish non-retained normalized packet/message/receipt events "
                           "without raw radio payloads")
    mqtt.add_argument("--mqtt-active-seconds", type=float, default=900, metavar="SECONDS",
                      help="age after which a node is inactive (default: 900)")
    notify = parser.add_argument_group("notifications")
    notify.add_argument("--notify-node", action="append", default=[], metavar="NAME_OR_ID",
                        help="notify when this node reappears (repeatable; wildcards allowed)")
    notify.add_argument("--notify-trace-fail", action="store_true",
                        help="notify when a trace/path discovery reports failure")
    notify.add_argument("--notify-desktop", action="store_true",
                        help="deliver configured rules to the local desktop")
    notify.add_argument("--notify-active-seconds", type=float, default=900,
                        metavar="SECONDS", help="absence window before reappearance (default: 900)")
    notify.add_argument("--ntfy-topic", metavar="TOPIC",
                        help="deliver configured rules to an ntfy topic")
    notify.add_argument("--ntfy-url", default="https://ntfy.sh", metavar="URL",
                        help="ntfy server root (default: https://ntfy.sh)")
    notify.add_argument("--ntfy-token-env", metavar="ENV_VAR",
                        help="read an ntfy access token from this environment variable")
    parser.add_argument("--debug", action="store_true")
    return parser


def run_gateway(argv: list[str]) -> int:
    import signal
    from .gateway import build_gateway

    args = _gateway_parser().parse_args(argv)

    # systemd stops a service with SIGTERM; turn it into the same clean exit as
    # Ctrl-C so the radio and socket are released and the next start is clean.
    # Disarm after the first delivery: a signal that lands while the finally
    # block is already cleaning up would escape as a traceback and mark the
    # unit failed (exit 130) over nothing.
    def _graceful(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mqtt_config = None
    if args.mqtt_password_env and not args.mqtt_host:
        print("--mqtt-password-env requires --mqtt-host", file=sys.stderr)
        return 2
    if args.mqtt_host:
        from .ha_mqtt import MQTTConfig, default_gateway_id
        password = None
        if args.mqtt_password_env:
            password = os.environ.get(args.mqtt_password_env)
            if password is None:
                print(f"MQTT password environment variable {args.mqtt_password_env!r} is not set",
                      file=sys.stderr)
                return 2
        try:
            mqtt_config = MQTTConfig(
                host=args.mqtt_host, port=args.mqtt_port,
                username=args.mqtt_username, password=password,
                tls=args.mqtt_tls, ca_certs=args.mqtt_ca,
                base_topic=args.mqtt_prefix,
                discovery_prefix=args.ha_discovery_prefix,
                gateway_id=args.mqtt_gateway_id or default_gateway_id(),
                include_position=args.mqtt_include_position,
                publish_events=args.mqtt_events,
                active_seconds=args.mqtt_active_seconds,
            )
        except ValueError as exc:
            print(f"invalid MQTT configuration: {exc}", file=sys.stderr)
            return 2
    ntfy_token = None
    if args.ntfy_token_env:
        if not args.ntfy_topic:
            print("--ntfy-token-env requires --ntfy-topic", file=sys.stderr)
            return 2
        ntfy_token = os.environ.get(args.ntfy_token_env)
        if ntfy_token is None:
            print(f"ntfy token environment variable {args.ntfy_token_env!r} is not set",
                  file=sys.stderr)
            return 2
    store = Store(args.db)
    if not store.open():
        print(store.error or "could not open gateway database", file=sys.stderr)
        return 1
    def _channel_arg(value: str | None) -> str | int | None:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    try:
        gateway = build_gateway(
            store=store, port=args.port, host=args.host, protocol=args.protocol,
            demo=args.demo, socket_path=args.socket,
            bot_channel=_channel_arg(args.bot_channel),
            pathbot_channel=_channel_arg(args.pathbot),
            testbot_channel=([_channel_arg(c) for c in args.testbot]
                             if args.testbot else None),
            testbot_location=args.testbot_location,
            telemetry_bot_nodes=args.telemetry_bot_allow,
            telemetry_bot_trigger=args.telemetry_bot_trigger,
            telemetry_bot_position=args.telemetry_bot_position,
            mqtt_config=mqtt_config,
            plugin_dir=args.plugins,
            notify_nodes=args.notify_node,
            notify_trace_failures=args.notify_trace_fail,
            notify_desktop=args.notify_desktop,
            ntfy_topic=args.ntfy_topic,
            ntfy_url=args.ntfy_url,
            ntfy_token=ntfy_token,
            notify_active_seconds=args.notify_active_seconds,
            map_upload=args.map_upload,
            weatherbot_channel=_channel_arg(args.weatherbot),
            weatherbot_times=args.weatherbot_times,
            sensorbot_channel=_channel_arg(args.sensorbot),
            sensorbot_minutes=args.sensorbot_minutes,
            ai_model=args.ai_model, ai_endpoint=args.ai_endpoint,
        )
    except (RuntimeError, ValueError) as exc:
        store.close()
        print(f"invalid gateway configuration: {exc}", file=sys.stderr)
        return 2
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
        # A late SIGTERM (or an impatient Ctrl-C) during cleanup must not
        # abort it - a half-stopped gateway leaves the radio and socket dirty.
        # And cleanup itself must not outlive systemd's stop timeout: a hung
        # gateway.stop() (a wedged serial close, a stuck disconnect) once ran
        # past it and the SIGKILL that followed wedged the radio. Exit under
        # our own power before systemd loses patience.
        import threading
        deadline = threading.Timer(8.0, lambda: os._exit(0))
        deadline.daemon = True
        deadline.start()
        try:
            gateway.stop()
            store.close()
        except KeyboardInterrupt:
            pass
        # Exit without interpreter teardown: everything above already flushed
        # and released, and teardown joins any lingering non-daemon thread -
        # an in-flight HTTP call once held that past systemd's stop timeout,
        # and the resulting SIGKILL mid-serial-write wedged the radio.
        os._exit(0)
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


def run_serve(argv: list[str]) -> int:
    """Serve the read-only browser companion from a gateway subscription."""
    import signal
    from .web import CompanionServer

    parser = argparse.ArgumentParser(
        prog="meshtui serve",
        description="Read-only map and chat web companion over a running gateway socket.")
    parser.add_argument("--gateway", metavar="SOCKET", help="gateway Unix socket path")
    parser.add_argument("--listen", default="127.0.0.1", metavar="ADDRESS",
                        help="HTTP listen address (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args(argv)
    server = CompanionServer(args.gateway, args.listen, args.port)

    def _graceful(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)
    try:
        server.start()
        host, port = server.address
        print(f"meshtui companion listening on http://{host}:{port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"companion failed: {exc}", file=sys.stderr)
        return 1
    finally:
        server.stop()
    return 0


def run_replay(argv: list[str]) -> int:
    """Replay a captured SQLite/PCAP window through a second read-only gateway."""
    import signal
    from .replay import (build_pcap_replay_gateway, build_replay_gateway,
                         default_replay_socket_path)

    parser = argparse.ArgumentParser(
        prog="meshtui replay",
        description="Replay a SQLite or PCAP/PCAPNG window as a read-only ghost gateway.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="source meshtui SQLite database")
    source.add_argument("--pcap", help="source PCAP or PCAPNG packet capture")
    parser.add_argument("--socket", default=str(default_replay_socket_path()),
                        help="ghost gateway socket path")
    parser.add_argument("--from", dest="start_ts", type=float, metavar="EPOCH")
    parser.add_argument("--to", dest="end_ts", type=float, metavar="EPOCH")
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="playback speed multiplier (default: 1)")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--protocol", choices=("auto", "meshtastic", "meshcore"),
                        default="auto")
    args = parser.parse_args(argv)
    if args.start_ts is not None and args.end_ts is not None \
            and args.start_ts > args.end_ts:
        print("--from must not be later than --to", file=sys.stderr)
        return 2
    try:
        builder = build_pcap_replay_gateway if args.pcap else build_replay_gateway
        gateway = builder(
            args.pcap or args.db, socket_path=args.socket, start_ts=args.start_ts,
            end_ts=args.end_ts, limit=args.limit, speed=args.speed,
            loop=args.loop, protocol=args.protocol)
    except ValueError as exc:
        print(f"invalid replay: {exc}", file=sys.stderr)
        return 2

    def _graceful(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)
    try:
        gateway.start()
        print(f"ghost mesh listening on {gateway.socket_path}", flush=True)
        print(f"attach with: meshtui --gateway {gateway.socket_path}", flush=True)
        gateway.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    finally:
        gateway.stop()
    return 0


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


def should_auto_attach(args) -> bool:
    """Plain `meshtui` attaches to a running gateway instead of the radio.

    The gateway owns the serial port; a second opener doesn't just fail,
    the dual access has wedged the radio's USB stack. Any explicit radio
    choice (--port/--host/--demo) still wins.
    """
    if args.gateway is not None or args.port or args.host or args.demo:
        return False
    from .gateway import default_socket_path
    path = default_socket_path()
    if not path.exists():
        return False
    # A SIGKILLed gateway leaves its socket file behind; attach only to a
    # gateway that actually answers, else fall through to a direct radio.
    import socket as socket_mod
    try:
        probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        probe.settimeout(1.0)
        probe.connect(str(path))
        probe.close()
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gateway":
        return run_gateway(argv[1:])
    if argv and argv[0] == "send":
        return run_send(argv[1:])
    if argv and argv[0] == "gateway-status":
        return run_gateway_status(argv[1:])
    if argv and argv[0] == "serve":
        return run_serve(argv[1:])
    if argv and argv[0] == "replay":
        return run_replay(argv[1:])
    parser = argparse.ArgumentParser(
        prog="meshtui",
        description="Terminal dashboard and gateway for Meshtastic and MeshCore meshes.",
        epilog=("unattended commands: meshtui gateway, meshtui serve, meshtui replay, "
                "meshtui gateway-status, meshtui send dm, meshtui send channel"),
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

    if should_auto_attach(args):
        print("attaching to running gateway (use --port to open a radio directly)",
              file=sys.stderr)
        args.gateway = ""

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
