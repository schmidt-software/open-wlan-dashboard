#!/usr/bin/env python3
"""Local live view of real WLAN traffic per band and device (FRITZ!Box + mesh repeater), read-only.

Combines the FRITZ!Box online monitor (measured internet traffic per device, 5 s samples)
with the mesh topology (which device is on which band / access point).
Serves a single page on http://127.0.0.1:8765; the FRITZ!Box password never leaves this process.
"""
import atexit, hashlib, json, os, re, socket, threading, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOX = "192.168.178.1"
USER = "claude"
PW = open(os.path.expanduser("~/.claude_fritz")).readline().strip()
PORT = 8765

_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
_mgr.add_password(None, f"http://{BOX}:49000", USER, PW)
_opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(_mgr))
_sid = None
_devs = {"t": 0.0, "data": {}}


def login():
    global _sid
    x = urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2", timeout=10).read().decode()
    _, i1, s1, i2, s2 = re.search(r"<Challenge>(.*?)</Challenge>", x).group(1).split("$")
    h1 = hashlib.pbkdf2_hmac("sha256", PW.encode(), bytes.fromhex(s1), int(i1))
    h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(s2), int(i2))
    body = urllib.parse.urlencode({"username": USER, "response": f"{s2}${h2.hex()}"}).encode()
    x = urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2", body, timeout=10).read().decode()
    _sid = re.search(r"<SID>(.*?)</SID>", x).group(1)
    if _sid == "0000000000000000":
        raise RuntimeError("FRITZ!Box login failed")


def logout():
    if _sid:
        try:
            urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2&logout=1&sid={_sid}", timeout=5)
        except OSError:
            pass


def api(path):
    for attempt in (0, 1):
        if not _sid:
            login()
        req = urllib.request.Request(f"http://{BOX}/api/v0/{path}", headers={"Authorization": f"AVM-SID {_sid}"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403) or attempt:
                raise
            login()  # session expired


def mesh():
    svc, act = "urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetMeshListPath"
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{act} xmlns:u="{svc}"/></s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://{BOX}:49000/upnp/control/hosts", env.encode(),
                                 {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f"{svc}#{act}"})
    path = next(el.text for el in ET.fromstring(_opener.open(req, timeout=10).read()).iter() if el.tag.endswith("MeshListPath"))
    return json.loads(urllib.request.urlopen(f"http://{BOX}:49000{path}", timeout=10).read())


def landevices():
    if time.time() - _devs["t"] > 60:
        _devs["data"], _devs["t"] = {x["UID"]: x for x in api("generic/landevice")["landevice"]}, time.time()
    return _devs["data"]


