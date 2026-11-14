![WLAN Dashboard – local WiFi health monitoring](assets/banner.png)

# WLAN Dashboard

Local, read-only health dashboard for a FRITZ!Box mesh (FRITZ!Box + FRITZ!Repeater).
It shows the internet line, channel load and traffic per band, link quality per device
and recent WLAN events, and raises alerts early:

- always-on devices going offline
- reconnect loops (roaming between box and repeater is not counted)
- weak signal of always-on devices
- crowded or noisy channels
- weak repeater uplink
- DSL / internet interruptions, radar interruptions on 5 GHz
- FRITZ!Box restarts in the last 24 h (flagged as a likely crash when the box sent an error report)
- stopped history collection, available FRITZ!OS updates

Clicking a device opens its detail page: connection, signal and noise margin, negotiated and
maximum rates, Wi-Fi standard, streams, latency, addresses, 24 h signal and rate history and
all WLAN events of the device.

Themes: LCARS (default), Klingon, Futuristic, Retro 8Bit, MacBook, Newspaper, Woodworker,
Pokemon, Unicorn, Coca-Cola, C64 and Hundertwasser, picked in the header and remembered per browser. A theme is a block of CSS custom properties plus shape
overrides under `html[data-theme="…"]` in `dashboard.py`; adding one means adding a block and an
`<option>`.

Alerts can be acknowledged or muted for 1 h / 24 h; they re-arm automatically when the
problem gets worse, when a mute expires, or when a resolved problem comes back.

Everything runs on your own machine. The dashboard only binds to `127.0.0.1` and only
reads from the FRITZ!Box.

## Files

| File | Purpose |
|---|---|
| `dashboard.py` | The dashboard: web server on http://127.0.0.1:8765, alert logic, page |
| `collect.py` | Collector for the 24 h history: WLAN log, link quality per device, channel load. Run every 15 min |
| `live.py` | FRITZ!Box access helpers used by the dashboard (also a minimal standalone traffic view) |
| `launchd/local.wlan-test.collect.plist` | macOS LaunchAgent template that runs `collect.py` every 15 min |

## Setup

1. Create a FRITZ!Box user for the dashboard (System → FRITZ!Box-Benutzer) with
   permission for FRITZ!Box settings. Read access is all the scripts use.
2. Store its password in `~/.claude_fritz` (first line only) and restrict access:

   ```sh
   printf '%s\n' 'the-password' > ~/.claude_fritz && chmod 600 ~/.claude_fritz
   ```

   The user name `claude` and the box address `192.168.178.1` are set at the top of
   `live.py` and `collect.py`.
3. Start the history collector every 15 minutes (macOS):

   ```sh
   sed "s#/path/to/wlan-dashboard#$PWD#g" launchd/local.wlan-test.collect.plist > ~/Library/LaunchAgents/local.wlan-test.collect.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.wlan-test.collect.plist
   ```

   On first run macOS asks for "Local Network" access for `python3`; allow it
   (System Settings → Privacy & Security → Local Network).
4. Start the dashboard and open http://127.0.0.1:8765:

   ```sh
   python3 dashboard.py
   ```

Self-test of the alert logic: `python3 dashboard.py --selftest`

## Tuning

Alert thresholds are constants at the top of `dashboard.py` (channel load, noise, signal,
reconnects per hour, repeater uplink, DSL noise margin, "always-on" detection).

Devices that should never count as always-on (e.g. a game console or a tablet that is often
switched off) go into `config.local.json` next to `dashboard.py`; they then raise no offline
or weak-signal alerts. The file is git-ignored because it contains MAC addresses:

```json
{ "never_always_on": ["AA:BB:CC:DD:EE:FF"] }
```

## Private data — never commit

The collector and the dashboard write MAC addresses, device names and network events into
this directory (`*.csv`, `*.jsonl`, `*.log`, `acks.json`, `config.local.json`). They are excluded via
`.gitignore`, as is the password file. Python standard library only, no dependencies.
