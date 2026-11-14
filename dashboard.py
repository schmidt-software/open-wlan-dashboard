#!/usr/bin/env python3
"""Local WLAN health dashboard for a FRITZ!Box mesh (box + repeater), read-only.

Shows internet line, band load, traffic, per-device link quality and recent WLAN events,
and raises alerts early: always-on devices going offline, reconnect loops, weak signals,
crowded or noisy channels, a weak repeater uplink, DSL problems. History (24 h) comes from
the files collect.py writes into this directory. Serves http://127.0.0.1:8765 (local only).

Run: python3 dashboard.py        Self-test: python3 dashboard.py --selftest
"""
import csv, datetime as dt, json, os, re, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import live  # FRITZ!Box access helpers: login, REST api, mesh, landevices, mbit, last, own_ip

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8765

# Alert thresholds - adjust to taste
UTIL_WARN, UTIL_CRIT = 50, 70                  # channel utilization in % (mean of the last 30 min)
NOISE_WARN = -85                               # dBm; values above mean a noisy channel
RSSI_WARN, RSSI_CRIT = -75, -80                # dBm; only for always-on devices
DROPS_WARN, DROPS_CRIT = 4, 10                 # disconnects per hour, roaming excluded; always-on devices
UPLINK_RSSI_WARN, UPLINK_RATE_WARN = -70, 400  # repeater uplink on 5 GHz: dBm, Mbit/s
SNR_WARN = 6.0                                 # DSL noise margin in dB
ALWAYS_ON_SHARE, ALWAYS_ON_MIN_SAMPLES = 0.9, 8
COLLECTOR_STALE_MIN = 40
ROAM_WINDOW_S = 10
ACK_FORGET_S = 600     # a confirmation is dropped once its problem has been gone this long
ACK_FILE = os.path.join(DIR, "acks.json")
# Local, git-ignored settings (contains MAC addresses), e.g. {"never_always_on": ["AA:BB:CC:DD:EE:FF"]}:
# devices listed there are never treated as always-on, so they raise no offline or weak-signal alerts.
CONFIG_FILE = os.path.join(DIR, "config.local.json")

LEVEL = {"info": 0, "warn": 1, "crit": 2}
EV = re.compile(r"^(?:\[(?P<rep>[^\]]+)\] )?WLAN-Gerät (?P<what>angemeldet|hat sich abgemeldet|wurde abgemeldet) "
                r"\((?P<band>2,4|5) GHz\)(?:, [\d.]+ Mbit/s)?, (?P<name>.+?), IP [^,]*, MAC (?P<mac>[0-9A-F:]{17})")


# ---------------------------------------------------------------- pure logic (self-tested)

def parse_event(date, clock, msg):
    m = EV.match(msg)
    if not m:
        return None
    return {"ts": dt.datetime.strptime(f"{date} {clock}", "%d.%m.%y %H:%M:%S").timestamp(),
            "mac": m["mac"], "name": m["name"], "ap": "Repeater" if m["rep"] else "FRITZ!Box",
            "band": "2.4" if m["band"] == "2,4" else "5", "up": m["what"] == "angemeldet"}


def classify(events):
    """Mark each logoff as 'roam' (re-login at another radio within the window) or 'drop'."""
    by_mac = {}
    for e in events:
        by_mac.setdefault(e["mac"], []).append(e)
    for evs in by_mac.values():
        for e in evs:
            if e["up"]:
                e["kind"] = "login"
                continue
            moved = any(o["up"] and abs(o["ts"] - e["ts"]) <= ROAM_WINDOW_S and (o["ap"], o["band"]) != (e["ap"], e["band"])
                        for o in evs)
            e["kind"] = "roam" if moved else "drop"
    return events


def is_random_mac(mac):
    return bool(int(mac[:2], 16) & 2)


def always_on(mac, link_rows, since):
    """True if a device with a fixed MAC was connected in nearly all collector samples since `since`."""
    if is_random_mac(mac):
        return False, 0.0
    samples = {r["time"] for r in link_rows if r["ts"] >= since}
    if len(samples) < ALWAYS_ON_MIN_SAMPLES:
        return False, 0.0
    share = len({r["time"] for r in link_rows if r["ts"] >= since and r["mac"] == mac}) / len(samples)
    return share >= ALWAYS_ON_SHARE, share


def device_alerts(d):
    out = []
    if d["always_on"] and not d["online"]:
        out.append(("crit", f"offline:{d['mac']}", f"{d['name']} ist offline", f"Sonst dauerhaft verbunden (in {round(d['share'] * 100)} % der Messungen der letzten 24 h)."))
    if d["always_on"] and d["drops_1h"] >= DROPS_WARN:
        lvl = "crit" if d["drops_1h"] >= DROPS_CRIT else "warn"
        out.append((lvl, f"drops:{d['mac']}", f"{d['name']} verliert ständig die Verbindung", f"{d['drops_1h']} Abbrüche in der letzten Stunde."))
    if d["always_on"] and d["online"] and d["rssi"] is not None and d["rssi"] <= RSSI_WARN:
        lvl = "crit" if d["rssi"] <= RSSI_CRIT else "warn"
        out.append((lvl, f"rssi:{d['mac']}", f"{d['name']}: schwaches Signal", f"{d['rssi']} dBm am {d['ap']} ({band_label(d['band'])})."))
    return out


def apply_acks(alerts, acks, now):
    """Mark alerts as active / acked / muted. Mutates `acks`: expired mutes, confirmations of
    problems gone for ACK_FORGET_S and confirmations of problems that got worse are removed."""
    seen = {a["id"]: a for a in alerts}
    for k, v in list(acks.items()):
        if v.get("until") and v["until"] <= now:
            del acks[k]
        elif k in seen and LEVEL[seen[k]["level"]] > LEVEL[v["level"]]:
            del acks[k]  # escalated: alarm again
        elif k in seen:
            v["last_seen"] = now
        elif v["kind"] == "ack" and now - v.get("last_seen", v["at"]) > ACK_FORGET_S:
            del acks[k]
    out = []
    for a in alerts:
        v = acks.get(a["id"])
        out.append(dict(a, state="active" if not v else "muted" if v["kind"] == "mute" else "acked",
                        since=round(v["at"] * 1000) if v else None,
                        until=round(v["until"] * 1000) if v and v.get("until") else None))
    status = max((a["level"] for a in out if a["state"] == "active"), key=LEVEL.get, default="ok")
    return out, status


def apply_device_states(devices, alerts):
    """Carry the acknowledge/mute state of alerts into the device list and re-rank it."""
    states = {a["id"]: a["state"] for a in alerts}
    out = []
    for d in devices:
        al = [dict(a, state=states.get(a["id"], "active")) for a in d["alerts"]]
        act = [a for a in al if a["state"] == "active"]
        quiet = None if act or not al else "muted" if any(a["state"] == "muted" for a in al) else "acked"
        out.append(dict(d, alerts=al, level=max((LEVEL[a["level"]] for a in act), default=-1), quiet=quiet,
                        quiet_level=max((LEVEL[a["level"]] for a in al), default=-1)))
    out.sort(key=lambda d: (-d["level"], not d["online"], d["band"], d["name"].lower()))
    return out


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {"never_always_on": set()}
    return {"never_always_on": {m.upper() for m in cfg.get("never_always_on", [])}}


