#!/usr/bin/env python3
"""Collect FRITZ!Box WLAN log entries, link quality of all WLAN clients and AP load (read-only).

Appends new WLAN log lines to events.jsonl (deduplicated), one row per connected
WLAN client to links.csv and one row per access point radio to aps.csv.
Meant to run every 15 minutes via launchd.
"""
import csv, datetime, hashlib, json, os, re, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

BOX = "192.168.178.1"
USER = "claude"
PW = open(os.path.expanduser("~/.claude_fritz")).readline().strip()
DIR = os.path.dirname(os.path.abspath(__file__))


def login():
    x = urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2", timeout=20).read().decode()
    _, i1, s1, i2, s2 = re.search(r"<Challenge>(.*?)</Challenge>", x).group(1).split("$")
    h1 = hashlib.pbkdf2_hmac("sha256", PW.encode(), bytes.fromhex(s1), int(i1))
    h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(s2), int(i2))
    body = urllib.parse.urlencode({"username": USER, "response": f"{s2}${h2.hex()}"}).encode()
    x = urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2", body, timeout=20).read().decode()
    sid = re.search(r"<SID>(.*?)</SID>", x).group(1)
    if sid == "0000000000000000":
        raise SystemExit("login failed")
    return sid


def wlan_log(sid):
    body = urllib.parse.urlencode({"sid": sid, "page": "log", "lang": "de", "filter": "wlan", "xhr": 1}).encode()
    return json.loads(urllib.request.urlopen(f"http://{BOX}/data.lua", body, timeout=30).read())["data"]["log"]


def mesh_links():
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{BOX}:49000", USER, PW)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    svc, act = "urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetMeshListPath"
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{act} xmlns:u="{svc}"/></s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://{BOX}:49000/upnp/control/hosts", env.encode(),
                                 {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f"{svc}#{act}"})
    path = next(el.text for el in ET.fromstring(opener.open(req, timeout=20).read()).iter() if el.tag.endswith("MeshListPath"))
    mesh = json.loads(urllib.request.urlopen(f"http://{BOX}:49000{path}", timeout=20).read())
    ifs = {i["uid"]: (n["device_name"], i["name"], i.get("mac_address")) for n in mesh["nodes"] for i in n["node_interfaces"]}
    links, aps, seen = [], [], set()
    for n in mesh["nodes"]:
        for i in n["node_interfaces"]:
            if n.get("is_meshed") and i["type"] == "WLAN" and i["name"].startswith("AP:") and i.get("current_channel"):
                aps.append((n["device_name"], i["name"], i["current_channel"], i.get("channel_utilization"), i.get("anpi")))
            for l in i.get("node_links", []):
                if l["type"] != "WLAN" or l["state"] != "CONNECTED" or l["uid"] in seen:
                    continue
                seen.add(l["uid"])
                ap, cl = ifs[l["node_interface_1_uid"]], ifs[l["node_interface_2_uid"]]
                links.append((cl[0], cl[2], ap[0], ap[1], l["rx_rcpi"], l["rx_rsni"], l["cur_data_rate_rx"], l["cur_data_rate_tx"]))
    return links, aps


def main():
    now = datetime.datetime.now().isoformat(timespec="seconds")
    sid = login()
    try:
        entries = wlan_log(sid)
    finally:
        urllib.request.urlopen(f"http://{BOX}/login_sid.lua?version=2&logout=1&sid={sid}", timeout=10)

    ev_path = os.path.join(DIR, "events.jsonl")
    seen = set()
    if os.path.exists(ev_path):
        with open(ev_path) as f:
            seen = {json.loads(line)["key"] for line in f}
    new = 0
    with open(ev_path, "a") as f:
        for e in reversed(entries):  # oldest first
            key = f'{e["date"]} {e["time"]} {e["msg"]}'
            if key not in seen:
                seen.add(key); new += 1
                f.write(json.dumps({"key": key, "date": e["date"], "time": e["time"], "msg": e["msg"], "collected": now}, ensure_ascii=False) + "\n")

    links, aps = mesh_links()
    for fname, header, rows in (("links.csv", ["time", "device", "mac", "ap", "band", "rssi_dbm", "snr_db", "rate_rx_kbit", "rate_tx_kbit"], links),
                                ("aps.csv", ["time", "ap", "band", "channel", "utilization_pct", "noise_dbm"], aps)):
        path = os.path.join(DIR, fname)
        fresh = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(header)
            w.writerows([now, *r] for r in rows)
    print(f"{now}: {len(entries)} log entries on box, {new} new; {len(links)} WLAN clients, {len(aps)} radios")
    # shortcut: the box drops old log lines; if new == len(entries) entries were probably lost, collect more often then


if __name__ == "__main__":
    main()
