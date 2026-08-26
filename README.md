# meshtui

A terminal dashboard for a [Meshtastic](https://meshtastic.org) mesh: live packet
feed, node table, chat, mesh statistics, and a braille map of where everything is
— all in one TUI, over USB serial.

```
  * Basecamp Relay  demo://synthetic-mesh  fw 2.5.0.demo   new message from HARB
╭─ nodes ──────────────────────────────────────────────────────╮╭─ packets - all ────────────────────────────────────╮
│    Node              SNR    Trend     Hop  Bat   Pkt   Age   ││ 16:24:51 POS    HARB  -> all    -14.1dB 2h 37.718… │
│ *  BASE  Basecamp R  -12.6            dir  41%   0     8m    ││ 16:24:53 TEXT   WXMT  -> all     +2.1dB    "wx sa… │
│ +  JEEP  Mobile Jee  -8.2          ▃  3    87%   1     1s    ││ 16:24:55 TEXT   HARB  -> all     -6.0dB 2h "radio… │
│ +  HARB  Harbor Rep  -4.9        ▂▄▄  2    73%   3     2s    ││ 16:24:56 TEXT   HARB  -> all     -4.9dB 2h "headi… │
│ +  R018  Relay 18    -7.9             1    -     0     4s   ▂││ 16:24:57 POS    JEEP  -> all     -8.2dB 3h 37.754… │
│ +  WXMT  Weather Ma  +2.1          ▆  dir  64%   1     5s    ││                                                    │
│ +  R021  Relay 21    -13.0            2    -     0     1m    ││                                                    │
│ +  R019  Relay 19    +3.5             2    -     0     2m    ││                                                    │
│ +  R005  Relay 5     -4.2             dir  -     0     4m    ││                                                    │
│ +  TRAIL Trailhead   -6.2             3    77%   0     6m    ││                                                    │
│ +  R012  Relay 12    -19.0            1    -     0     7m    ││                                                    │
│ +  FLD   Field Hand  +7.5             2    75%   0     7m    ││                                                    │
│ +  R011  Relay 11    +4.7             3    -     0     9m    ││                                                    │
╰──────────────────────────────────────────────────────────────╯│                                                    │
╭─ stats ──────────────────────────────────────────────────────╮╰────────────────────────────────────────────────────╯
│ packets                      5 nodes                      35 │╭─ chat - #LongFast ─────────────────────────────────╮
│ pkt/min                   40.9 active 15m                 14 ││  LongFast  Ops  Private                            │
│ sent                         0 direct                      9 ││ ╸━━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│ uptime                      9s with gps                   35 ││ 16:24 WXMT: wx says gusts to 30 tonight            │
│ snr avg                   -5.0 snr best                 +7.9 ││ 16:24 HARB: heading up the ridge, back in an ho    │
│                                                              ││                                                    │
│  packet mix                                                  ││                                                    │
│ TEXT                ##########                             3 ││                                              ▏     │
│ POS                 #######...                             2 ││────────────────────────────────────────────────────│
│                                                              ││ message   esc to leave, /help for commands         │
│                                                              ││                                                    │
╰──────────────────────────────────────────────────────────────╯╰────────────────────────────────────────────────────╯
 q quit  ? help  / chat  p pause feed  f filter  s sort  d dm node  m map  t trace                        ▏^p palette
```

Press `m` for the map. Nodes are plotted on a braille canvas (2x4 dots per
terminal cell), centred on your node, with distance rings and links to everything
you hear directly:

```
                                                      ⣀ ⢀⡀ ⠤ ⢀⡀ ⡀
                                                 ⠤ ⠐⠁     ⢀ R009⠈ ⠐⠂⠠⡀
                                            ⢀⠄ ⠉                       ⠑ ⣀
                                          ⠐⠂                               ⠤
                                        ⠐⠁              ⠠ R004        ⢀⡀     ⠢
                                     ⢀ ⠉                              ⠈⠁ R021 ⠐⠂
                                    ⡀⠈                ⢀⡀                        ⠐
                                    ⠁                 ⡨⠁⠐R012⠠⠄ ⣀                ⠐⠄
                                   ⠃              ⡀ ⠊            ⡿⠧R003            ⠆
                                  ⠃             ⢀ ⠁            ⢀⡜R023              ⠠⡀
                                 ⠆             ⡀⠁        ⠠⠄ ⣀ ⢀⠎     ⠈⠂             ⢀
                                ⢠         ⢀⣀   ⠁      ⡀⠈ ⠄ R027⠲⠆ R025 ⠆   ⠶ FLD    ⠈
                                ⢀         ⠘⠛⠑WXMT⣀⡀ ⢀⠈⡀     ⢠⠊ ⢀⠑R006  ⠠⡀            ⠃
                                ⠈  TRAIL  ⠈  R011 ⠈⠩⣿⡶R022⣤⣴⣷ R005 R020 ⠄            ⠂
                                ⠑            ⢠      ⠃ ⢀  ⢈⠟⢟⠲BASE⠘5.0km ⠃10km⡀       ⠘20km
                                ⠰             ⡄     ⠐⠄ ⢀⠔⠁ ⢸  ⠈⠛⠷R010  ⠐⠁   ⠈⠁ R019  ⠃
                                 ⡄             ⡀     ⡠⠲⠁   ⠈⡆⢀⡀⠒⢀⠈R024 ⠆            ⠠⠂
                                 ⢀             ⠁⡀  ⡠⠊   ⠉ ⠐⠂⢇⠉⠁ R018 ⢩⠖JEEP         ⡄
                                 ⠈⡀        R002 ⠈⠻⡿ R014  ⢀ ⢸⠠ R000 ⠤   ⠈⠑⣶⡆R015   ⢀
                                  ⠈        ⠉ RIDG ⠁ ⢄     ⠘⠃ R008 ⠔                ⠁
                                   ⠘        ⠛ HARB    ⠑ ⠠⠄ ⠤ ⢳⠂⠠⠉                 ⠉
                                    ⠈⠂                       ⢸                   ⠃
                                      ⠑                       ⡇                ⠘
                                       ⠈⠂                     ⣣⡀            ⢀⠐⠁
                                         ⠈⠁⢀⡀                 ⠛⠃R013      ⡠ ⠁
                                             ⠐⠄                       ⡀ ⠆
                                               ⠈⠁ ⠤ ⢀⡀           ⣀ ⠔ ⠈
                                                       ⠉ ⠈⠁ ⠉ ⠈⠁
 esc back  ↑ pan  + zoom in  - zoom out  f fit  c colour  r rings  i links  ? help  / chat  p pause feed  ▏^p palette
```

## Features

- **Live packet feed** — every packet, decoded and colour-coded by type, with SNR
  and hop count. Pause it, filter it, scroll back through it.
- **Node table** — SNR, hop distance, battery, packet count and age for every node,
  with a rolling SNR sparkline per node so you can see which links are fading.
- **Chat** — a tab per channel, plus direct messages. Live byte counter against the
  mesh's 233-byte payload limit.
- **Map** — braille-rendered positions, distance rings, pan and zoom, colour by
  SNR / hops / age.
- **Packet inspector** — full decoded protobuf and a hex dump for any packet.
- **History** — everything logged to SQLite, restored on startup, exportable to CSV.
- **Relay dependency** — which nodes actually carry your traffic, and what you lose
  if one drops. Built from the `relay_node` byte every packet carries.
- **Mesh health** — packet, duplicate and relay-cancel counters, noise floor and free
  heap, from the `localStats` telemetry nodes broadcast about themselves.
- **Sensors** — temperature, humidity, pressure, lux, air quality and more from any
  node on the mesh running a sensor.
- **Channel security audit** — grades your own channels' keys and flags any traffic
  on the mesh that is using a key published in Meshtastic's source.
- **Demo mode** — a synthetic mesh, so you can try the whole thing with no hardware.

## Quick start

Try it with no radio attached:

```sh
git clone https://github.com/jsaveker/meshtui.git
cd meshtui
uv run meshtui --demo
```

[uv](https://docs.astral.sh/uv/) installs the right Python and all dependencies
automatically — that one command is the whole setup. Then plug a Meshtastic node
in over USB and run:

```sh
uv run meshtui
```

### Without uv

Any Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
meshtui --demo
```

### Options

```sh
meshtui                      # autodetect a USB node
meshtui -p /dev/ttyUSB0      # explicit port
meshtui --demo               # synthetic mesh, no hardware needed
meshtui --list-ports         # show candidate serial devices
meshtui --debug              # also write meshtui.log
meshtui --no-store           # do not persist anything to disk
meshtui --db /path/mesh.db   # use a specific database
meshtui --stats              # print database summary and exit
meshtui --export packets:out.csv   # dump a table to CSV and exit
meshtui --audit              # offline channel security audit, then exit
```

Only one process can hold the serial port at a time — the meshtastic library opens
it exclusively, so a second `meshtui` (or the `meshtastic` CLI, or a serial monitor)
will be told the port is busy.

## Serial permissions

Serial devices are owned by a group that varies by distribution — `dialout` on
Debian, Ubuntu and Fedora, `uucp` on Arch. meshtui detects which group actually
owns your device and tells you exactly what to run, so if you hit a permission
error just read the message. It amounts to:

```sh
sudo usermod -aG dialout $USER     # or uucp, per the message
```

**Then log out and back in.** Supplementary groups are fixed at login, so a running
desktop session keeps the old group set and opening a new terminal will not help.
To test without logging out, start a shell that has the group first:

```sh
newgrp dialout     # this replaces your shell
meshtui            # run as a separate command, inside that new shell
```

(`newgrp dialout && meshtui` does not work — `newgrp` execs a new interactive shell,
so `meshtui` would only run after you exit it, back in the original shell.)

## Keys

| key | action |
|---|---|
| `?` | key reference overlay |
| `tab` | switch pane |
| `/` | focus the message box |
| `escape` | leave the message box / close an overlay |
| `enter` | node detail, or inspect the selected packet |
| `i` | inspect the selected packet |
| `m` | open the map |
| `a` | channel security audit |
| `r` | relay dependency and mesh health |
| `w` | sensors: environment and air quality |
| `p` | pause / resume the packet feed |
| `f` | cycle packet filter (all / chatty / text only) |
| `s` | cycle node sort (heard / name / snr / hops / packets) |
| `d` | open a direct message with the selected node |
| `t` | traceroute the selected node |
| `G` / `end` | jump to the newest packet and resume following |
| `ctrl+l` | clear the packet feed |
| `q` | quit |

Scrolling up in the packet feed pauses auto-follow; `G` resumes it.

**While the message box has focus every letter is text**, so the single-key
shortcuts above are unreachable until you leave it with `escape` or `tab`. The
footer reflects this — it shows `esc leave chat` while you are typing, and the chat
pane title reads `typing, esc to leave`. Half-composed text is kept when you escape out.

### Map keys

| key | action |
|---|---|
| arrows / `hjkl` | pan |
| `+` / `-` | zoom |
| `f` | refit and recentre |
| `c` | cycle colour: SNR / hops / age |
| `r` | toggle distance rings |
| `i` | toggle direct links |
| `t` | toggle movement trails |
| `esc` / `m` | back to the dashboard |

## Chat commands

```
/dm <node> <text>   direct message (node = short name or !id)
/trace <node> [hops]  traceroute; pass 1 to test only the direct link
/nodes              list known nodes
/clear              clear the conversation view
/help               show help
```

## Traceroute

`/trace <node>` asks a node to report the path back to you, with per-hop signal in
both directions:

```
  traceroute to STLH: 1 hop
    out   Prom  (you)
              +6.2dB  -> SNTL
                   ?  -> STLH
    back  STLH
              +4.0dB  -> Prom
```

The hop count matters when you are testing a specific link. With the default limit
the mesh may route around a weak path and still succeed, which looks like the link
is fine. **`/trace <node> 1` allows no relays**, so it succeeds only if the two
radios really can hear each other.

SNR is carried in quarter-dB with `-128` meaning "not measured", and the list has
one entry more than the route because the final endpoint reports what it received.

meshtui does not use the library's `sendTraceRoute()` helper: that calls
`waitForTraceRoute()`, blocking for tens of seconds, and its response handler
prints to stdout - both fatal in a TUI. The request is sent without waiting and the
reply is rendered from the ordinary `TRACEROUTE_APP` packet that comes back, so the
interface stays live. Replies can take 30 seconds or more.

## Message length

Meshtastic caps a packet's data payload at **233 bytes** (`DATA_PAYLOAD_LEN`, read
from the installed library at runtime rather than hard-coded). The chat pane shows
a live counter in its bottom border — `142/233 bytes` — yellow past 85%, red past
the limit.

The count is in **bytes, not characters**: accented letters cost 2 and most emoji
cost 4, so 60 emoji already exceed the limit. For `/dm <node> <text>` only `<text>`
is counted, since the rest never goes on the air. Over-length messages are refused
with an explanation, and your text is left in the box to trim.

## Relay dependency (`r`)

Every packet carries `relay_node`: the low byte of the node number that last
forwarded it. It is the only routing evidence on the wire, and aggregated over a
capture it shows what your view of the mesh actually rests on.

```
 relay                       share                  packets  origins  avg snr
 SNTL  Santaluz Solar        ████████████   50.8%      1020      131   +6.2dB
 e422  Meshtastic e422       ██████████░    44.7%       897      198  -16.4dB
 8176  Meshtastic 8176       ░               2.8%        56       31  -18.7dB
 STLH  Santaluz Home  ?x4                    0.9%        19        2   +6.4dB

 ! 2 relays carry 95.5% of your inbound traffic.
   losing SNTL alone would cost you about 51% of what you hear.
```

Only one byte identifies the relay, so several known nodes can match; those rows
are marked `?xN` rather than guessing.

The same screen shows **mesh health** from `localStats` — the telemetry nodes
broadcast about themselves. Duplicate rate and relay-cancel rate are direct
congestion indicators, free heap catches nodes about to fall over, and noise floor
tracks the RF environment.

## Sensors (`w`)

Any node with a sensor broadcasts readings to the whole mesh, which makes for a
free weather network:

```
 node                 temp   humid    press     iaq      gas   pm2.5     age
 KSR1  Kaiser Sola   47.7C     79%   986hPa     150      104       -  30m18s
 BCWb  Barton Cree   30.6C     37%   987hPa       -        -       -   1h17m
 KHB3  KohlBeam KJ   39.8C     26%   983hPa      60      202       3  12h22m
```

Columns appear only when some node is reporting that measurement. The protocol
also carries wind, rainfall, soil moisture, radiation, CO2 and particulates —
all handled if your mesh has them.

## Motion and position precision

Position packets carry more than coordinates. Nodes in motion report ground speed
and heading, which the map draws as a spur in the direction of travel, with a
trail of recent positions behind them (`t` toggles trails).

Nodes also advertise `precision_bits` — how much location precision they chose to
keep. Fewer bits means deliberate fuzzing, and the node detail view converts it to
a real distance. A mesh where everyone reports 13 bits is quantising positions to
roughly 5.8 km steps, so trails will look coarse; that is the senders' privacy
setting, not a bug here.

One correction worth recording: the protobuf comment for `ground_track` says
"1/100 degrees" and is marked `TODO: REPLACE`. Real traffic disagrees — values
like `23714000` are far past 36000, and `/1e5` gives 237.14°. meshtui uses 1e-5.

## Channel security audit (`a`)

Meshtastic's default channel key, and every single-byte "simple" key shorthand,
are printed in the firmware's own source. The protobuf documenting them says so
outright:

> These psks should be treated as only minimally secure, because they are listed
> in this source code.

meshtui uses that fact two ways. It grades **your own** channels from the keys on
your node:

```
 #  name        verdict       hash   why
 0  LongFast    NOT PRIVATE   8      single-byte shorthand 'default', listed in the firmware source
 1  Ops         ok            91     256-bit key
 2  Scouts      WEAK          44     4 bytes zero-padded to 16 - only 32 bits of key
 3  Open        NOT PRIVATE   -      empty key - inherits the primary channel, or no crypto
```

That `WEAK` row is worth knowing about: the firmware **zero-pads any PSK shorter
than 16 bytes** (`Channels.cpp:242`), so a 4-byte key really is a 32-bit keyspace.

And it reports which channels on the air are readable by anyone:

```
 hash  packets  senders  status
  143       31        1  no published key applies
   77        1        1  PUBLIC KEY (simple3)
```

Packets opened this way are tagged `[pub]` in the feed so they are never mistaken
for traffic that was actually private. `meshtui --audit` runs the same report
offline over a captured database.

**This only ever tries keys that Meshtastic publishes.** A channel using a real
random PSK is AES-128 or AES-256 and is not readable — by this tool or any other.
The audit exists to tell you when a channel is *not* protected, which is a thing
worth knowing, and to be honest about what leaks regardless.

### What leaks even when the payload does not

Sender, destination, packet id, hop count, signal strength and the 8-bit channel
hash all travel in the clear on every packet. The audit's third table builds an
activity map from exactly that — who transmits, on which channels, from how far
away — and needs no key at all. Encryption protects message content, not the fact
that you are talking.

The crypto is pinned to upstream's own test vectors in `tests/test_crypto.py`,
including the nonce vector from
`meshtastic/firmware test/test_crypto/test_main.cpp`. That matters: a wrong nonce
would make everything fail to decrypt, which looks identical to "every channel is
strong".

## History and privacy

Everything is logged to SQLite at `~/.local/share/meshtui/mesh.db` (override with
`--db`, disable entirely with `--no-store`). Writes go through a background thread,
so disk I/O never blocks the UI.

On startup meshtui restores nodes and chat from their own tables, then **replays the
last 3000 stored packets** through the same code path live traffic takes. That
replay is what rebuilds everything *derived*: SNR sparklines, sensor readings,
relay counts, foreign channels and movement trails. None of those are stored per
node, so without the replay they would start empty after every restart even though
the packets were safely on disk.

The replay reads and decodes on a worker thread and takes well under a second for a
few thousand packets. Replayed packets are marked historical: they rebuild state but
do not inflate this session's packet counter or per-node totals. Tune with
`--restore-limit N`, or `--restore-limit 0` to skip it.

Derived state is **also stored in its own right**, so it survives losing the packets
it came from. Sparklines, sensor readings, node stats, motion and trails live in
extra `nodes` columns; relay counters and channel observations get their own tables.
A `meta.state_ts` row records the newest packet already folded into that snapshot,
so a restart replays only what is genuinely new — the rest is shown as scrollback
without being counted twice.

That means you can prune the packets table (it is the bulk of the database) and keep
every derived view intact:

```sh
sqlite3 ~/.local/share/meshtui/mesh.db \
  "DELETE FROM packets WHERE ts < strftime('%s','now') - 7*86400; VACUUM;"
```

Older databases are migrated in place on first open — columns are added, nothing is
rewritten.

```sh
meshtui --stats
meshtui --export nodes:nodes.csv
sqlite3 ~/.local/share/meshtui/mesh.db \
  "SELECT portnum, COUNT(*) FROM packets GROUP BY 1 ORDER BY 2 DESC"
```

> **That database contains other people's data** — node IDs, GPS positions and
> message content from every radio your node hears. It stays on your machine;
> meshtui never uploads anything. It lives outside the repository and `*.db` is
> gitignored, but be deliberate before sharing one, and remember that a public
> mesh is not a private channel.

## How it works

- `model.py` — normalized `Packet` / `Node` / `ChatMessage` types
- `state.py` — `MeshState`: node database, packet ring buffer, chat log, stats
- `radio.py` — transports. `SerialLink` wraps the meshtastic library; `DemoLink`
  generates synthetic traffic so the UI runs without hardware.
- `store.py` — SQLite persistence, written on a background thread
- `geo.py` — haversine, bearing and km-offset helpers
- `crypto.py` — channel key expansion, the nonce, AES-CTR, and PSK grading
- `app.py` — the Textual app, keybindings, and the radio-to-UI thread bridge
- `widgets/canvas.py` — braille drawing surface (dots, lines, circles, labels)
- `widgets/` — node table, packet feed, chat, stats, map, relays, sensors, audit,
  node detail, packet inspector

The radio layer only ever calls one `emit(kind, payload)` callback, from its own
thread; `app.py` marshals that onto the UI thread with `call_from_thread`. Adding a
TCP, BLE or MQTT transport means writing another `RadioLink` subclass and nothing else.

## Development

```sh
uv run python tests/smoke.py    # headless end-to-end run against the demo mesh
uv run python tests/test_crypto.py  # crypto pinned to upstream's test vectors
uv run python tests/live.py 30  # connect to real hardware and report what it sees
```

`tests/smoke.py` drives the real app through Textual's headless pilot: every
keybinding, the map, the inspector, send and ack, slash commands, the payload
limit, and a database round-trip.

## License

MIT — see [LICENSE](LICENSE).