def band_label(b):
    return "2,4 GHz" if b == "2.4" else "5 GHz"


# ---------------------------------------------------------------- FRITZ!Box access

def soap(ctl, svc, act):
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{act} xmlns:u="{svc}"/></s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://{live.BOX}:49000{ctl}", env.encode(),
                                 {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f"{svc}#{act}"})
    return {el.tag: el.text for el in ET.fromstring(live._opener.open(req, timeout=10).read()).iter() if el.tag.startswith("New")}


def box_log(flt):
    for _ in (0, 1):
        if not live._sid:
            live.login()
        body = urllib.parse.urlencode({"sid": live._sid, "page": "log", "lang": "de", "filter": flt, "xhr": 1}).encode()
        try:
            return json.loads(urllib.request.urlopen(f"http://{live.BOX}/data.lua", body, timeout=20).read())["data"]["log"]
        except (ValueError, KeyError):
            live.login()  # session expired: the box answers with the login page
    return []


def read_history(since):
    def ts(s):
        return dt.datetime.fromisoformat(s).timestamp()
    aps, links, events = [], [], []
    try:
        with open(os.path.join(DIR, "aps.csv")) as f:
            aps = [dict(r, ts=ts(r["time"])) for r in csv.DictReader(f) if ts(r["time"]) >= since]
        with open(os.path.join(DIR, "links.csv")) as f:
            links = [dict(r, ts=ts(r["time"])) for r in csv.DictReader(f) if r.get("mac") and ts(r["time"]) >= since]
        with open(os.path.join(DIR, "events.jsonl")) as f:
            events = [json.loads(line) for line in f]
    except FileNotFoundError:
        pass
    return aps, links, events


_slow = {"t": 0.0, "data": None}


def slow_data():
    if time.time() - _slow["t"] > 60:
        now = time.time()
        aps, links, hist_events = read_history(now - 86400)
        wlan, net = box_log("wlan"), box_log("net")
        raw = {f'{e["date"]} {e["time"]} {e["msg"]}': (e["date"], e["time"], e["msg"]) for e in hist_events}
        raw.update({f'{e["date"]} {e["time"]} {e["msg"]}': (e["date"], e["time"], e["msg"]) for e in wlan})
        events = classify([p for p in (parse_event(*v) for v in raw.values()) if p and p["ts"] >= now - 86400])
        events.sort(key=lambda e: e["ts"])
        def when(e):
            return dt.datetime.strptime(f'{e["date"]} {e["time"]}', "%d.%m.%y %H:%M:%S").timestamp()
        _slow["data"] = {
            "aps": aps, "links": links, "events": events,
            "radar": [e["msg"] for e in wlan if re.search("Radar|bevorrechtigten Nutzer", e["msg"]) and when(e) >= now - 86400],
            "net_issues": [f'{e["date"]} {e["time"]} {e["msg"]}' for e in net
                           if re.search("fehlgeschlagen|Synchronisierung beginnt|unterbrochen|nicht verfügbar", e["msg"]) and when(e) >= now - 86400],
            "dsl": soap("/upnp/control/wandslifconfig1", "urn:dslforum-org:service:WANDSLInterfaceConfig:1", "GetInfo"),
            "ip": soap("/upnp/control/wanipconnection1", "urn:dslforum-org:service:WANIPConnection:1", "GetStatusInfo"),
            "ui": soap("/upnp/control/userif", "urn:dslforum-org:service:UserInterface:1", "GetInfo"),
        }
        _slow["t"] = time.time()
    return _slow["data"]


# ---------------------------------------------------------------- state assembly

