# meshtui

A terminal dashboard for [Meshtastic](https://meshtastic.org) **and**
[MeshCore](https://meshcore.co.uk) meshes: live packet feed, node table, chat, mesh
statistics, a braille map of where everything is, and remote administration of
MeshCore repeaters over RF — all in one TUI.

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
  active protocol's payload limit (Meshtastic's installed protobuf limit or a
  conservative 133-byte MeshCore limit).
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

## Running unattended (for days)

The TUI is for sitting in front of. To leave a node **receiving, storing and
relaying for days**, run the headless **gateway** instead — it owns the radio,
reconnects on its own if the link drops, and has no UI that can hang. Clients
(the CLI, and a future attached TUI) talk to it over a local socket.

```sh
meshtui gateway                 # foreground: owns the radio, prints a socket path
meshtui gateway-status          # ask the running gateway how it is doing
meshtui send channel 0 "hi"     # queue a message through it
meshtui send dm <node> "hi"
```

For true days-long operation, run it under **systemd** so any crash restarts
automatically. A unit is provided at
`~/.config/systemd/user/meshtui-gateway.service`:

```sh
systemctl --user daemon-reload
systemctl --user enable --now meshtui-gateway
loginctl enable-linger $USER     # keep it running when you are logged out
systemctl --user status meshtui-gateway
journalctl --user -u meshtui-gateway -f
```

Only one process can hold the radio, so stop the gateway before launching the
TUI on the same node (`systemctl --user stop meshtui-gateway`), or point the TUI
at a *different* radio.

### What makes it survive

- The companion never pushes messages, so the link **polls every ~2.5s** to pull
  queued and live traffic; a run that stops polling would go quiet without
  noticing, which is the bug behind "left it overnight, no messages".
- **Repeated poll failures mark the radio dead**, so the gateway's reconnect loop
  reopens the link instead of spinning on a dead port.
- **SIGTERM shuts down cleanly** (releasing the serial port), so `systemctl
  restart` never leaves the USB wedged or a half-dead process on the port - the
  state that made a plain restart fail to reconnect.

## Two protocols

meshtui speaks both mesh protocols and works out which one is attached:

```sh
meshtui                          # probe the radio and pick the right protocol
meshtui --protocol meshcore      # force MeshCore
meshtui --protocol meshtastic    # force Meshtastic
```

Detection probes for MeshCore first, because it fails fast — the Meshtastic
library blocks waiting for a config dump that a MeshCore node never sends, so
trying that first would hang on the wrong hardware.

Most panes work for both. A few are protocol-specific, because the underlying
concept only exists on one side:

| pane | Meshtastic | MeshCore |
|---|---|---|
| packets, nodes, chat, map, stats | yes | yes |
| relay dependency (`r`) | yes | — (MeshCore routes by explicit path, not relay-flooding) |
| channel security audit (`a`) | yes | — |
| remote admin (`x`) | — | yes |

### If MeshCore direct messages fail

MeshCore encrypts a direct message with an X25519 shared secret derived from the
sender's private key and the recipient's public key. The recipient therefore needs
the **sender's** public key to decrypt anything — and it only learns that from an
advert.

A radio with `autoadd_config = 0` discards every advert it hears. It still receives
the packets, but holds no key for the sender, so it cannot decrypt the message and
cannot acknowledge it. The sender sees a plain delivery failure with no clue why.

meshtui warns about this on connect and `A` fixes it. The bits, from
`examples/companion_radio/MyMesh.cpp`:

| bit | meaning |
|---|---|
| `0x01` | overwrite oldest non-favourite when contacts are full |
| `0x02` | auto-add Chat / companion nodes |
| `0x04` | auto-add Repeaters |
| `0x08` | auto-add Room Servers |
| `0x10` | auto-add Sensors |

`A` sets `0x1F` (all types, plus overwrite-oldest). `V` sends a flood advert, which
is how you announce yourself so peers can message *you*. Contacts only appear as
peers advertise, so a new node stays empty for a while — trigger an advert from the
other device to speed it up.

## Chat

Chat lives in two places. The **corner pane** (bottom right) is a read-only
monitor of *every* channel at once — each line tagged with its channel — with no
input. It is for glancing, not typing. Press **`z`** (or click its header) to open
the pop-out.

The **pop-out overlay** is where you read a single channel and write: channels
down the left, a full-width conversation on the right. `/` opens it straight onto
the message box.

```
  chat  -  #weather   (7 nodes, meshcore)   esc to close
╭─ channels ─────────────╮╭─ #weather ───────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ ★ All activity         ││  OZTX v4   19:39                                                                                                         │
│ # Public               ││    Lunar eclipse tonight! clear skies out west                                                                           │
│ # weather              ││                                                                                                                          │
│ # wx  1                ││  SolarNerd   19:47                                                                                                       │
│ # wardriving           ││    getting dark, we may get smacked                                                                                      │
│ # licensed             ││    anyone north of 290 seeing this yet?                                                                                  │
│ # backontheroof        ││                                                                                                                          │
│                        ││  you   19:48  ✓                                                                                                          │
│                        ││    copy, battening down here                                                                                             │
│                        ││                                                                                                                          │
│                        ││  Picassoman-B   19:48                                                                                                    │
│                        ││    smells great though                                                                                                   │
```

- **Channels and DMs down the left**, with unread counts — no more tab overflow.
  `↑`/`↓` to move, or `[` and `]` from anywhere.
- **`★ All activity`** merges every channel into one stream, each line tagged with
  its channel — the best way to watch a busy mesh, then dive into one to reply.
- **Messages are grouped** by sender: name and time once, the body indented and
  wrapped with room. Your own messages show a delivery tick (`✓` acked, `··`
  pending).
- The corner monitor always shows everything; the overlay is where you pick a
  channel and type. Selecting a channel there is what a typed message is addressed
  to. `esc` closes the overlay.

## Channels (`c`)

A MeshCore radio has a fixed set of channel slots — 40 on current firmware — and
they are **not filled contiguously**. A radio can have channels at slots 0, 5 and
12 with everything between them empty, and the slot index is what a channel
message is addressed to.

```
 channels  -  3 of 40 slots in use
 ╭─ channel slots ──────────────────╮╭─ help ─────────────────────────────╮
 │  #   name                    key ││ commands                           │
 │  0   Public                  set ││   add <idx> <name>                 │
 │  5   #austin             derived ││   add <idx> <name> <hex>           │
 │ 12   Ops                     set ││   del <idx>                        │
 │ 13   (empty)                     ││   refresh                          │
 ╰──────────────────────────────────╯╰────────────────────────────────────╯
```

With more than a handful of channels the tab bar overflows the pane. `[` and `]`
step between channels from anywhere, and pressing enter on a row in this screen
jumps the chat straight to that channel.

Keys work two ways. A name starting with `#` derives its key from
`sha256(name)[:16]`, so anyone who knows the name can join — that is how public
channels are shared. Supply an explicit 32-hex-character key instead for a private
group:

```
add 5 #austin                                  join by name
add 6 Ops 00112233445566778899aabbccddeeff     explicit 16-byte key
del 6                                          clear the slot
```

## Remote administration (`x`, MeshCore)

MeshCore repeaters and room servers can be administered **over the air**: log in
with the node's password and run its console commands across the mesh. No cable,
no climbing onto the roof.

```
 remote admin  Santaluz Solar  authenticated
 ╭─ repeaters ────────────╮╭─ session ──────────────────────────────╮
 │ * Santaluz Solar  REPE ││ 16:12:04 > login to Santaluz Solar ... │
 │   Tachyon Mobile  CHAT ││ 16:12:09 Santaluz Solar ** logged in **│
 ╰────────────────────────╯│ 16:12:15 > ver                         │
 ╭─ commands ─────────────╮│ 16:12:21 Santaluz Solar v1.17.1        │
 │  ver      firmware ver ││                                        │
 │  advert   send advert  ││                                        │
 ╰────────────────────────╯╰────────────────────────────────────────╯
```

The session log persists across restarts, scoped to the radio that ran it, so a
reply that arrives after you have closed meshtui is still there next time.
**Credentials are never written down**: anything typed after `login` or `password`
is redacted before it reaches memory or disk.

Select a repeater, type `login <password>`, then send commands. `F2` requests
status, `F3` telemetry, `F4` logs out. Replies travel over LoRa, so they take
seconds and can be lost — the session log shows exactly what came back.

A remote command and its reply are ordinary text messages tagged
`TxtType.CLI_DATA`; that tag is the only thing separating a repeater's console
output from someone messaging you. `CLI_REPLY` is a different event entirely — it
belongs to the *local* device's console — so a reply that is not checked for its
tag silently lands in the chat pane instead of the admin session.

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
meshtui -H 192.168.1.42      # connect over WiFi instead of USB
meshtui -H meshtastic.local  # ... or by mDNS name
meshtui --demo               # synthetic mesh, no hardware needed
meshtui --list-ports         # show candidate serial devices
meshtui --debug              # also write meshtui.log
meshtui --no-store           # do not persist anything to disk
meshtui --db /path/mesh.db   # use a specific database
meshtui --stats              # print database summary and exit
meshtui --export packets:out.csv   # dump a table to CSV and exit
meshtui --audit              # offline channel security audit, then exit
```

## Unattended home gateway, DMs, and `#bots`

Run the gateway on the computer that stays at home with the companion radio
attached. It is the **only process that opens the radio**. The TUI and a second
CLI process must not open the same serial port at the same time; local producers
send requests to the gateway's owner-only Unix socket instead.

```bash
# Home computer: reliable radio owner and outbound queue
meshtui gateway --protocol meshtastic --port /dev/ttyACM0

# Any local automation on that home computer: DM the radio paired to your phone
meshtui send dm --to '!20000002' 'garage is closed and the alarm is armed'

# Acceptance mode: exit 0 only after the direct-message mesh ACK arrives
meshtui send dm --to '!20000002' --wait 300 'numbered field test'

# If a MeshCore peer is not yet in the live contact list, its full key is usable
meshtui send dm --to '!2935ec59' \
  --public-key "$MOBILE_MESHCORE_PUBLIC_KEY" 'numbered test 1'

# Inspect the unattended process
meshtui gateway-status
```

The send command records one logical message in SQLite before touching the
radio. The gateway reopens a failed or disconnected companion link; messages
remain queued across radio and process restarts. A direct message is retried with
backoff up to three attempts and expires after 24 hours.
The displayed states mean:

- `queued`: durable locally, not yet accepted by the radio
- `sent`: accepted by the local radio; it has **not** proved the recipient saw it
- `delivered`: the direct-message mesh acknowledgement arrived
- `failed` or `expired`: the retry or time limit stopped the attempt

Channel broadcasts stop at `sent`, because neither protocol can prove that every
channel member received a broadcast. A missing acknowledgement can also cause a
direct-message retry, so the mobile recipient should tolerate the occasional
duplicate for important notifications.

For a named channel, use its name while the radio is online or its exact numeric
slot while offline. Sparse slots are preserved:

```bash
meshtui send channel --channel '#bots' '@ai summarize the weather alert'
meshtui send channel --channel 12 '@ai summarize the weather alert'
```

### Tool-free AI replies

Set `OPENAI_API_KEY` only in the home gateway's service environment, then opt in
to one channel when starting it:

```bash
OPENAI_API_KEY='...' meshtui gateway \
  --protocol meshtastic --port /dev/ttyACM0 \
  --bot-channel '#bots' --ai-model gpt-5-mini
```

Messages must begin with `@ai`. The router answers only in the configured bot
channel or in a direct message addressed to the home node. It passes the provider
only the prompt, sender ID, and conversation label; the provider API has no tool
interface and receives no database history, local files, commands, or radio
credentials. The request explicitly supplies no tools, forbids tool choice, and
sets `store: false`. Replies are prefixed `[AI]`, UTF-8 safe, limited to three packets,
rate-limited per sender, and duplicate-suppressed in SQLite across restarts.

From work, use the mobile radio's normal Meshtastic or MeshCore phone client:

1. Send `@ai ...` in the shared `#bots` channel for a channel reply, or DM the
   home node with `@ai ...` for a private reply.
2. The home radio receives it, the gateway invokes the text-only provider, and
   the response goes back through the same channel or peer route.
3. Home automations use `meshtui send dm --to <mobile-node-id> ...` to initiate a
   message to you without opening the serial port themselves.

This provides the software path, not RF coverage. Home-to-work delivery still
requires both nodes to share the same modem/region and channel configuration and
to have a real route: direct RF, a chain of repeaters, or—on Meshtastic—an MQTT
bridge deliberately configured at both ends. MeshCore uses its learned contacts
and repeater paths; the home radio must first know the mobile peer's full public
key. Prove the route with a harmless numbered DM before depending on it for an
alarm.

For an always-on installation, run `meshtui gateway` under your normal service
manager with restart-on-failure, a fixed `--db` and `--socket`, access to the
serial device, and the API key in a protected environment file. Do not place the
key in a mesh message or command-line argument.

Non-AI home bots do not need an SDK. Their final action is simply a local command:

```bash
meshtui send channel --channel '#bots' 'backup completed at 02:14'
meshtui send dm --to '!20000002' 'water sensor is dry again'
```

### Home-to-work RF acceptance test

Do this before treating the path as operational:

1. Record the home and mobile node IDs, protocol, region/modem preset, `#bots`
   slot, and—on MeshCore—the full public keys. Confirm both radios show the same
   intended channel configuration.
2. Start the home gateway with a new test database. At work, send the home node
   `@ai field-001 reply with field-001` by DM. Require the numbered response on
   the mobile device and a direct-message acknowledgement at home.
3. From the home computer, run
   `meshtui send dm --to <mobile-id> --wait 300 'field-002'`. Require `field-002`
   on the phone and a zero exit status after `delivered`; `sent` alone is not
   end-to-end proof.
4. Repeat on `#bots`, including a channel stored at a sparse slot such as 12.
   Expect `sent`, not `delivered`, because broadcasts have no recipient ACK.
5. Disconnect the home radio, enqueue `field-003`, restart the gateway, then
   reconnect it. The same message ID must survive and drain from the outbox; the
   chat history must contain one logical outgoing message.
6. Make the mobile node unavailable for one test. Confirm exponential retry stops
   after three attempts or at the 24-hour expiry. Restore it and send a fresh
   numbered message; do not silently extend retries for an alarm forever.
7. Re-send the same captured `@ai` packet in a lab/replay setup. The provider must
   be called once. Send a long emoji-heavy answer and verify every emitted frame
   remains under the active protocol's byte limit and no more than three frames
   are transmitted.

The repository simulations cover steps 4–7 in `tests/test_service.py` and
`tests/test_gateway_bot.py`; steps 1–3 require the two actual radios and the route
between home and work.

Only one process can hold the serial port at a time — the meshtastic library opens
it exclusively, so a second `meshtui` (or the `meshtastic` CLI, or a serial monitor)
will be told the port is busy.

## Connecting over WiFi

ESP32 nodes (Heltec, Station G2, T-Beam and friends) can serve the same protobuf
stream over TCP that they serve over USB. The library's `TCPInterface` shares its
base class with `SerialInterface`, so everything here — telemetry, the relay pane,
noise floor, sensors, chat, traceroute — works identically. The USB cable is only
ever a pipe; none of that data is produced by it.

That lets you put the radio where the RF is good rather than where the computer is.

**1. Enable WiFi on the node, over USB, in your own terminal** (so the password
stays out of any logs):

```sh
uv run meshtastic --port /dev/ttyACM0 \
  --set network.wifi_enabled true \
  --set network.wifi_ssid "YourNetwork" \
  --set network.wifi_psk "YourPassword"
```

The node reboots and joins the network.

**2. Find it.** The firmware advertises `_meshtastic._tcp` over mDNS with its short
name and node id:

```sh
meshtui --list-ports          # lists serial ports AND nodes found on WiFi
```

**3. Connect:**

```sh
meshtui -H 192.168.1.42
meshtui -H meshtastic.local      # if mDNS resolves on your machine
meshtui -H 192.168.1.42:4403     # explicit port
```

### Things to know first

- **WiFi and Bluetooth are mutually exclusive on ESP32.** Enabling WiFi means that
  node stops being reachable from the phone app over BLE.
- **WiFi costs power.** Combined with a high `tx_power`, a 500 mA USB-A port may not
  be enough — a node set to 30 dBm can want more than 2 W for the amplifier alone
  during transmit. Use a proper supply for a node you intend to leave running.
- **Only one client at a time**, same as serial.
- Check `hasWifi` before assuming a board has it: `meshtastic --port ... --info`.
  nRF52 boards (RAK4631 and similar) have no WiFi at all.

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
| `z` | expand chat to the full-screen overlay |
| `[` `]` | previous / next channel |
| `escape` | leave the message box / close an overlay |
| `enter` | node detail, or inspect the selected packet |
| `i` | inspect the selected packet |
| `m` | open the map |
| `a` | channel security audit |
| `c` | browse and edit channels; enter jumps to one |
| `[` `]` | previous / next channel |
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

## Seeing a message repeated (MeshCore)

When you send a message on a MeshCore channel, repeaters rebroadcast it and your
own radio hears those rebroadcasts. meshtui ties each one back to your message and
shows the repeaters next to it, the way the phone apps do:

```
 you  #Public   11:16  ✓  ⟳ Santaluz Solar Repeater, e422
```

A repeater is named when its public key is known (its path byte is the key's first
byte); otherwise it shows as `0x<hex>`. The packet hash reported with each repeat
is stable, so every repeater that carries the same message accumulates onto the one
line — and a different packet on the same channel is never misattributed to it.

## Message length

Meshtastic's limit comes from the installed protobuf's `DATA_PAYLOAD_LEN` (233
bytes in current releases). MeshCore is held to a conservative **133 UTF-8
bytes**. The chat pane switches with the connected protocol and shows a live
counter in its bottom border — `112/133 bytes`, for example — yellow past 85% and
red past the limit.

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

## Multiple radios

Signal, hop count, relay share and packet counts all mean *"as heard from here"*.
Swap the radio and they mean something different, so meshtui separates:

- **Facts** about a node — name, hardware, role, position, sensor readings — are
  shared. Every radio contributes and they accumulate into one picture of the mesh.
- **Observations** — SNR, hops, packet counts, sparklines, relay shares, channel
  sightings — are keyed by the radio that made them, in `node_obs` and the
  `local_node` column on `packets`, `relays`, `relay_edges` and `foreign_channels`.

On startup the last-attached radio's view is loaded so the dashboard is useful
immediately, and works with no radio plugged in at all. If a *different* node then
connects, that view is dropped rather than blended into, and the new radio starts
its own. Nothing is lost: each radio's history stays in the database under its own
id, and switching back restores it.

This matters most when one radio is mobile. Merging a portable node's readings from
all over town into a fixed station's picture would make the relay analysis quietly
wrong.

```sh
sqlite3 ~/.local/share/meshtui/mesh.db \
  "SELECT local_node, COUNT(*) FROM node_obs GROUP BY 1"
```

Databases from before this split are migrated in place. The previous radio is
identified from its outgoing messages, `localStats` or routing acknowledgements —
only the locally attached node's reach the client — and every existing row is
attributed to it.

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

Replay rebuilds each packet from its stored **columns**, not by re-decoding the raw
payload — the columns already hold everything the UI renders, and a decoder only
understands its own protocol. Re-running the Meshtastic decoder over MeshCore rows
turned every one into `? -> ? UNKNOWN`.

The replay reads on a worker thread and takes well under a second for a few
thousand packets. Replayed packets are marked historical: they rebuild state but
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
> message content from every radio your node hears. It stays on your machine by
> default. If you explicitly enable the AI router, only an `@ai` prompt plus its
> sender ID and conversation label is sent to the configured provider; history
> and unrelated traffic are not. The database lives outside the repository and
> `*.db` is gitignored, but be deliberate before sharing one, and remember that a
> public mesh is not a private channel.

## How it works

- `model.py` — normalized packets plus `ChannelRef`, `PeerRef`, and `SendReceipt`
- `state.py` — `MeshState`: node database, packet ring buffer, chat log, stats
- `service.py` — protocol-neutral state ownership, durable outbox, retries, receipts
- `radio.py` — transports. `SerialLink` wraps the meshtastic library; `DemoLink`
  generates synthetic traffic so the UI runs without hardware.
- `gateway.py` — unattended single-radio owner and local `0600` Unix-socket API
- `bot.py` — opt-in, tool-free AI routing, rate limits, dedupe, and chunking
- `store.py` — SQLite persistence, written on a background thread
- `geo.py` — haversine, bearing and km-offset helpers
- `crypto.py` — channel key expansion, the nonce, AES-CTR, and PSK grading
- `app.py` — the Textual app, keybindings, and the radio-to-UI thread bridge
- `widgets/canvas.py` — braille drawing surface (dots, lines, circles, labels)
- `widgets/` — node table, packet feed, chat, stats, map, relays, sensors, audit,
  node detail, packet inspector

The radio layer only ever calls one `emit(kind, payload)` callback, from its own
thread; `app.py` marshals that onto the UI thread with `call_from_thread`. Adding a
BLE or MQTT transport means writing another `RadioLink` subclass and nothing else —
`TCPLink` was exactly that.

## Development

```sh
uv run python tests/smoke.py    # headless end-to-end run against the demo mesh
uv run python tests/test_crypto.py    # crypto pinned to upstream's test vectors
uv run python tests/test_meshcore.py  # MeshCore mapping, no radio needed
uv run python tests/test_admin_isolation.py  # admin input must never reach the mesh
uv run python tests/test_admin_log.py        # admin log persists, credentials never stored
uv run python tests/test_service.py          # restart, retry, expiry and ACK state
uv run python tests/test_gateway_bot.py      # local socket, DM, bot dedupe and chunking
uv run python tests/live.py 30  # connect to real hardware and report what it sees
```

`tests/smoke.py` drives the real app through Textual's headless pilot: every
keybinding, the map, the inspector, send and ack, slash commands, the payload
limit, and a database round-trip.

## License

MIT — see [LICENSE](LICENSE).