def own_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((BOX, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def mbit(v):
    return round((v or 0) * 8 / 1e6, 3)  # the monitor reports bytes per second


def last(a):
    return next((v for v in reversed(a) if v is not None), 0) if a else 0


def sample():
    m, mon, devs = mesh(), api("monitor/online/0"), landevices()
    meshed = {n["uid"] for n in m["nodes"] if n.get("is_meshed")}
    mac_of = {i["uid"]: i.get("mac_address") for n in m["nodes"] for i in n["node_interfaces"]}
    by_mac = {x["mac"]: x for x in devs.values()}
    mine = next((x["UID"] for x in devs.values() if x.get("ip") == own_ip()), None)
    traffic = {}
    for d in mon["devices"]:
        uid = d.get("landeviceUid") or (mine if d.get("type") == "own" else None)
        if uid:  # "own" is the device that runs this page; fold it into its landevice entry
            traffic[uid] = d
    other = next((d for d in mon["devices"] if d.get("type") == "other"), None)
    n = max((len(d["downstream"]) for d in mon["devices"]), default=0)
    bands = {b: {"band": b, "devices": [], "util": {}, "down": [0.0] * n, "up": [0.0] * n} for b in ("2.4", "5")}
    for node in m["nodes"]:
        if node["uid"] not in meshed:
            continue
        ap = "FRITZ!Box" if node.get("mesh_role") == "master" else "Repeater"
        for i in node["node_interfaces"]:
            if i["type"] != "WLAN" or not i["name"].startswith("AP:") or not i.get("current_channel"):
                continue
            band = bands["5" if "5G" in i["name"] else "2.4"]
            band["util"][ap] = i.get("channel_utilization")
            for l in i.get("node_links", []):
                if l["state"] != "CONNECTED" or l["node_interface_1_uid"] != i["uid"] or l["node_2_uid"] in meshed:
                    continue
                ld = by_mac.get(mac_of.get(l["node_interface_2_uid"]), {})
                t = traffic.get(ld.get("UID"))
                band["devices"].append({"name": ld.get("friendly_name") or "?", "ap": ap, "recorded": t is not None,
                                        "down": mbit(last(t["downstream"])) if t else None,
                                        "up": mbit(last(t["upstream"])) if t else None})
                if t:  # history of the band total, assuming the device stayed on this band
                    for key in ("down", "up"):
                        series = t[key + "stream"][-n:]
                        for k, v in enumerate(series):
                            band[key][n - len(series) + k] += mbit(v)
    for band in bands.values():
        band["devices"].sort(key=lambda d: (-((d["down"] or 0) + (d["up"] or 0)), d["name"]))
        band["down"] = [round(v, 3) for v in band["down"]]
        band["up"] = [round(v, 3) for v in band["up"]]
    wan = {k: mbit(sum(last(c[k + "stream"]) for c in mon["categories"])) for k in ("down", "up")}
    return {"bands": list(bands.values()), "wan": wan, "interval": mon.get("sampleInterval", 5000) / 1000,
            "other": {"down": mbit(last(other["downstream"])), "up": mbit(last(other["upstream"]))} if other else None}


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WLAN-Datenverkehr live</title><style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#667085;--grid:#e3e6ec;--dn:#1f6feb;--up:#d9730d}
@media(prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#181e26;--fg:#e8ecf2;--mut:#93a0b4;--grid:#2a323d;--dn:#6aa8ff;--up:#ffa657}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--mut);margin-bottom:14px}
.wrap{display:grid;gap:16px;grid-template-columns:repeat(2,minmax(0,1fr))}@media(max-width:760px){.wrap{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--card);border-radius:12px;padding:16px;overflow-x:auto}.card h2{font-size:16px;margin:0 0 8px}
.tiles{display:flex;gap:28px;flex-wrap:wrap;margin-bottom:6px}.tile b{font-size:30px;font-variant-numeric:tabular-nums}
.tile small{display:block;color:var(--mut)}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.foot{color:var(--mut);font-size:13px;margin:6px 0 10px}.wide{grid-column:1/-1}
svg{width:100%;height:auto;display:block}.g{stroke:var(--grid);stroke-width:1}.t{fill:var(--mut);font-size:11px}
.l{fill:none;stroke-width:2.5;stroke-linejoin:round}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font-weight:600;font-size:12px;color:var(--mut);text-align:right;padding:4px 6px;border-bottom:1px solid var(--grid)}
td{padding:5px 6px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}td small{color:var(--mut);display:block;font-size:12px}
.na{color:var(--mut)}.err{color:#d1242f}
</style></head><body><h1>WLAN-Datenverkehr live</h1><div class="sub" id="st">verbinde …</div><div class="wrap">
<div class="card" id="c24"></div><div class="card" id="c5"></div><div class="card wide" id="cw"></div></div><script>
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=v=>v==null?'–':v>=10?v.toFixed(1).replace('.',',')+' Mbit/s':v>=1?v.toFixed(2).replace('.',',')+' Mbit/s':Math.round(v*1000)+' kbit/s';
const col={down:'var(--dn)',up:'var(--up)'},lab={down:'Download',up:'Upload'};
function tiles(o){let s='<div class="tiles">';for(const k of ['down','up'])s+='<div class="tile"><small><span class="dot" style="background:'+col[k]+'"></span>'+lab[k]+'</small><b>'+fmt(o[k])+'</b></div>';return s+'</div>'}
function chart(b,title,secs,W){const H=220,L=44,B=20,pw=W-L-6,ph=H-B-6,n=b.down.length;const top=Math.max(0.1,...b.down,...b.up);
 const step=[0.02,0.05,0.1,0.25,0.5,1,2.5,5,10,25,50,100].find(x=>top/x<=4)||250,max=Math.ceil(top/step)*step;
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Verlauf Download und Upload '+title+' in Mbit/s">';
 for(let p=0;p<=max+1e-9;p+=step){const y=6+ph*(1-p/max);s+='<line class="g" x1="'+L+'" x2="'+(W-6)+'" y1="'+y+'" y2="'+y+'"/><text class="t" x="'+(L-6)+'" y="'+(y+4)+'" text-anchor="end">'+String(+p.toFixed(2)).replace('.',',')+'</text>'}
 s+='<text class="t" x="'+L+'" y="'+(H-4)+'">vor '+Math.round(n*secs/60)+' min</text><text class="t" x="'+(W-6)+'" y="'+(H-4)+'" text-anchor="end">jetzt · Mbit/s</text>';
 for(const k of ['up','down'])s+='<polyline class="l" stroke="'+col[k]+'" points="'+b[k].map((v,i)=>(L+pw*i/Math.max(1,n-1)).toFixed(1)+','+(6+ph*(1-v/max)).toFixed(1)).join(' ')+'"/>';
 return s+'</svg>'}
function card(b,title,secs,W){const u=b.util,rec=b.devices.filter(d=>d.recorded);
 const now={down:rec.reduce((a,d)=>a+d.down,0),up:rec.reduce((a,d)=>a+d.up,0)};
 let s='<h2>'+title+'</h2>'+tiles(now)+chart(b,title,secs,W);
 s+='<div class="foot">'+b.devices.length+' Geräte, davon '+rec.length+' mit Messwerten · Kanalauslastung FRITZ!Box '+(u['FRITZ!Box']??'–')+' %, Repeater '+(u['Repeater']??'–')+' %</div>';
 s+='<table><thead><tr><th>Gerät</th><th>Download</th><th>Upload</th></tr></thead><tbody>';
 for(const d of b.devices)s+='<tr><td>'+esc(d.name)+'<small>'+d.ap+'</small></td>'+(d.recorded?'<td>'+fmt(d.down)+'</td><td>'+fmt(d.up)+'</td>':'<td class="na" colspan="2">keine Aufzeichnung</td>')+'</tr>';
 return s+'</tbody></table>'}
async function tick(){const st=document.getElementById('st');try{const d=await (await fetch('/data',{cache:'no-store'})).json();if(d.error)throw new Error(d.error);
 const w=id=>Math.max(300,document.getElementById(id).clientWidth-32);
 document.getElementById('c24').innerHTML=card(d.bands.find(b=>b.band=='2.4'),'2,4 GHz',d.interval,w('c24'));
 document.getElementById('c5').innerHTML=card(d.bands.find(b=>b.band=='5'),'5 GHz',d.interval,w('c5'));
 document.getElementById('cw').innerHTML='<h2>Internetanschluss gesamt</h2>'+tiles(d.wan)+'<div class="foot">Übrige Geräte ohne eigene Aufzeichnung zusammen: Download '+(d.other?fmt(d.other.down):'–')+', Upload '+(d.other?fmt(d.other.up):'–')+'. Sie lassen sich keinem Band zuordnen.</div>';
 st.className='sub';st.textContent='Aktualisiert '+new Date().toLocaleTimeString('de-AT')+' · alle 5 Sekunden · gemessener Internet-Datenverkehr laut Online-Monitor der FRITZ!Box. Verkehr zwischen Geräten im Heimnetz ist nicht enthalten.';
 }catch(e){st.className='sub err';st.textContent='Keine Daten von der FRITZ!Box: '+e.message}}
tick();setInterval(tick,5000);
</script></body></html>"""


_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def cached_sample():
    # several open tabs must not multiply the load on the FRITZ!Box
    with _lock:
        if time.time() - _cache["t"] > 4:
            _cache["data"], _cache["t"] = sample(), time.time()
        return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            try:
                body, ctype = json.dumps(cached_sample()).encode(), "application/json"
            except Exception as e:  # keep the page alive when the box is briefly unreachable
                body, ctype = json.dumps({"error": str(e)}).encode(), "application/json"
        elif self.path == "/":
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    atexit.register(logout)
    assert sample()["bands"], "no data from FRITZ!Box"
    print(f"http://127.0.0.1:{PORT}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
