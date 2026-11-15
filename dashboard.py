#!/usr/bin/env python3
"""Local WLAN health dashboard for a FRITZ!Box mesh (box + repeater), read-only.

Shows internet line, band load, traffic, per-device link quality and recent WLAN events,
and raises alerts early: always-on devices going offline, reconnect loops, weak signals,
crowded or noisy channels, a weak repeater uplink, DSL problems. History (24 h) comes from
the files collect.py writes into this directory. Serves http://127.0.0.1:8765 (local only).

Run: python3 dashboard.py        Self-test: python3 dashboard.py --selftest
"""
import csv, datetime as dt, html, json, os, re, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import live  # FRITZ!Box access helpers: login, REST api, mesh, landevices, mbit, last, own_ip
from collect import merge_new  # same duplicate rule as the collector

DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(DIR, "web")        # page, base design (LCARS), script
THEME_DIR = os.path.join(DIR, "themes")   # one CSS file per theme, see README
PORT = 8765
POLL_S = 15            # page refresh interval; the box is queried at most this often

# Alert thresholds - adjust to taste
UTIL_WARN, UTIL_CRIT = 50, 70                  # channel utilization in % (mean of the last 30 min)
NOISE_WARN = -85                               # dBm; values above mean a noisy channel
RSSI_WARN, RSSI_CRIT = -75, -80                # dBm; only for always-on devices
DROPS_WARN, DROPS_CRIT = 4, 10                 # disconnects per hour, roaming excluded; always-on devices
UPLINK_RSSI_WARN, UPLINK_RATE_WARN = -70, 400  # repeater uplink on 5 GHz: dBm, Mbit/s
SNR_WARN = 6.0                                 # DSL noise margin in dB
ALWAYS_ON_SHARE, ALWAYS_ON_MIN_SAMPLES = 0.9, 8
COLLECTOR_STALE_MIN = 40
REBOOT_ALERT_H = 24    # show a box restart this long
ROAM_WINDOW_S = 10
ACK_FORGET_S = 600     # a confirmation is dropped once its problem has been gone this long
ACK_FILE = os.path.join(DIR, "acks.json")
# Local, git-ignored settings (contains MAC addresses), e.g. {"never_always_on": ["AA:BB:CC:DD:EE:FF"]}:
# devices listed there are never treated as always-on, so they raise no offline or weak-signal alerts.
CONFIG_FILE = os.path.join(DIR, "config.local.json")

LEVEL = {"info": 0, "warn": 1, "crit": 2}
# box "net" log lines (German box UI) that mean the internet was down, with a language-neutral kind for the page;
# IPv4 line only, its IPv6 twin would list the same outage twice
NET_ISSUES = (("Internetverbindung wurde getrennt", "disconnected"), ("Synchronisierung beginnt", "dsl_resync"),
              ("fehlgeschlagen", "failed"), ("unterbrochen", "interrupted"), ("nicht verfügbar", "unavailable"))
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
    samples = {r["time"] for r in link_rows if r["ts"] >= since}
    share = len({r["time"] for r in link_rows if r["ts"] >= since and r["mac"] == mac}) / len(samples) if samples else 0.0
    return not is_random_mac(mac) and len(samples) >= ALWAYS_ON_MIN_SAMPLES and share >= ALWAYS_ON_SHARE, share


def alert(aid, level, key, **args):
    """An alert without text: the page turns key + args into a sentence in the chosen language (web/app.js, I18N)."""
    return {"id": aid, "level": level, "key": key, "args": args}


def net_issue_kind(msg):
    return next((kind for text, kind in NET_ISSUES if text in msg), None)


def device_alerts(d):
    out = []
    if d["always_on"] and not d["online"]:
        out.append(alert(f"offline:{d['mac']}", "crit", "offline", name=d["name"], share=round(d["share"] * 100)))
    if d["always_on"] and d["drops_1h"] >= DROPS_WARN:
        out.append(alert(f"drops:{d['mac']}", "crit" if d["drops_1h"] >= DROPS_CRIT else "warn", "drops", name=d["name"], n=d["drops_1h"]))
    if d["always_on"] and d["online"] and d["rssi"] is not None and d["rssi"] <= RSSI_WARN:
        out.append(alert(f"rssi:{d['mac']}", "crit" if d["rssi"] <= RSSI_CRIT else "warn", "rssi", name=d["name"], rssi=d["rssi"], ap=d["ap"], band=d["band"]))
    return out


