"""Read-only browser companion backed by the gateway's Unix socket."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .gateway import GatewayLink, default_socket_path, request_gateway
from .pathcalc import PathObservation, analyze, split_sender
from .service import MeshService

log = logging.getLogger(__name__)


def companion_snapshot(service: MeshService) -> dict[str, Any]:
    """Build one atomic, JSON-safe browser snapshot."""
    with service.lock:
        state = service.state
        nodes = [{
            "id": node.node_id,
            "name": node.name,
            "short_name": node.short_name,
            "role": node.role,
            "lat": node.lat,
            "lon": node.lon,
            "snr": node.snr,
            "hops": node.hops,
            "last_heard": node.last_heard,
            "battery": node.battery,
        } for node in state.nodes.values()]
        messages = []
        for message in list(state.chat)[-250:]:
            sender, body = split_sender(message.text)
            messages.append({
                "ts": message.ts,
                "from": "you" if message.outgoing else
                        (sender or message.from_name or state.node_name(message.from_id)),
                "text": body if sender and not message.outgoing else message.text,
                "channel": state.channel_name(message.channel),
                "dm": message.is_dm,
                "outgoing": message.outgoing,
                "delivery": message.delivery_status,
                "repeats": len(message.repeated_by),
                "path_hash_size": message.path_hash_size,
                "route_mode": message.route_mode,
            })
        routes = []
        for observation in state.paths[-100:]:
            analysis = analyze(state, observation)
            points = analysis.points()
            if len(points) >= 2:
                routes.append({
                    "ts": observation.ts,
                    "origin": observation.origin_name or observation.origin_id,
                    "points": [{"lat": lat, "lon": lon, "name": name, "role": role}
                               for lat, lon, name, role in points],
                })
        return {
            "connected": state.connected,
            "protocol": state.protocol,
            "node_id": state.my_node_id,
            "node_name": state.my_node_name,
            "nodes": nodes,
            "messages": messages,
            "routes": routes,
            "channels": [{"index": index, "name": name}
                         for index, name in state.channel_pairs()],
            "airtime_1h": state.stats.airtime_last_hour(),
        }


COMPANION_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>meshtui companion</title>
<style>
:root{color-scheme:dark;--bg:#020907;--panel:#07140f;--line:#1e6b4c;--fg:#c8ffe8;
--muted:#72a88d;--green:#43f59d;--amber:#ffd75f;--cyan:#5fffd0;--red:#ff625c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px ui-monospace,
SFMono-Regular,Menlo,monospace}header{height:44px;padding:11px 16px;border-bottom:1px solid var(--line);
display:flex;gap:18px;align-items:center}header strong{color:var(--green);letter-spacing:.08em}
#status{color:var(--muted)}main{height:calc(100vh - 44px);display:grid;grid-template-columns:3fr 2fr;
gap:1px;background:var(--line)}section{background:var(--panel);min-width:0;overflow:hidden}
.title{height:34px;padding:9px 12px;color:var(--muted);border-bottom:1px solid #123c2d}
#map{width:100%;height:calc(100% - 34px);background:radial-gradient(circle at 50% 50%,#0a2118,#020907 70%)}
.route{fill:none;stroke:var(--cyan);stroke-width:2;opacity:.34}.node{fill:var(--green);stroke:#001b10;
stroke-width:2}.node.repeater{fill:var(--amber)}.node-label{fill:var(--fg);font-size:12px;paint-order:stroke;
stroke:#020907;stroke-width:4px;stroke-linejoin:round}.grid{stroke:#164332;stroke-width:1;opacity:.45}
#side{display:grid;grid-template-rows:3fr 2fr;gap:1px;background:var(--line)}#chat,#nodes{overflow:auto;
background:var(--panel)}.msg{padding:8px 12px;border-bottom:1px solid #102d23}.meta{color:var(--muted);font-size:12px}
.msg strong{color:var(--green)}.msg.out strong{color:var(--cyan)}.msg p{margin:4px 0 0;white-space:pre-wrap;
overflow-wrap:anywhere}.badge{color:var(--amber)}.node-row{display:grid;grid-template-columns:1fr auto auto;
gap:10px;padding:7px 12px;border-bottom:1px solid #102d23}.ghost{opacity:.42}.amber{color:var(--amber)}
.green{color:var(--green)}.error{color:var(--red);padding:16px}@media(max-width:850px){main{grid-template-columns:1fr;
grid-template-rows:1fr 1fr}#side{grid-template-columns:1fr 1fr;grid-template-rows:1fr}}
</style></head><body><header><strong>MESHTUI // COMPANION</strong><span id="status">connecting…</span></header>
<main><section><div class="title">READ-ONLY MAP + RECENT ROUTES</div><svg id="map" viewBox="0 0 1000 650"
role="img" aria-label="Mesh nodes and recently observed routes"></svg></section><section id="side"><div id="chat"></div>
<div id="nodes"></div></section></main><script>
const NS='http://www.w3.org/2000/svg';function el(tag,attrs={}){const n=document.createElementNS(NS,tag);
for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n}function text(tag,value,cls){const n=document.createElement(tag);
n.className=cls||'';n.textContent=value;return n}function age(s,now){if(s==null)return'never';const d=Math.max(0,now-s);
if(d<60)return Math.floor(d)+'s';if(d<3600)return Math.floor(d/60)+'m';if(d<86400)return Math.floor(d/3600)+'h';
return Math.floor(d/86400)+'d'}function renderMap(data){const svg=document.querySelector('#map');svg.replaceChildren();
for(let x=0;x<=1000;x+=100)svg.append(el('line',{x1:x,y1:0,x2:x,y2:650,class:'grid'}));
for(let y=0;y<=650;y+=100)svg.append(el('line',{x1:0,y1:y,x2:1000,y2:y,class:'grid'}));
const points=data.nodes.filter(n=>n.lat!=null&&n.lon!=null);if(!points.length){const t=el('text',{x:40,y:70,class:'node-label'});
t.textContent='No positioned nodes yet';svg.append(t);return}let minLat=Math.min(...points.map(n=>n.lat)),maxLat=Math.max(...points.map(n=>n.lat));
let minLon=Math.min(...points.map(n=>n.lon)),maxLon=Math.max(...points.map(n=>n.lon));const pad=.01;
minLat-=pad;maxLat+=pad;minLon-=pad;maxLon+=pad;const xy=p=>[50+(p.lon-minLon)/(maxLon-minLon)*900,
600-(p.lat-minLat)/(maxLat-minLat)*550];for(const route of data.routes.slice(-30)){const coords=route.points.map(p=>xy(p).join(',')).join(' ');
svg.append(el('polyline',{points:coords,class:'route'}))}for(const n of points){const[x,y]=xy(n);svg.append(el('circle',
{cx:x,cy:y,r:n.role&&/rep|router|room/i.test(n.role)?7:5,class:'node '+(n.role&&/rep|router|room/i.test(n.role)?'repeater':'')}));
const t=el('text',{x:x+9,y:y-7,class:'node-label'});t.textContent=n.name;svg.append(t)}}function render(data){const now=Date.now()/1000;
const status=document.querySelector('#status');status.textContent=(data.connected?'● ONLINE':'○ OFFLINE')+'  '+data.protocol+'  '+
data.nodes.length+' nodes'+(data.airtime_1h==null?'':'  air 1h '+data.airtime_1h.toFixed(1)+'%');renderMap(data);
const chat=document.querySelector('#chat');chat.replaceChildren(text('div','CHAT // RECENT','title'));for(const m of data.messages.slice().reverse()){
const row=text('div','',m.outgoing?'msg out':'msg');const meta=text('div','', 'meta');const who=text('strong',m.from);meta.append(who,
document.createTextNode('  '+new Date(m.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+'  '+(m.dm?'DM':(m.channel&&m.channel.startsWith('#')?m.channel:'#'+m.channel))));
if(m.path_hash_size)meta.append(text('span','  ['+m.path_hash_size+'B'+(m.route_mode==='flood'?' F':'')+']','badge'));
row.append(meta,text('p',m.text));chat.append(row)}const nodes=document.querySelector('#nodes');nodes.replaceChildren(text('div','NODES // HEALTH','title'));
for(const n of data.nodes.slice().sort((a,b)=>(b.last_heard||0)-(a.last_heard||0))){const d=n.last_heard==null?1e12:now-n.last_heard;
const row=text('div','',d>3600?'node-row ghost':'node-row');row.append(text('span',n.name),text('span',n.snr==null?'—':n.snr.toFixed(1)+'dB',
n.snr!=null&&n.snr>=0?'green':n.snr!=null&&n.snr>=-8?'amber':''),text('span',age(n.last_heard,now),d<900?'green':d<3600?'amber':''));
nodes.append(row)}}async function refresh(){try{const r=await fetch('/api/snapshot',{cache:'no-store'});if(!r.ok)throw Error(r.status);
render(await r.json())}catch(e){document.querySelector('#status').textContent='companion unavailable: '+e}}refresh();setInterval(refresh,2000);
</script></body></html>"""


