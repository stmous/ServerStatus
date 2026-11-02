# Architecture and design decisions

The guiding constraint: **this runs unattended in a server room for years, and
its one job is to tell someone when something is wrong.** So reliability and
"failing loudly" beat features everywhere below.

```
                    ┌──────────────────────── Raspberry Pi 4 ───────────────────────────┐
 SHT45 ─I2C─▶ collector ──line protocol──▶ InfluxDB 2.x ──Flux──▶ Grafana ──▶ Telegram │
 (0x44)       (systemd, Type=notify,         127.0.0.1:8086          127.0.0.1:3000 ─▶ SMTP     │
 SSD1306 ◀──── WatchdogSec)                  raw 30 d                  │  │       ─▶ webhook  │
 (0x3C)        │   ▲                          5-min 2 y ◀─task─┘       │  └─▶ Chromium kiosk  │
               │   └─ SQLite spool (InfluxDB down)                     └─▶ tailscale serve ──┼─▶ phone/laptop
               └──────────── heartbeat ping ───────────────────────────────────────────────┼─▶ healthchecks.io
                    └─────────────────────────────────────────────────────────────────────┘
```

## Data flow and storage

**InfluxDB 2.x with two buckets.** Raw 30-second data is kept 30 days. A Flux
task writes 5-minute `mean`/`min`/`max` into a second bucket kept 2 years (an
`agg` tag distinguishes them, field names stay the same). At ~2 points per
cycle, raw data is about 175k points a month: trivial for the Pi.
The dashboard's graphs switch to the downsampled bucket automatically for time
ranges over 48 h, so a "last 90 days" view stays fast and keeps working after
raw data has expired.

The task recomputes the **last two** 5-minute windows on each run, so points
that arrive late (flushed from the collector's spool after an outage) are still
aggregated. Rewriting a window is idempotent because the series and timestamp
are identical.

**No InfluxDB client library.** The collector speaks the HTTP write API
directly (`requests` and a small, unit-tested line-protocol encoder). It is one
fewer large dependency and gives exact control over timeouts and over how HTTP
statuses map to "retry later" (5xx, connection errors, *and* 401/403/404, which
are fixable misconfigurations) versus "drop" (400/413/422, malformed data).

**Local spool in SQLite**, not a flat file. SQLite survives power loss mid-write
without corrupting earlier data, and "delete exactly the rows that were just
flushed" is a single statement. It is capped (oldest dropped first, logged), and
flushing has a time budget so a large backlog can't starve the watchdog.

**Points carry the reading's own timestamp**, and the collector refuses to write
anything stamped before 2025. The Pi 4 has no RTC, so until NTP syncs, the clock
may show the last shutdown time. At startup the collector waits (bounded) for
`systemd-timesyncd` to report sync.

**DHT11/DHT22 via the kernel, not Python.** The DHT single-wire protocol needs
microsecond timing, which userspace Python on Linux can't guarantee (the
Adafruit DHT library is notoriously flaky on a Pi). The Raspberry Pi kernel's
`dht11` driver (enabled by `dtoverlay=dht11,gpiopin=N`) decodes it in interrupt
context and exposes the result through IIO sysfs, so the driver here just reads
two files. It needs no extra Python dependency.

## Validation

1. **Bounds:** −20…80 °C, 0…100 %RH. Out-of-range values are garbage and count as a sensor failure. A sensor that only returns garbage is a broken sensor.
2. **Spike filter with re-baselining.** A naive "reject jumps larger than X" filter has a fatal flaw: after a restart, an outage, or a *real* fast change (AC failure), every new value differs from the stale baseline and is rejected forever. The monitor goes blind exactly when it matters. So the jump check only applies against a *recent* baseline (`jump_window`), and once `rebaseline_after` consecutive readings agree with *each other*, they are accepted as the new baseline. A one-sample glitch is dropped; a real step change gets through after about 1.5 minutes.
3. **I2C retries** with exponential backoff within a cycle; after N failed cycles the bus is re-initialized (closing and reopening the Blinka I2C object recovers most stuck-bus states).

## Health signals: three independent layers

| Failure | Detected by |
|---|---|
| Room too hot / humid / dry | Grafana threshold rules |
| Sensor broken or disconnected | `collector_status.sensor_ok` metric → *Sensor error* rule; the heartbeat pings `/fail` |
| Collector crashed or hung | systemd `Restart=always` + `WatchdogSec`; *No data* rule |
| InfluxDB down | Collector spools locally; *No data* rule fires (`execErrState: Alerting`) |
| Kernel / PID 1 hang | BCM2835 hardware watchdog via systemd `RuntimeWatchdogSec=15` |
| Pi dead, no power, no network | Heartbeat stops → healthchecks.io alerts (it is outside the Pi) |
| Grafana alerting itself broken | Optional *deadman* rule pings a second external check |

The collector writes `collector_status` (with `sensor_ok`) **every cycle**, not
just on failure, so the sensor-error alert can resolve on its own and its absence
is itself meaningful.

**Rule states.** The threshold rules use `noDataState`/`execErrState:
KeepLast`, so when data stops they hold their state instead of sending a
misleading "✅ resolved". The stale-data rule is the one that speaks up, with
`Alerting` for both no-data and errors and **no** extra pending period:
`stale_after` already *is* the debounce, and stacking a 2-minute pending period
on top would delay a critical alert to 5 minutes. (This was found by running the
full alert cycle against real Grafana: with the default `OK` state, stopping
the collector "resolved" a firing temperature alert.)

Warning and critical rules **overlap** (above 30 °C both fire): escalating from
warning to critical then doesn't produce a confusing "warning resolved" message.