def stable_boot(prev, uptime_s, fetched_at):
    """Boot time from the box uptime; keeps the previous value while it only jitters (whole-second uptime)."""
    if not uptime_s:
        return None
    boot = fetched_at - uptime_s
    return prev if prev and abs(prev - boot) < 60 else boot


def reboot_alert(boot, crash_times, now):
    """Alert for a box restart within REBOOT_ALERT_H; crash_times are log times of error reports sent to AVM."""
    if not boot or now - boot > REBOOT_ALERT_H * 3600:
        return None
    crash = any(t >= boot for t in crash_times)
    return alert(f"reboot:{round(boot / 60)}", "warn" if crash else "info", "reboot_crash" if crash else "reboot", boot=round(boot * 1000))


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
    states = {a["id"]: a for a in alerts}  # whole alert: the page also needs since/until
    out = []
    for d in devices:
        al = [states.get(a["id"], dict(a, state="active")) for a in d["alerts"]]
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


def wifi_gen(modes):
    for m, g in (("be", "Wi-Fi 7"), ("ax", "Wi-Fi 6"), ("ac", "Wi-Fi 5"), ("n", "Wi-Fi 4")):
        if m in modes:
            return g
    return "< Wi-Fi 4" if modes else None


def device_detail(mac, ld, node, iface, link, t):
    """Static and live details of one device from the LAN device list and the mesh topology."""
    val = lambda v: None if v in (None, 255, "") else v
    mbit = lambda v: round(v / 1000) if v else None
    ms = lambda s: int(s) * 1000 if s and str(s) != "0" else None
    streams = [(w, s) for w, s in iface.get("supported_streams_rx", []) if s]
    metrics = node.get("metrics") or {}
    lat = metrics.get("latency_measurement") or {}
    wan = (metrics.get("wan_counters") or {}).get("bytes") or {}
    return {
        "ip": ld.get("ip") or None, "static_ip": ld.get("static_dhcp") == "1", "random_mac": is_random_mac(mac),
        "ipv6": [a["value"] for a in node.get("ip_addresses", []) if a.get("version") == "V6"],
        "hostname": node.get("device_name") or ld.get("name") or None,
        "vendor": node.get("device_manufacturer") or ld.get("vendorname") or None,
        "model": node.get("device_model") or ld.get("modelname") or None,
        "firmware": node.get("device_firmware_version") or None,
        "first_seen": ms(ld.get("firstused")), "last_used": ms(ld.get("lastused")),
        "connected_since": ms(link.get("last_connected")),
        "wifi": wifi_gen(iface.get("phymodes") or []), "phymodes": iface.get("phymodes") or [],
        "streams": max((s for _, s in streams), default=None), "width": streams[-1][0] if streams else None,
        "channel": iface.get("current_channel"), "security": iface.get("security"),
        "k": iface.get("11k_friendly"), "v": iface.get("11v_friendly"),
        "snr": val(link.get("rx_rsni")), "avail": val(link.get("cur_availability_tx")),
        "max_down": mbit(link.get("max_data_rate_tx")), "max_up": mbit(link.get("max_data_rate_rx")),
        "latency_ms": lat["rt_latency"] / 1000 if lat.get("status_code") == "SUCCESS" and lat.get("rt_latency") else None,
        "latency_at": lat.get("timestamp") if lat.get("status_code") == "SUCCESS" else None,
        "wan_rx": wan.get("rx"), "wan_tx": wan.get("tx"),
        "traffic": {k: [live.mbit(v) for v in t[k + "stream"]] for k in ("down", "up")} if t else None,
    }


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
        wlan, net, sys_log = box_log("wlan"), box_log("net"), box_log("sys")
        merged = hist_events + merge_new(hist_events, reversed(wlan))
        events = classify([p for p in (parse_event(e["date"], e["time"], e["msg"]) for e in merged) if p and p["ts"] >= now - 86400])
        events.sort(key=lambda e: e["ts"])
        def when(e):
            return dt.datetime.strptime(f'{e["date"]} {e["time"]}', "%d.%m.%y %H:%M:%S").timestamp()
        _slow["data"] = {
            "aps": aps, "links": links, "events": events,
            "radar": [e["msg"] for e in wlan if re.search("Radar|bevorrechtigten Nutzer", e["msg"]) and when(e) >= now - 86400],
            "net_issues": [(f'{e["date"]} {e["time"]}', net_issue_kind(e["msg"])) for e in net
                           if net_issue_kind(e["msg"]) and when(e) >= now - 86400],
            "dsl": soap("/upnp/control/wandslifconfig1", "urn:dslforum-org:service:WANDSLInterfaceConfig:1", "GetInfo"),
            "ip": soap("/upnp/control/wanipconnection1", "urn:dslforum-org:service:WANIPConnection:1", "GetStatusInfo"),
            "ui": soap("/upnp/control/userif", "urn:dslforum-org:service:UserInterface:1", "GetInfo"),
            "boot": stable_boot((_slow["data"] or {}).get("boot"),
                                int(soap("/upnp/control/deviceinfo", "urn:dslforum-org:service:DeviceInfo:1", "GetInfo").get("NewUpTime") or 0), time.time()),
            "crash_reports": [when(e) for e in sys_log if "Fehlerbericht" in e["msg"]],
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
    iface_of = {i["uid"]: (n, i) for n in m["nodes"] for i in n["node_interfaces"]}
    details = {}
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

    bands = {b: {"band": b, "aps": [], "down": [0.0] * n, "up": [0.0] * n} for b in ("2.4", "5")}
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
                details[mac] = device_detail(mac, ld, *iface_of.get(l["node_interface_2_uid"], ({}, {})), l, t)
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
        details[mac] = device_detail(mac, ld, {}, {}, {}, None)
    for d in devices.values():
        d["always_on"], d["share"] = always_on(d["mac"], slow["links"], since)
        if d["mac"] in cfg["never_always_on"]:
            d["always_on"] = False
        evs = [e for e in slow["events"] if e["mac"] == d["mac"]]
        d["drops_1h"] = sum(1 for e in evs if e["kind"] == "drop" and e["ts"] >= now - 3600)
        d["drops_24h"] = sum(1 for e in evs if e["kind"] == "drop")
        d["roams_24h"] = sum(1 for e in evs if e["kind"] == "roam")
        d["alerts"] = device_alerts(d)
        d["level"] = max((LEVEL[a["level"]] for a in d["alerts"]), default=-1)
    devices = [d for d in devices.values() if d["online"] or d["always_on"]]  # skip offline phones etc.

    alerts = [a for d in devices for a in d["alerts"]]
    for b in bands.values():
        for a in b["aps"]:
            if a["util_avg30"] is not None and a["util_avg30"] >= UTIL_WARN:
                alerts.append(alert(f"util:{b['band']}:{a['ap']}", "crit" if a["util_avg30"] >= UTIL_CRIT else "warn", "util",
                                    band=b["band"], ap=a["ap"], channel=a["channel"], pct=a["util_avg30"]))
            if a["noise"] is not None and a["noise"] > NOISE_WARN:
                alerts.append(alert(f"noise:{b['band']}:{a['ap']}", "warn", "noise", band=b["band"], ap=a["ap"], noise=a["noise"], channel=a["channel"]))
    if uplink is None:
        alerts.append(alert("uplink", "crit", "uplink_none"))
    elif (uplink["rssi"] is not None and uplink["rssi"] < UPLINK_RSSI_WARN) or (uplink["down"] or 0) < UPLINK_RATE_WARN:
        alerts.append(alert("uplink", "warn", "uplink_weak", rssi=uplink["rssi"], rate=uplink["down"]))
    dsl, ip = slow["dsl"], slow["ip"]
    if dsl.get("NewStatus") != "Up":
        alerts.append(alert("dsl", "crit", "dsl", status=dsl.get("NewStatus")))
    if ip.get("NewConnectionStatus") != "Connected":
        alerts.append(alert("inet", "crit", "inet", status=ip.get("NewConnectionStatus"), error=ip.get("NewLastConnectionError")))
    snr = min(int(dsl.get("NewDownstreamNoiseMargin") or 0), int(dsl.get("NewUpstreamNoiseMargin") or 0)) / 10
    if dsl.get("NewStatus") == "Up" and snr < SNR_WARN:
        alerts.append(alert("snr", "warn", "snr", snr=snr))
    for when_s, kind in slow["net_issues"][:3]:
        alerts.append(alert(f"netlog:{when_s[:13]}", "warn", "netlog", time=when_s, kind=kind))
    if slow["radar"]:
        alerts.append(alert("radar", "warn", "radar", line=slow["radar"][0]))  # raw box log line (German box UI)
    last_sample = max((r["ts"] for r in slow["aps"]), default=0)
    if now - last_sample > COLLECTOR_STALE_MIN * 60:
        alerts.append(alert("collector", "warn", "collector", minutes=COLLECTOR_STALE_MIN))
    reboot = reboot_alert(slow["boot"], slow["crash_reports"], now)
    if reboot:
        alerts.append(reboot)
    if slow["ui"].get("NewUpgradeAvailable") == "1":
        alerts.append(alert("update", "info", "update", version=slow["ui"].get("NewX_AVM-DE_Version")))
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
        "events": [{"ts": round(e["ts"] * 1000), "name": names.get(e["mac"]) or e["name"], "ap": e["ap"], "band": e["band"],
                    "kind": e["kind"]} for e in slow["events"][-25:][::-1]],
        "collector_age_min": round((now - last_sample) / 60) if last_sample else None,
        "details": details,  # served per device by /device, not in /data
    }