def build_state():
    now = time.time()
    cfg = load_config()
    m, mon, devs, slow = live.mesh(), live.api("monitor/online/0"), live.landevices(), slow_data()
    meshed = {n["uid"] for n in m["nodes"] if n.get("is_meshed")}
    mac_of = {i["uid"]: i.get("mac_address") for n in m["nodes"] for i in n["node_interfaces"]}
    by_mac = {x["mac"]: x for x in devs.values()}
    mine = next((x["UID"] for x in devs.values() if x.get("ip") == live.own_ip()), None)
    traffic = {}
    for d in mon["devices"]:
        uid = d.get("landeviceUid") or (mine if d.get("type") == "own" else None)
        if uid:
            traffic[uid] = d
    n = max((len(d["downstream"]) for d in mon["devices"]), default=0)
    step, t_last = mon.get("sampleInterval", 5000) / 1000, mon.get("lastSampleTime", now)
    t_axis = [round((t_last - (n - 1 - k) * step) * 1000) for k in range(n)]

    bands = {b: {"band": b, "label": band_label(b), "aps": [], "down": [0.0] * n, "up": [0.0] * n} for b in ("2.4", "5")}
    devices, uplink = {}, None
    for node in m["nodes"]:
        if node["uid"] not in meshed:
            continue
        ap = "FRITZ!Box" if node.get("mesh_role") == "master" else "Repeater"
        for i in node["node_interfaces"]:
            if i["type"] != "WLAN" or not i["name"].startswith("AP:") or not i.get("current_channel"):
                continue
            b = "5" if "5G" in i["name"] else "2.4"
            clients = 0
            for l in i.get("node_links", []):
                if l["state"] != "CONNECTED" or l["node_interface_1_uid"] != i["uid"]:
                    continue
                rssi = l["rx_rcpi"] if l.get("rx_rcpi") not in (None, 255) else None
                rate = lambda v: round(v / 1000) if v and v > 1000 else None
                if l["node_2_uid"] in meshed:  # the repeater's own uplink to the box
                    if b == "5":
                        uplink = {"rssi": rssi, "down": rate(l.get("cur_data_rate_tx")), "up": rate(l.get("cur_data_rate_rx"))}
                    continue
                clients += 1
                mac = mac_of.get(l["node_interface_2_uid"])
                ld = by_mac.get(mac, {})
                t = traffic.get(ld.get("UID"))
                devices[mac] = {"mac": mac, "name": ld.get("friendly_name") or mac, "online": True, "ap": ap, "band": b,
                                "rssi": rssi, "rate_down": rate(l.get("cur_data_rate_tx")), "rate_up": rate(l.get("cur_data_rate_rx")),
                                "recorded": t is not None,
                                "traffic_down": live.mbit(live.last(t["downstream"])) if t else None,
                                "traffic_up": live.mbit(live.last(t["upstream"])) if t else None}
                if t:
                    for key in ("down", "up"):
                        series = t[key + "stream"][-n:]
                        for k, v in enumerate(series):
                            bands[b][key][n - len(series) + k] += live.mbit(v)
            hist = [(r["ts"], int(r["utilization_pct"])) for r in slow["aps"]
                    if r["band"] == i["name"] and r["utilization_pct"] and (r["ap"] == "fritz.box") == (ap == "FRITZ!Box")]
            util = i.get("channel_utilization")
            recent = [v for ts, v in hist if ts >= now - 1800] + ([util] if util is not None else [])
            bands[b]["aps"].append({"ap": ap, "channel": i["current_channel"], "util": util, "noise": i.get("anpi"),
                                    "clients": clients, "util_avg30": round(sum(recent) / len(recent)) if recent else None,
                                    "util_hist": [[round(ts * 1000), v] for ts, v in hist] + ([[round(now * 1000), util]] if util is not None else [])})

    # devices that are normally always on but missing right now (mesh nodes' own links excluded)
    since = now - 86400
    mesh_macs = {i.get("mac_address") for n in m["nodes"] if n["uid"] in meshed for i in n["node_interfaces"]}
    for mac in {r["mac"] for r in slow["links"]} - set(devices) - mesh_macs:
        ld = by_mac.get(mac, {})
        last_row = max((r for r in slow["links"] if r["mac"] == mac), key=lambda r: r["ts"])
        devices[mac] = {"mac": mac, "name": ld.get("friendly_name") or last_row["device"] or mac, "online": False,
                        "ap": "Repeater" if "repeater" in last_row["ap"] else "FRITZ!Box", "band": "5" if "5G" in last_row["band"] else "2.4",
                        "rssi": None, "rate_down": None, "rate_up": None, "recorded": False, "traffic_down": None, "traffic_up": None,
                        "last_seen": round(last_row["ts"] * 1000)}
    for d in devices.values():
        d["always_on"], d["share"] = always_on(d["mac"], slow["links"], since)
        if d["mac"] in cfg["never_always_on"]:
            d["always_on"] = False
        evs = [e for e in slow["events"] if e["mac"] == d["mac"]]
        d["drops_1h"] = sum(1 for e in evs if e["kind"] == "drop" and e["ts"] >= now - 3600)
        d["drops_24h"] = sum(1 for e in evs if e["kind"] == "drop")
        d["roams_24h"] = sum(1 for e in evs if e["kind"] == "roam")
        d["alerts"] = [{"id": i, "level": lv, "title": ti, "detail": de} for lv, i, ti, de in device_alerts(d)]
        d["level"] = max((LEVEL[a["level"]] for a in d["alerts"]), default=-1)
    devices = [d for d in devices.values() if d["online"] or d["always_on"]]  # skip offline phones etc.

    alerts = [a for d in devices for a in d["alerts"]]
    for b in bands.values():
        for a in b["aps"]:
            if a["util_avg30"] is not None and a["util_avg30"] >= UTIL_WARN:
                alerts.append({"id": f"util:{b['band']}:{a['ap']}", "level": "crit" if a["util_avg30"] >= UTIL_CRIT else "warn",
                               "title": f"{b['label']} am {a['ap']} stark ausgelastet",
                               "detail": f"Kanal {a['channel']} im Mittel der letzten 30 min zu {a['util_avg30']} % belegt. Geräte mit viel Datenverkehr auf 5 GHz umziehen."})
            if a["noise"] is not None and a["noise"] > NOISE_WARN:
                alerts.append({"id": f"noise:{b['band']}:{a['ap']}", "level": "warn", "title": f"Störungen auf {b['label']} am {a['ap']}",
                               "detail": f"Rauschen {a['noise']} dBm auf Kanal {a['channel']}. Möglicherweise Nachbarnetze oder Störquellen; anderen Kanal prüfen."})
    if uplink is None:
        alerts.append({"id": "uplink", "level": "crit", "title": "Repeater nicht über 5 GHz angebunden", "detail": "Der Repeater hat keine 5-GHz-Verbindung zur FRITZ!Box."})
    elif (uplink["rssi"] is not None and uplink["rssi"] < UPLINK_RSSI_WARN) or (uplink["down"] or 0) < UPLINK_RATE_WARN:
        alerts.append({"id": "uplink", "level": "warn", "title": "Schwache Anbindung des Repeaters",
                       "detail": f"Signal {uplink['rssi']} dBm, {uplink['down']} Mbit/s zur FRITZ!Box."})
    dsl, ip = slow["dsl"], slow["ip"]
    if dsl.get("NewStatus") != "Up":
        alerts.append({"id": "dsl", "level": "crit", "title": "DSL-Leitung nicht synchron", "detail": f"Status: {dsl.get('NewStatus')}."})
    if ip.get("NewConnectionStatus") != "Connected":
        alerts.append({"id": "inet", "level": "crit", "title": "Keine Internetverbindung", "detail": f"Status: {ip.get('NewConnectionStatus')}, letzter Fehler {ip.get('NewLastConnectionError')}."})
    snr = min(int(dsl.get("NewDownstreamNoiseMargin") or 0), int(dsl.get("NewUpstreamNoiseMargin") or 0)) / 10
    if dsl.get("NewStatus") == "Up" and snr < SNR_WARN:
        alerts.append({"id": "snr", "level": "warn", "title": "Geringer Störabstand der DSL-Leitung", "detail": f"{snr:.1f} dB. Die Leitung kann bei Störungen abbrechen."})
    for line in slow["net_issues"][:3]:
        alerts.append({"id": f"netlog:{line[:13]}", "level": "warn", "title": "Internetunterbrechung in den letzten 24 h", "detail": line})
    if slow["radar"]:
        alerts.append({"id": "radar", "level": "warn", "title": "5-GHz-Funk wegen Radar unterbrochen", "detail": slow["radar"][0]})
    last_sample = max((r["ts"] for r in slow["aps"]), default=0)
    if now - last_sample > COLLECTOR_STALE_MIN * 60:
        alerts.append({"id": "collector", "level": "warn", "title": "Verlauf wird nicht aufgezeichnet",
                       "detail": "Der Sammeldienst (collect.py) hat seit über 40 Minuten nichts gespeichert. Ist der Mac im Ruhezustand?"})
    if slow["ui"].get("NewUpgradeAvailable") == "1":
        alerts.append({"id": "update", "level": "info", "title": "FRITZ!OS-Update verfügbar", "detail": f"Version {slow['ui'].get('NewX_AVM-DE_Version')}."})
    alerts.sort(key=lambda a: -LEVEL[a["level"]])

    names = {d["mac"]: d["name"] for d in devices}
    names.update({x["mac"]: x.get("friendly_name") for x in devs.values() if x.get("friendly_name")})
    wan = {k: live.mbit(sum(live.last(c[k + "stream"]) for c in mon["categories"])) for k in ("down", "up")}
    return {
        "time": round(now * 1000), "status": max((a["level"] for a in alerts), key=LEVEL.get, default="ok"),
        "alerts": alerts, "t_axis": t_axis, "bands": list(bands.values()), "uplink": uplink,
        "internet": {"connected": ip.get("NewConnectionStatus") == "Connected", "uptime_s": int(ip.get("NewUptime") or 0),
                     "dsl_up": dsl.get("NewStatus") == "Up", "sync_down": int(dsl.get("NewDownstreamCurrRate") or 0) / 1000,
                     "sync_up": int(dsl.get("NewUpstreamCurrRate") or 0) / 1000, "snr": snr, "traffic": wan},
        "devices": sorted(devices, key=lambda d: (-d["level"], not d["online"], d["band"], d["name"].lower())),
        "events": [{"ts": round(e["ts"] * 1000), "name": names.get(e["mac"]) or e["name"], "ap": e["ap"], "band": band_label(e["band"]),
                    "kind": e["kind"]} for e in slow["events"][-25:][::-1]],
        "collector_age_min": round((now - last_sample) / 60) if last_sample else None,
    }