class _CompanionHandler(BaseHTTPRequestHandler):
    server_version = "meshtui-companion"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        companion = self.server.companion  # type: ignore[attr-defined]
        if path in ("/", "/index.html"):
            self._reply(HTTPStatus.OK, "text/html; charset=utf-8",
                        COMPANION_HTML.encode("utf-8"))
        elif path == "/api/snapshot":
            body = json.dumps(companion_snapshot(companion.service),
                              separators=(",", ":")).encode("utf-8")
            self._reply(HTTPStatus.OK, "application/json", body)
        elif path == "/health":
            body = json.dumps({"ok": True,
                               "gateway_connected": companion.service.state.connected}).encode()
            self._reply(HTTPStatus.OK, "application/json", body)
        else:
            self._reply(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")

    def _reply(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("companion http: " + fmt, *args)


class CompanionServer:
    """Mirror gateway state into a small loopback-bound HTTP server."""

    def __init__(self, gateway_socket: str | Path | None = None,
                 host: str = "127.0.0.1", port: int = 8765) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("HTTP port must be between 0 and 65535")
        self.gateway_socket = Path(gateway_socket or default_socket_path())
        self.host = host
        self.port = port
        self.service = MeshService(None)
        self.link = GatewayLink(self.service.handle_event, self.gateway_socket)
        self.service.attach_link(self.link)
        self.http: ThreadingHTTPServer | None = None
        self._serving = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        if self.http is None:
            return self.host, self.port
        return str(self.http.server_address[0]), int(self.http.server_address[1])

    def start(self) -> None:
        if self.http is not None:
            return
        self._load_paths()
        self.link.start()
        http = ThreadingHTTPServer((self.host, self.port), _CompanionHandler)
        http.daemon_threads = True
        http.companion = self  # type: ignore[attr-defined]
        self.http = http

    def serve_forever(self) -> None:
        if self.http is None:
            raise RuntimeError("companion server has not started")
        self._serving.set()
        try:
            self.http.serve_forever(poll_interval=0.25)
        finally:
            self._serving.clear()

    def stop(self) -> None:
        if self.http is not None:
            if self._serving.is_set():
                self.http.shutdown()
            self.http.server_close()
            self.http = None
        self.link.stop()

    def _load_paths(self) -> None:
        try:
            result = request_gateway({"command": "paths", "limit": 1000},
                                     self.gateway_socket, timeout=5.0)
        except Exception:  # noqa: BLE001 - stream can connect when gateway returns
            return
        fields = {field.name for field in dataclasses.fields(PathObservation)}
        for row in result.get("paths") or []:
            if not isinstance(row, dict):
                continue
            try:
                self.service.state.note_path(PathObservation(
                    **{key: value for key, value in row.items() if key in fields}))
            except TypeError:
                continue