# ---------------------------------------------------------------- web page

THEME_HEAD = re.compile(r"/\*\s*theme:\s*([^*]+?)\s*\*/")  # first line: /* theme: Ocean; de: Ozean; startrek */


def parse_theme_head(line):
    """(name, German name, startrek) from a theme file's first line, or None without a header."""
    m = THEME_HEAD.match(line)
    if not m:
        return None
    name, *opts = [p.strip() for p in m[1].split(";")]
    de = next((o[3:].strip() for o in opts if o.startswith("de:")), name)
    return name, de, "startrek" in opts


def list_themes(folder=THEME_DIR):
    """[(id, name, German name, startrek)] for every <id>.css in the theme folder, LCARS (the base design) first."""
    out = []
    for f in os.listdir(folder):
        m = re.fullmatch(r"([a-z0-9-]+)\.css", f)
        if not m:
            continue
        with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:  # one badly saved file must not break the page
            h = parse_theme_head(fh.readline())
        out.append((m[1], *(h or (m[1], m[1], False))))
    return sorted(out, key=lambda t: (t[0] != "lcars", t[1].lower()))


def read_web(name):
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read()


def static_file(path):
    """(body, content type) for the page and its design files, or None. Read on every request,
    so edits to a theme show up with a page reload."""
    if path == "/":
        opts = "".join(f'<option value="{t}" data-en="{html.escape(n)}" data-de="{html.escape(de)}"{" data-startrek" if trek else ""}>{html.escape(n)}</option>'
                       for t, n, de, trek in list_themes())
        return read_web("index.html").replace("<!--THEME_OPTIONS-->", opts), "text/html; charset=utf-8"
    if path == "/app.js":
        return read_web("app.js").replace("__POLL_S__", str(POLL_S)), "text/javascript; charset=utf-8"
    if path == "/i18n.js":
        return read_web("i18n.js"), "text/javascript; charset=utf-8"
    if path == "/base.css":
        return read_web("base.css"), "text/css; charset=utf-8"
    if path == "/themes.css":
        parts = []
        for t, *_ in list_themes():
            with open(os.path.join(THEME_DIR, t + ".css"), encoding="utf-8", errors="replace") as f:
                parts.append(f.read())
        return "\n".join(parts), "text/css; charset=utf-8"
    return None