# ---------------------------------------------------------------- web page

PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WLAN-Überwachung</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Antonio:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#000;--panel:#0a0911;--o1:#ff9c00;--o2:#ffcc66;--pe:#ff9f80;--li:#cc99ff;--pw:#9999ff;--sk:#99ccff;--tan:#d6a77a;
 --tx:#ffdcae;--tx2:#c9b49a;--mut:#8f8376;--grid:#241d2d;--axis:#3d3249;
 --s1:#5b7cf5;--s2:#e06a1f;--good:#33cc66;--warn:#ffb300;--crit:#ff3b3b;--info:#99ccff;
 --font:'Antonio','Arial Narrow','Roboto Condensed','Helvetica Neue',sans-serif}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0;padding:10px;background:var(--bg);color:var(--tx);font:17px/1.35 var(--font);letter-spacing:.02em}
a{color:inherit;text-decoration:none}
.frame{display:grid;grid-template-columns:170px minmax(0,1fr);min-height:calc(100vh - 20px)}
.rail{display:flex;flex-direction:column;gap:6px}
.elbow{background:var(--o1);color:#000;font-weight:700;font-size:15px;letter-spacing:.12em;display:flex;justify-content:flex-end;padding:10px 14px}
.elbow.t{border-top-left-radius:70px;min-height:118px;align-items:flex-end}
.elbow.b{border-bottom-left-radius:70px;min-height:90px;align-items:flex-start}
.pill{display:flex;flex-direction:column;align-items:flex-end;justify-content:flex-end;padding:6px 14px;color:#000;text-transform:uppercase;min-height:70px;font-size:13px;font-weight:600;letter-spacing:.1em;line-height:1.1;transition:filter .15s}
.pill b{font-size:26px;letter-spacing:.02em;font-weight:700}.pill:hover,.pill:focus-visible{filter:brightness(1.2);outline:none}
.fill{flex:1;min-height:40px;background:var(--o2)}
.main{display:flex;flex-direction:column;min-width:0}
.bar{display:flex;gap:6px;height:24px;flex:none}.bar i{display:block;height:100%}
.inner{position:relative;flex:1;padding:16px 6px 12px 30px}
.inner::before,.inner::after{content:"";position:absolute;left:0;width:44px;height:44px}
.inner::before{top:0;background:radial-gradient(circle at 100% 100%,transparent 43px,var(--o1) 44px)}
.inner::after{bottom:0;background:radial-gradient(circle at 100% 0,transparent 43px,var(--o1) 44px)}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:46px;line-height:1;color:var(--o1);font-weight:700;letter-spacing:.06em;text-transform:uppercase;text-align:right}
.sub{color:var(--tx2);font-size:15px;letter-spacing:.12em;text-transform:uppercase;margin-top:6px}
.alarm{display:inline-block;padding:5px 22px;border-radius:22px;color:#000;font-weight:700;letter-spacing:.16em;font-size:20px;text-transform:uppercase;background:var(--c)}
.alarm.crit{animation:blink 1.6s steps(1) infinite}@keyframes blink{50%{opacity:.4}}
@media (prefers-reduced-motion:reduce){.alarm.crit{animation:none}}
.hb{display:flex;align-items:center;gap:12px;margin:22px 0 10px;text-transform:uppercase;letter-spacing:.14em;font-weight:600;font-size:19px;color:var(--c)}
.hb::before{content:"";flex:none;width:30px;height:16px;border-radius:8px 0 0 8px;background:var(--c)}
.hb::after{content:"";flex:1;height:16px;border-radius:0 8px 8px 0;background:var(--c)}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.kpi{border-left:16px solid var(--c);border-radius:24px 0 0 24px;background:var(--panel);padding:8px 14px 10px;min-width:0}
.kpi h3{margin:0;font-size:13px;letter-spacing:.16em;color:var(--c);text-transform:uppercase;font-weight:600}
.big{font-size:40px;font-weight:700;color:var(--tx);line-height:1.05;font-variant-numeric:tabular-nums}.big small{font-size:20px;color:var(--tx2)}
.sm{font-size:15px;color:var(--tx2)}
.alert{display:flex;align-items:stretch;gap:12px;margin:6px 0;background:var(--panel);border-radius:18px}
.alert .tag{flex:none;width:118px;border-radius:18px 0 0 18px;display:flex;align-items:center;justify-content:center;gap:6px;color:#000;font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:15px;background:var(--c)}
.alert .txt{padding:7px 0;flex:1}.alert b{display:block;color:var(--tx);font-size:19px;font-weight:600}.alert span{color:var(--tx2);font-size:16px}
.alert .end{flex:none;width:18px;border-radius:0 18px 18px 0;background:var(--c)}
.acts{display:flex;gap:6px;align-items:center;padding:6px 4px;flex-wrap:wrap;justify-content:flex-end}
.btn{font:600 14px var(--font);letter-spacing:.1em;text-transform:uppercase;border:0;border-radius:15px;padding:6px 14px;color:#000;background:var(--b);cursor:pointer}
.btn:hover,.btn:focus-visible{filter:brightness(1.2);outline:2px solid var(--tx);outline-offset:1px}.btn:disabled{opacity:.5;cursor:wait}
.alert.acked{opacity:.55}.allbar{display:flex;justify-content:flex-end;margin:0 0 6px}
details.muted{margin-top:10px}details.muted summary{cursor:pointer;color:var(--tx2);letter-spacing:.14em;text-transform:uppercase;font-size:15px;padding:4px 0}
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}
.panel h4{margin:6px 0 2px;font-size:14px;letter-spacing:.14em;color:var(--tx2);text-transform:uppercase;font-weight:600}
.legend{display:flex;gap:18px;font-size:14px;color:var(--tx2);margin:2px 0 4px;letter-spacing:.06em}.sw{display:inline-block;width:16px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}.gl{stroke:var(--grid);stroke-width:1}.ax{stroke:var(--axis);stroke-width:1}
.tl{fill:var(--mut);font:12px var(--font);letter-spacing:.06em}.ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}.th{stroke:var(--warn);stroke-width:1.5;stroke-dasharray:5 5}
.tip{position:absolute;pointer-events:none;background:#000;border:2px solid var(--o1);border-radius:12px;padding:6px 10px;font-size:14px;white-space:nowrap;display:none;z-index:2}
.tip i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0 4px;font-variant-numeric:tabular-nums;font-size:17px}
th{color:var(--li);font-weight:600;text-transform:uppercase;letter-spacing:.12em;font-size:13px;text-align:right;padding:2px 12px;white-space:nowrap}
td{background:var(--panel);padding:5px 12px;text-align:right;white-space:nowrap}
th.l,td.l{text-align:left}td.l{white-space:normal}
td:first-child{border-radius:16px 0 0 16px;border-left:12px solid var(--c)}td:last-child{border-radius:0 16px 16px 0}tr.quiet td{opacity:.6}
.st{font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:14px;color:var(--c)}
.mut{color:var(--mut)}.note{font-size:14px;color:var(--mut);margin-top:6px;letter-spacing:.03em}
.log{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:2px 14px;font-size:16px}
.log div{padding:3px 0;border-bottom:1px solid var(--grid)}.log .t{color:var(--sk);font-variant-numeric:tabular-nums}
.err{color:var(--crit);font-weight:600}
@media(max-width:1200px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.frame{grid-template-columns:minmax(0,1fr)}.rail{display:none}.inner{padding-left:6px}.inner::before,.inner::after{display:none}.cols{grid-template-columns:minmax(0,1fr)}h1{font-size:34px}}
@media(max-width:560px){.kpis{grid-template-columns:minmax(0,1fr)}.alert .tag{width:84px}}
</style></head><body>
<div class="frame">
 <nav class="rail" aria-label="Bereiche">
  <div class="elbow t">WLAN 01</div>
  <a class="pill" id="r-status" href="#s-alerts" style="background:var(--good)"><span>Status</span><b>–</b></a>
  <a class="pill" id="r-inet" href="#s-kpi" style="background:var(--pe)"><span>DSL Mbit/s</span><b>–</b></a>
  <a class="pill" id="r-24" href="#s-24" style="background:var(--li)"><span>2,4 GHz</span><b>–</b></a>
  <a class="pill" id="r-5" href="#s-5" style="background:var(--pw)"><span>5 GHz</span><b>–</b></a>
  <a class="pill" id="r-dev" href="#s-dev" style="background:var(--sk)"><span>Geräte</span><b>–</b></a>
  <a class="pill" id="r-log" href="#s-log" style="background:var(--tan)"><span>Protokoll</span><b>–</b></a>
  <div class="fill"></div>
  <div class="elbow b">FRITZ!BOX</div>
 </nav>
 <div class="main">
  <div class="bar" aria-hidden="true"><i style="flex:5;background:var(--o1)"></i><i style="width:70px;background:var(--li)"></i><i style="width:150px;background:var(--pw)"></i><i style="flex:1;background:var(--o2)"></i><i style="width:46px;background:var(--sk);border-radius:0 12px 12px 0"></i></div>
  <div class="inner">
   <header><div><span class="alarm" id="alarm" style="--c:var(--good)">Lade …</span><div class="sub" id="upd">Verbinde mit der FRITZ!Box …</div></div>
    <div><h1>WLAN-Überwachung</h1><div class="sub" style="text-align:right" id="sd"></div></div></header>
   <section id="s-alerts"><h2 class="hb" style="--c:var(--o1)">Hinweise</h2><div id="alerts" aria-live="polite"></div></section>
   <section id="s-kpi"><h2 class="hb" style="--c:var(--pe)">Systeme</h2><div class="kpis" id="kpis"></div></section>
   <div class="cols">
    <section id="s-24" class="panel"></section>
    <section id="s-5" class="panel"></section>
   </div>
   <section id="s-dev"><h2 class="hb" style="--c:var(--sk)">Geräte</h2><div class="wrap" id="devs"></div></section>
   <section id="s-log"><h2 class="hb" style="--c:var(--tan)">Protokoll</h2><div id="evs"></div></section>
  </div>
  <div class="bar" aria-hidden="true"><i style="flex:3;background:var(--o1)"></i><i style="width:110px;background:var(--pe)"></i><i style="flex:1;background:var(--li)"></i><i style="width:60px;background:var(--o2);border-radius:0 12px 12px 0"></i></div>
 </div>
</div>
<script>
const $=id=>document.getElementById(id),esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const LV={crit:['!','Rot','var(--crit)','Roter Alarm'],warn:['!','Gelb','var(--warn)','Gelber Alarm'],info:['i','Info','var(--info)','Hinweis'],ok:['✓','OK','var(--good)','Alle Systeme nominal']};
const num=(v,d=0)=>v==null?'–':v.toLocaleString('de-AT',{maximumFractionDigits:d,minimumFractionDigits:d});
const rate=v=>v==null?'–':v>=10?num(v,1)+' Mbit/s':v>=1?num(v,2)+' Mbit/s':num(v*1000)+' kbit/s';
const hm=t=>new Date(t).toLocaleTimeString('de-AT',{hour:'2-digit',minute:'2-digit'});
const hms=t=>new Date(t).toLocaleTimeString('de-AT');
const dur=s=>{const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+' T '+h+' h':h?h+' h '+m+' min':m+' min'};
const stardate=t=>{const d=new Date(t),y=d.getFullYear(),s=new Date(y,0,1),e=new Date(y+1,0,1);return num((y-2000)*1000+1000*(d-s)/(e-s),1)};

function lineChart(el,series,o){const W=Math.max(280,el.clientWidth||600),H=o.h||180,L=44,R=8,T=8,B=22,pw=W-L-R,ph=H-T-B;
 const t0=o.t0,t1=o.t1,all=series.flatMap(s=>s.pts.map(p=>p[1]));let max=o.max??Math.max(o.minMax||1,...all);
 if(o.max==null){const st=[0.025,0.05,0.1,0.25,0.5,1,2,5,10,20,25,50,100,200,500].find(x=>max/x<=4)||1000;max=4*st}
 const X=t=>L+pw*(t-t0)/Math.max(1,t1-t0),Y=v=>T+ph*(1-v/max);
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(o.aria)+'">';
 for(let k=0;k<=4;k++){const v=max*k/4,y=Y(v);s+='<line class="'+(k?'gl':'ax')+'" x1="'+L+'" x2="'+(W-R)+'" y1="'+y+'" y2="'+y+'"/><text class="tl" x="'+(L-6)+'" y="'+(y+4)+'" text-anchor="end">'+o.fmtAxis(v)+'</text>'}
 if(o.thr!=null)s+='<line class="th" x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(o.thr)+'" y2="'+Y(o.thr)+'"/>';
 o.ticks.forEach((t,k)=>{const a=k==0?'start':k==o.ticks.length-1?'end':'middle';s+='<text class="tl" x="'+X(t)+'" y="'+(H-4)+'" text-anchor="'+a+'">'+hm(t)+'</text>'});
 for(const se of series){if(!se.pts.length)continue;s+='<polyline class="ln" stroke="'+se.color+'" points="'+se.pts.map(p=>X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join(' ')+'"/>';
  if(se.pts.length<3)for(const p of se.pts)s+='<circle cx="'+X(p[0])+'" cy="'+Y(p[1])+'" r="4" fill="'+se.color+'" stroke="#000" stroke-width="2"/>'}
 s+='<line class="xh ax" y1="'+T+'" y2="'+(T+ph)+'" style="display:none;stroke:var(--o1)"/><rect x="'+L+'" y="'+T+'" width="'+pw+'" height="'+ph+'" fill="transparent"/></svg><div class="tip"></div>';
 el.innerHTML=s;const svg=el.querySelector('svg'),tip=el.querySelector('.tip'),xh=svg.querySelector('.xh');
 svg.onmousemove=e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width;if(px<L){svg.onmouseleave();return}
  const t=t0+(px-L)/pw*(t1-t0);let rows=[],best=null;
  for(const se of series){let p=null,d=1e18;for(const q of se.pts){const dd=Math.abs(q[0]-t);if(dd<d){d=dd;p=q}}if(p&&d<=(o.snap||1e18)){rows.push('<i style="background:'+se.color+'"></i>'+esc(se.label)+': <b>'+o.fmt(p[1])+'</b>');best=best??p[0]}}
  if(best==null){svg.onmouseleave();return}xh.setAttribute('x1',X(best));xh.setAttribute('x2',X(best));xh.style.display='';
  tip.innerHTML='<div class="mut">'+(o.long?hm(best):hms(best))+'</div>'+rows.join('<br>');tip.style.display='block';
  const lx=X(best)*r.width/W;tip.style.left=Math.min(lx+12,r.width-tip.offsetWidth-4)+'px';tip.style.top='6px'};
 svg.onmouseleave=()=>{tip.style.display='none';xh.style.display='none'}}
const legend=items=>'<div class="legend">'+items.map(i=>'<span><span class="sw" style="background:'+i[1]+'"></span>'+esc(i[0])+'</span>').join('')+'</div>';
const pill=(id,val)=>{$(id).querySelector('b').textContent=val};

function render(d){
 const st=LV[d.status],nAl=d.alerts.filter(a=>a.level==d.status).length;
 $('alarm').className='alarm'+(d.status=='crit'?' crit':'');$('alarm').style.setProperty('--c',st[2]);
 const nAck=d.alerts.filter(a=>a.state=='acked').length,nMut=d.alerts.filter(a=>a.state=='muted').length;
 $('alarm').textContent=st[3]+(d.status!='ok'&&nAl>1?' · '+nAl:'')+(nAck?' · '+nAck+' bestätigt':'')+(nMut?' · '+nMut+' stumm':'');
 $('upd').textContent='Aktualisiert '+hms(d.time)+' · Intervall 5 s'+(d.collector_age_min!=null?' · Verlauf vor '+d.collector_age_min+' min gesichert':'');
 $('sd').textContent='Sternzeit '+stardate(d.time)+' · FRITZ!Box-Mesh';
 $('r-status').style.background=st[2];pill('r-status',st[1]);
 const i=d.internet,u=d.uplink,ap=(b,n)=>b.aps.find(a=>a.ap==n)||{},maxU=b=>Math.max(...b.aps.map(a=>a.util??0));
 pill('r-inet',num(i.sync_down,1));pill('r-24',num(maxU(d.bands[0]))+' %');pill('r-5',num(maxU(d.bands[1]))+' %');
 pill('r-dev',d.devices.filter(x=>x.online).length);pill('r-log',d.events.length?hm(d.events[0].ts):'–');
 renderAlerts(d.alerts);
 const kpi=(c,h,big,sm)=>'<div class="kpi" style="--c:'+c+'"><h3>'+h+'</h3><div class="big">'+big+'</div><div class="sm">'+sm+'</div></div>';
 const bandK=(b,c)=>{const f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');return kpi(c,b.label+' · Auslastung',num(f.util)+' % <small>/ '+num(r.util)+' %</small>','Box / Repeater · '+((f.clients||0)+(r.clients||0))+' Geräte · Kanal '+(f.channel??'–'))};
 $('kpis').innerHTML=kpi('var(--pe)','Internet',i.connected&&i.dsl_up?'Verbunden':'Getrennt','seit '+dur(i.uptime_s)+' · DSL '+num(i.sync_down,1)+' / '+num(i.sync_up,1)+' Mbit/s · Störabstand '+num(i.snr,1)+' dB')
  +kpi('var(--o2)','Datenverkehr','↓ '+rate(i.traffic.down),'↑ '+rate(i.traffic.up)+' · gesamter Anschluss')
  +bandK(d.bands[0],'var(--li)')+bandK(d.bands[1],'var(--pw)')
  +kpi('var(--sk)','Repeater-Anbindung',u?num(u.down)+' <small>Mbit/s</small>':'Getrennt',u?'Signal '+num(u.rssi)+' dBm · 5 GHz zur FRITZ!Box':'keine 5-GHz-Verbindung');
 for(const b of d.bands){const id=b.band=='2.4'?'s-24':'s-5',c=b.band=='2.4'?'var(--li)':'var(--pw)',f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');
  $(id).innerHTML='<h2 class="hb" style="--c:'+c+'">Funk '+b.label+'</h2><h4>Kanalauslastung · 24 Stunden</h4>'+legend([['FRITZ!Box','var(--s1)'],['Repeater','var(--s2)'],['Warnschwelle '+d.thr_util+' %','var(--warn)']])+'<div class="chart" id="u'+b.band+'"></div>'
   +'<h4 style="margin-top:12px">Internet-Datenverkehr · 10 Minuten</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="t'+b.band+'"></div>'
   +'<div class="note">Rauschen FRITZ!Box '+num(f.noise)+' dBm · Repeater '+num(r.noise)+' dBm</div>';
  const t1=d.time,t0=t1-86400000,tk=[0,6,12,18,24].map(h=>t0+h*3600000);
  lineChart($('u'+b.band),[{label:'FRITZ!Box',color:'var(--s1)',pts:f.util_hist||[]},{label:'Repeater',color:'var(--s2)',pts:r.util_hist||[]}],
   {t0,t1,ticks:tk,max:100,thr:d.thr_util,fmt:v=>v+' %',fmtAxis:v=>v+' %',long:true,snap:1800000,aria:b.label+': Kanalauslastung der letzten 24 Stunden in Prozent'});
  const ta=d.t_axis,z=k=>ta.map((t,j)=>[t,b[k][j]]);
  lineChart($('t'+b.band),[{label:'Download',color:'var(--s1)',pts:z('down')},{label:'Upload',color:'var(--s2)',pts:z('up')}],
   {t0:ta[0],t1:ta[ta.length-1],ticks:[ta[0],ta[Math.floor(ta.length/2)],ta[ta.length-1]],minMax:0.1,fmt:rate,fmtAxis:v=>v&&v<1?num(v*1000)+' k':num(v),aria:b.label+': Datenverkehr der letzten 10 Minuten in Mbit/s'})}
 const sb=x=>x.level==2?LV.crit:x.level==1?LV.warn:x.quiet?['',x.quiet=='muted'?'Stumm':'Bestätigt',x.quiet_level==2?'var(--crit)':x.quiet_level==1?'var(--warn)':'var(--info)']:x.online?LV.ok:['','Offline','var(--mut)'];
 $('devs').innerHTML='<table><thead><tr><th class="l">Status</th><th class="l">Gerät</th><th class="l">Verbindung</th><th>Signal</th><th>Rate ↓ / ↑</th><th>Verkehr ↓ / ↑</th><th>Abbrüche 1 h</th><th>Abbrüche 24 h</th><th>Wechsel 24 h</th></tr></thead><tbody>'
  +d.devices.map(x=>{const s=sb(x);return '<tr'+(x.quiet?' class="quiet" title="Alarm '+(x.quiet=='muted'?'stummgeschaltet':'bestätigt')+'"':'')+' style="--c:'+s[2]+'"><td class="l"><span class="st">'+s[1]+'</span></td>'
   +'<td class="l">'+esc(x.name)+(x.always_on?' <span class="mut" title="in fast allen Messungen der letzten 24 h verbunden">· dauerhaft</span>':'')+'</td>'
   +'<td class="l">'+(x.online?(x.band=='2.4'?'2,4':'5')+' GHz · '+x.ap:'zuletzt '+(x.last_seen?hm(x.last_seen):'–'))+'</td>'
   +'<td>'+(x.rssi==null?'–':x.rssi+' dBm')+'</td><td>'+num(x.rate_down)+' / '+num(x.rate_up)+'</td>'
   +'<td>'+(x.recorded?rate(x.traffic_down)+' / '+rate(x.traffic_up):'<span class="mut">nicht aufgezeichnet</span>')+'</td>'
   +'<td>'+x.drops_1h+'</td><td>'+x.drops_24h+'</td><td>'+x.roams_24h+'</td></tr>'}).join('')+'</tbody></table>'
  +'<div class="note">Rate = ausgehandeltes Tempo in Mbit/s (der Repeater meldet keine Upload-Rate). Abbrüche zählen Abmeldungen ohne Wechsel zu einem anderen Zugangspunkt; Wechsel zwischen FRITZ!Box und Repeater sind normal.</div>';
 const kind={login:'angemeldet',drop:'abgemeldet',roam:'gewechselt'};
 $('evs').innerHTML='<div class="log">'+d.events.map(e=>'<div class="t">'+hm(e.ts)+'</div><div>'+esc(e.name)+' '+kind[e.kind]+'</div><div class="mut">'+e.band+' · '+e.ap+'</div>').join('')+'</div>'}

let lastAlerts='',mutedOpen=false;
const btn=(lbl,act,ids,c,h)=>'<button class="btn" type="button" style="--b:'+c+'" data-act="'+act+'" data-ids="'+esc(JSON.stringify(ids))+'"'+(h?' data-h="'+h+'"':'')+'>'+lbl+'</button>';
const until=t=>new Date(t).toLocaleString('de-AT',new Date(t).toDateString()==new Date().toDateString()?{hour:'2-digit',minute:'2-digit'}:{weekday:'short',hour:'2-digit',minute:'2-digit'});
function renderAlerts(all){const key=JSON.stringify(all);if(key==lastAlerts)return;lastAlerts=key;
 const act=all.filter(a=>a.state=='active'),ack=all.filter(a=>a.state=='acked'),mut=all.filter(a=>a.state=='muted');
 const row=(a,extra,acts)=>{const l=LV[a.level];return '<div class="alert '+(a.state||'')+'" style="--c:'+l[2]+'"><div class="tag"><span aria-hidden="true">'+l[0]+'</span>'+l[1]+'</div><div class="txt"><b>'+esc(a.title)+'</b><span>'+esc(a.detail)+extra+'</span></div>'+(acts?'<div class="acts">'+acts+'</div>':'')+'<div class="end"></div></div>'};
 let h=act.length>1?'<div class="allbar">'+btn('Alle bestätigen','ack',act.map(a=>a.id),'var(--o2)')+'</div>':'';
 h+=act.map(a=>row(a,'',btn('Bestätigen','ack',[a.id],'var(--o2)')+btn('Stumm 1 h','mute',[a.id],'var(--pw)',1)+btn('Stumm 24 h','mute',[a.id],'var(--li)',24))).join('');
 if(!act.length)h+=row({level:'ok',title:'Keine aktiven Alarme',detail:ack.length||mut.length?'Alle offenen Hinweise sind bestätigt oder stummgeschaltet.':'Alle Prüfungen ohne Befund.'},'','');
 h+=ack.map(a=>row(a,' · Bestätigt um '+hm(a.since),btn('Zurücknehmen','clear',[a.id],'var(--tan)'))).join('');
 if(mut.length)h+='<details class="muted"'+(mutedOpen?' open':'')+'><summary>Stummgeschaltet: '+mut.length+'</summary>'+mut.map(a=>row(a,' · stumm bis '+until(a.until),btn('Aufheben','clear',[a.id],'var(--tan)'))).join('')+'</details>';
 $('alerts').innerHTML=h}
$('alerts').addEventListener('click',async e=>{const b=e.target.closest('button[data-act]');if(!b)return;b.disabled=true;
 try{const r=await fetch('/ack',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard':'1'},body:JSON.stringify({ids:JSON.parse(b.dataset.ids),action:b.dataset.act,hours:+b.dataset.h||0})});if(!r.ok)throw new Error('HTTP '+r.status)}
 catch(err){$('upd').innerHTML='<span class="err">Aktion fehlgeschlagen: '+esc(err.message)+'</span>'}lastAlerts='';tick()});
document.addEventListener('toggle',e=>{if(e.target.classList&&e.target.classList.contains('muted'))mutedOpen=e.target.open},true);
let last=null;
async function tick(){try{const r=await fetch('/data',{cache:'no-store'}),d=await r.json();if(d.error)throw new Error(d.error);last=d;render(d)}
 catch(e){$('upd').innerHTML='<span class="err">Keine Daten von der FRITZ!Box: '+esc(e.message)+'</span>'}}
addEventListener('resize',()=>last&&render(last));tick();setInterval(tick,5000);
</script></body></html>"""


_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def cached_state():
    with _lock:  # several open tabs must not multiply the load on the FRITZ!Box
        if time.time() - _cache["t"] > 4:
            data = build_state()
            data["thr_util"] = UTIL_WARN
            _cache["data"], _cache["t"] = data, time.time()
        return _cache["data"]


_ack_lock = threading.Lock()


def load_acks():
    try:
        with open(ACK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_acks(acks):
    tmp = ACK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(acks, f, indent=1)
    os.replace(tmp, ACK_FILE)


def current_state():
    state = cached_state()
    with _ack_lock:
        acks = load_acks()
        before = json.dumps(acks, sort_keys=True)
        alerts, status = apply_acks(state["alerts"], acks, time.time())
        if json.dumps(acks, sort_keys=True) != before:
            save_acks(acks)
    return dict(state, alerts=alerts, status=status, devices=apply_device_states(state["devices"], alerts))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # a custom header forces a CORS preflight, which this server never answers: other websites cannot post here
        if self.path != "/ack" or self.headers.get("X-Dashboard") != "1":
            self.send_error(403)
            return
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10000)))
            ids, action = req["ids"], req["action"]
            assert isinstance(ids, list) and action in ("ack", "mute", "clear")
            hours = float(req.get("hours") or 0)
            assert action != "mute" or 0 < hours <= 168
        except (ValueError, KeyError, TypeError, AssertionError):
            self.send_error(400)
            return
        now, levels = time.time(), {a["id"]: a["level"] for a in (_cache["data"] or {}).get("alerts", [])}
        with _ack_lock:
            acks = load_acks()
            for i in ids:
                if action == "clear":
                    acks.pop(i, None)
                elif i in levels:  # only alerts that currently exist can be confirmed or muted
                    acks[i] = {"kind": action, "level": levels[i], "at": now, "last_seen": now,
                               "until": now + hours * 3600 if action == "mute" else None}
            save_acks(acks)
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path == "/data":
            try:
                body = json.dumps(current_state()).encode()
            except Exception as e:  # keep the page alive when the box is briefly unreachable
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            ctype = "application/json"
        elif self.path == "/":
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def selftest():
    ev = [parse_event("13.11.26", "22:00:00", "WLAN-Gerät wurde abgemeldet (5 GHz), iPhone, IP 192.168.178.118, MAC 02:00:00:00:00:01."),
          parse_event("13.11.26", "22:00:01", "[fritzrepeaterschlafzimmer] WLAN-Gerät angemeldet (5 GHz), 1201 Mbit/s, PC-02, IP 192.168.178.118, MAC 02:00:00:00:00:01."),
          parse_event("13.11.26", "22:10:00", "[fritzrepeaterschlafzimmer] WLAN-Gerät hat sich abgemeldet (2,4 GHz), PC-00, IP 192.168.178.29, MAC 00:00:5E:00:53:01."),
          parse_event("13.11.26", "22:10:02", "[fritzrepeaterschlafzimmer] WLAN-Gerät angemeldet (2,4 GHz), 72 Mbit/s, PC-00, IP 192.168.178.29, MAC 00:00:5E:00:53:01."),
          parse_event("13.11.26", "22:20:00", "WLAN-Gerät hat sich abgemeldet (2,4 GHz), VR200-EG, IP ---, MAC 00:00:5E:00:53:02.")]
    assert all(ev), "log line not parsed"
    assert ev[1]["ap"] == "Repeater" and ev[0]["ap"] == "FRITZ!Box" and ev[2]["band"] == "2.4" and ev[1]["up"]
    kinds = [e["kind"] for e in classify(ev)]
    assert kinds == ["roam", "login", "drop", "login", "drop"], kinds  # box->repeater is roaming; same-radio relogin is a drop
    assert is_random_mac("02:00:00:00:00:01") and not is_random_mac("00:00:5E:00:53:01")
    rows = [{"time": str(k), "ts": 1000 + k, "mac": "00:00:5E:00:53:01"} for k in range(10)]
    assert always_on("00:00:5E:00:53:01", rows, 0)[0]
    assert not always_on("00:00:5E:00:53:01", rows[:7], 0)[0]           # too few samples
    assert not always_on("02:00:00:00:00:01", rows, 0)[0]               # phones (random MAC) never count
    rows += [{"time": str(k), "ts": 1000 + k, "mac": "AA:00:00:00:00:01"} for k in range(10, 20)]
    assert not always_on("00:00:5E:00:53:01", rows, 0)[0]               # online in only half the samples
    base = {"name": "X", "mac": "00:00:5E:00:53:01", "always_on": True, "share": 0.98, "online": True, "drops_1h": 0, "rssi": -60, "ap": "Repeater", "band": "2.4"}
    assert device_alerts(base) == []
    assert device_alerts(dict(base, online=False))[0][0] == "crit"
    assert device_alerts(dict(base, drops_1h=5))[0][0] == "warn" and device_alerts(dict(base, drops_1h=60))[0][0] == "crit"
    assert device_alerts(dict(base, rssi=-77))[0][0] == "warn" and device_alerts(dict(base, rssi=-82))[0][0] == "crit"
    assert device_alerts(dict(base, always_on=False, online=False, drops_1h=60, rssi=-90)) == []
    A = lambda i, lv: {"id": i, "level": lv, "title": i, "detail": ""}
    acks = {"a": {"kind": "ack", "level": "warn", "at": 0, "last_seen": 0},
            "m": {"kind": "mute", "level": "crit", "at": 0, "until": 3600},
            "gone": {"kind": "ack", "level": "warn", "at": 0, "last_seen": 0}}
    out, st = apply_acks([A("a", "warn"), A("m", "crit"), A("n", "info")], acks, 100)
    assert [a["state"] for a in out] == ["acked", "muted", "active"] and st == "info", (out, st)
    assert "gone" in acks                                              # still within ACK_FORGET_S
    out, st = apply_acks([A("a", "crit"), A("m", "crit")], acks, 1000)
    assert out[0]["state"] == "active" and st == "crit" and "a" not in acks   # escalation re-arms
    assert "gone" not in acks                                          # problem gone long enough
    out, st = apply_acks([A("m", "crit")], acks, 4000)
    assert out[0]["state"] == "active" and "m" not in acks             # mute expired
    out, st = apply_acks([], {}, 0)
    assert st == "ok"
    dev = lambda n, als: {"name": n, "online": True, "band": "5", "alerts": als}
    devs = apply_device_states([dev("A", [A("x", "crit")]), dev("B", [A("y", "warn")]), dev("C", [])],
                               [dict(A("x", "crit"), state="muted"), dict(A("y", "warn"), state="active")])
    assert [(d["name"], d["level"], d["quiet"]) for d in devs] == [("B", 1, None), ("A", -1, "muted"), ("C", -1, None)], devs
    assert devs[1]["quiet_level"] == 2
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    import atexit
    atexit.register(live.logout)
    cached_state()  # fail early if the box is unreachable
    print(f"http://127.0.0.1:{PORT}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