## Configuration as the single source of truth

Thresholds would otherwise live in three places (alert rules, dashboard colors,
OLED). Instead `config.yaml` is parsed and validated by typed dataclasses
(every error names the exact key), and `serverroom-monitor render-grafana`
**generates** the dashboard JSON, alert rules, contact points, notification
policy and Grafana's settings from it. Grafana provisioning can't enable
contact points conditionally, so generating is also how channels are switched
on and off (with `deleteContactPoints` cleaning up removed ones). The repo's
`grafana/` directory holds the rendered defaults for review; a test keeps it
in sync with the generator.

Grafana is configured through `GF_*` environment variables in a systemd
drop-in, so the packaged `grafana.ini` is never edited and apt upgrades don't
conflict.

## Secrets and least privilege

- One human-edited file, `secrets.env` (root, 600). `apply-config.sh` splits it into per-service env files: the collector sees only its write token and heartbeat URL; Grafana sees only its read token, admin password and channel secrets.
- InfluxDB tokens are scoped: the collector can **write the raw bucket only**, and Grafana can **read only**. The all-access operator token is used by setup and backup, never by a running service.
- InfluxDB ≥ 2.9 stores tokens hashed and can't show them again. Setup therefore keeps the tokens it already holds, checks that they still work (an empty write returns 204 only with write permission), and otherwise replaces them by description.
- Secrets reach Grafana as `${VAR}` placeholders in the contact-point file, expanded from its environment. Verified against Grafana 12.3: alert *rule* files are not expanded, so `$labels`/`$values` in annotations are written as-is.
- Credentials are passed to `curl` and `grafana cli` on stdin or file descriptors, never as arguments visible in `ps`.
- The collector runs as a dedicated system user in the `i2c` group with a hardened unit (`ProtectSystem=strict`, empty capability set, `DevicePolicy=closed` + `DeviceAllow=char-i2c`).

## Network exposure

Grafana and InfluxDB bind to **127.0.0.1**. Remote access is `tailscale serve`,
which terminates HTTPS with a real certificate on the tailnet and proxies to
localhost; nothing is port-forwarded. ufw denies all incoming traffic except SSH
from the LAN and Tailscale, and 443 on `tailscale0`. LAN access to Grafana is an
explicit opt-in (`grafana.lan_access`), and it turns off the secure-cookie flag,
which plain HTTP would break.

Why not bind Grafana to the Tailscale IP as well? Grafana takes a single listen
address, and the Tailscale address may not exist yet when Grafana starts at
boot. `tailscale serve` avoids both problems and adds HTTPS.

## Kiosk

**Login problem.** Anonymous access is off, so Chromium would land on a login
page. Header-based "auth proxy" logins were rejected: `tailscale serve` also
connects from 127.0.0.1, so a remote user could forge the header. Instead the
installer creates a Grafana **shared (public) dashboard** link via the API: read-only,
limited to that one dashboard, no session to expire. It exists only while
`display.mode` is `hdmi`.

**Compositor.** On Lite, `cage` (a single-application Wayland compositor)
runs Chromium on tty1 as an unprivileged `kiosk` user via a systemd unit with
`PAMName=login`: no desktop, nothing to break. On the Desktop image, an XDG
autostart entry works unchanged under labwc, wayfire and LXDE/X11, all of which
run XDG autostart. Wayland has no `unclutter`, so the cursor is hidden with a
generated, fully transparent Xcursor theme; X11 additionally uses `unclutter`.

**Self-healing.** A launcher loop restarts Chromium when it exits (with backoff
for crash loops) and once a day (long-running Chromium leaks memory on a 4 GB
Pi). It also clears Chromium's "didn't shut down correctly" flag after power
cuts. systemd restarts cage itself.

**Layout.** The first screen (current values, 24 h graphs) is designed to fit
800×480, the official 7" display. Grafana stacks panels in order below 769 px
width, so on a phone the current values come first.

## OS-level reliability

- **Hardware watchdog** via systemd's `RuntimeWatchdogSec=15`, not the old `watchdog` daemon: one fewer moving part, and PID 1 is the thing whose hang matters.
- **journald** persistent but capped at 200 MB, syncing every 5 minutes, instead of log2ram. log2ram loses exactly the logs you need after a crash, and on an SSD the write savings are not worth it.
- **Unattended upgrades** for Debian / Raspberry Pi OS security updates and Tailscale only. Grafana and InfluxDB are excluded: a major Grafana release has changed provisioning semantics before, and an unattended upgrade must never silently break alerting. No automatic reboots by default.
- `Restart=always` with `StartLimitIntervalSec=0` on the collector, Grafana and InfluxDB: never give up.

## Testing strategy

- **Unit tests** (pytest) use a scriptable mock sensor and a fake InfluxDB writer: validation and re-baselining, dew-point reference values, buffering and flushing (including partial outages, rejected batches and capacity), config parsing (every invalid value names its key), Grafana generation, OLED rendering, sd_notify over a real Unix socket, and the shell helpers' secret quoting.
- **`--simulate` scenarios** (`overheat`, `dry`, `humid`, `spikes`, `flaky`, `dead`, plus `--sim-speed`) drive the real stack through every alert without hardware. `--dry-run` needs no database at all.
- During development, the whole pipeline was run against **real InfluxDB 2.8.0 / 2.9.1 and Grafana 12.3**: setup idempotency, buffering through an InfluxDB outage, downsampling output, firing → no-data → resolved notifications delivered to a webhook, the Grafana contact-point test API, the shared dashboard, and screenshots at 800×480 and phone width. Several bugs were found that way: `$$` escaping, misleading resolves, and InfluxDB 2.9 token hashing.