_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def cached_state():
    with _lock:  # several open tabs must not multiply the load on the FRITZ!Box
        if time.time() - _cache["t"] > POLL_S - 1:
            t0 = time.time()  # age counts from the start: fetching from the box takes seconds
            data = build_state()
            data["thr_util"], data["thr_rssi"] = UTIL_WARN, RSSI_WARN
            _cache["data"], _cache["t"] = data, t0
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
    out = dict(state, alerts=alerts, status=status, devices=apply_device_states(state["devices"], alerts))
    del out["details"]
    return out


def device_view(mac):
    """Everything known about one device: list entry, details, 24 h link history and events."""
    state = current_state()
    d = next((x for x in state["devices"] if x["mac"] == mac), None)
    if d is None:
        return None
    slow = _slow["data"]
    num = lambda v: None if v in ("", "255", None) else int(v)
    mbit = lambda v: round(int(v) / 1000) if v and int(v) > 1000 else None
    hist = [[round(r["ts"] * 1000), num(r["rssi_dbm"]), num(r["snr_db"]), mbit(r["rate_tx_kbit"]), mbit(r["rate_rx_kbit"]),
             "Repeater" if "repeater" in r["ap"].lower() else "FRITZ!Box", "5" if "5G" in r["band"] else "2.4"]
            for r in slow["links"] if r["mac"] == mac]
    events = [{"ts": round(e["ts"] * 1000), "ap": e["ap"], "band": e["band"], "kind": e["kind"]}
              for e in slow["events"] if e["mac"] == mac][::-1]
    return dict(d, detail=cached_state()["details"].get(mac, {}), hist=hist, events=events, time=state["time"],
                t_axis=state["t_axis"], thr_rssi=state["thr_rssi"])


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
        url = urllib.parse.urlsplit(self.path)
        if url.path in ("/data", "/device"):
            try:
                if url.path == "/data":
                    data = current_state()
                else:
                    mac = urllib.parse.parse_qs(url.query).get("mac", [""])[0].upper()
                    data = device_view(mac) if re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", mac) else None
                    data = data or {"error": "unknown_device"}  # translated by the page
                body = json.dumps(data).encode()
            except Exception as e:  # keep the page alive when the box is briefly unreachable
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            ctype = "application/json"
        elif (f := static_file(url.path)):
            body, ctype = f[0].encode(), f[1]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")  # theme edits show up on the next reload
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
    assert always_on("02:00:00:00:00:01", rows, 0) == (False, 0.0)     # phones (random MAC) never count
    assert always_on("02:00:00:00:00:01", [dict(r, mac="02:00:00:00:00:01") for r in rows], 0) == (False, 1.0)  # but keep their share
    rows += [{"time": str(k), "ts": 1000 + k, "mac": "AA:00:00:00:00:01"} for k in range(10, 20)]
    assert not always_on("00:00:5E:00:53:01", rows, 0)[0]               # online in only half the samples
    base = {"name": "X", "mac": "00:00:5E:00:53:01", "always_on": True, "share": 0.98, "online": True, "drops_1h": 0, "rssi": -60, "ap": "Repeater", "band": "2.4"}
    assert device_alerts(base) == []
    off = device_alerts(dict(base, online=False))[0]
    assert (off["level"], off["key"], off["args"]) == ("crit", "offline", {"name": "X", "share": 98}), off
    assert device_alerts(dict(base, drops_1h=5))[0]["level"] == "warn" and device_alerts(dict(base, drops_1h=60))[0]["level"] == "crit"
    assert device_alerts(dict(base, rssi=-77))[0]["level"] == "warn" and device_alerts(dict(base, rssi=-82))[0]["level"] == "crit"
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
                               [dict(A("x", "crit"), state="muted", until=5000), dict(A("y", "warn"), state="active")])
    assert [(d["name"], d["level"], d["quiet"]) for d in devs] == [("B", 1, None), ("A", -1, "muted"), ("C", -1, None)], devs
    assert devs[1]["quiet_level"] == 2 and devs[1]["alerts"][0]["until"] == 5000  # mute end reaches the detail page
    assert wifi_gen(["g", "n", "ax"]) == "Wi-Fi 6" and wifi_gen(["b", "g"]) == "< Wi-Fi 4" and wifi_gen([]) is None
    off = device_detail("02:00:00:00:00:01", {}, {}, {}, {}, None)    # offline device: no mesh data at all
    assert off["random_mac"] and off["snr"] is None and off["traffic"] is None and off["streams"] is None
    on = device_detail("00:00:5E:00:53:01", {"ip": "192.0.2.5", "static_dhcp": "1", "firstused": "1700000000"}, {},
                       {"supported_streams_rx": [["20 MHz", 2], ["40 MHz", 2], ["80 MHz", 0]], "phymodes": ["n", "ac"]},
                       {"rx_rsni": 255, "cur_availability_tx": 92, "max_data_rate_tx": 866000}, None)
    assert (on["streams"], on["width"], on["wifi"], on["snr"], on["avail"], on["max_down"], on["static_ip"], on["first_seen"]) \
        == (2, "40 MHz", "Wi-Fi 5", None, 92, 866, True, 1700000000000), on
    assert stable_boot(None, 0, 1e9) is None and reboot_alert(None, [], 1e9) is None   # uptime unknown
    b = stable_boot(None, 600, 1e9 + 649.6)                              # boot at hh:mm:29.6 (1e9 s is :40)
    assert stable_boot(b, 1200, 1e9 + 1250.9) == b                       # 0.9 s jitter keeps the boot time and so the alert id
    assert stable_boot(b, 5, 1e9 + 4000) == 1e9 + 3995                   # a real new restart replaces it
    assert reboot_alert(1e9, [], 1e9 + REBOOT_ALERT_H * 3600 + 1) is None   # restart too long ago
    r = reboot_alert(1e9, [1e9 + 300], 1e9 + 600)                        # report sent after the boot: crash
    assert r["level"] == "warn" and r["key"] == "reboot_crash" and r["args"]["boot"] == 1e12
    assert reboot_alert(1e9, [1e9 - 300], 1e9 + 600)["key"] == "reboot"   # older report belongs to an earlier boot
    L = lambda t, m="WLAN-Gerät hat sich abgemeldet (2,4 GHz), X": {"date": "14.11.26", "time": t, "msg": m}
    assert merge_new([L("22:24:29")], [L("22:24:30"), L("22:24:39"), L("22:24:43", "other")]) == [L("22:24:39"), L("22:24:43", "other")]  # ±1 s shift
    assert merge_new([], [L("22:01:30"), L("22:01:32")]) == [L("22:01:30"), L("22:01:32")]  # same read: real back-and-forth
    assert merge_new([L("22:01:30")], [L("22:01:30"), L("22:01:32")]) == [L("22:01:32")]   # one known line absorbs one entry
    assert net_issue_kind("Internetverbindung wurde getrennt.") == "disconnected"
    assert net_issue_kind("DSL-Synchronisierung beginnt (Training).") == "dsl_resync"
    assert net_issue_kind("Internetverbindung IPv6 wurde getrennt, Präfix nicht mehr gültig.") is None
    assert net_issue_kind("Internetverbindung wurde erfolgreich hergestellt. IP-Adresse: 192.0.2.1") is None
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for f, head in (("b.css", "/* theme: Zeta; startrek */"), ("a.css", "/* theme: Alpha */"), ("lcars.css", "/* theme: LCARS */"),
                        ("noname.css", "html{}"), ("Bad Name.css", "/* theme: X */"), ("notes.txt", "")):
            open(os.path.join(tmp, f), "w").write(head + "\n")
        open(os.path.join(tmp, "k.css"), "wb").write("/* theme: K\u00fchl */\n".encode("latin-1"))  # not UTF-8
        assert list_themes(tmp) == [("lcars", "LCARS", "LCARS", False), ("a", "Alpha", "Alpha", False), ("k", "K\ufffdhl", "K\ufffdhl", False),
                                    ("noname", "noname", "noname", False), ("b", "Zeta", "Zeta", True)], list_themes(tmp)
    assert parse_theme_head("/* theme: Ocean; de: Ozean */") == ("Ocean", "Ozean", False)
    assert parse_theme_head("/* theme: LCARS; startrek */") == ("LCARS", "LCARS", True)
    assert list_themes()[0] == ("lcars", "LCARS", "LCARS", True)
    for f in os.listdir(THEME_DIR):   # every shipped theme names itself in line 1
        assert THEME_HEAD.match(open(os.path.join(THEME_DIR, f), encoding="utf-8").readline()), f
    # the page start must be a real line of its own, not swallowed by a // comment (happened in b432062)
    assert re.search(r"^dispatchEvent\(.*setInterval\(tick", read_web("app.js"), re.M), "app.js: page start missing"
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
