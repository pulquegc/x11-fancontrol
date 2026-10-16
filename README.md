<p align="center"><img src="docs/icon.svg" alt="X11 Fan Control icon" width="128"></p>

<h1 align="center">X11 Fan Control</h1>

**English** · [Italiano](README.it.md)

A small, self-contained fan controller for **Supermicro X11 motherboards** running **TrueNAS SCALE**, packaged as a Docker container with a minimal web UI.

It reads the CPU temperature from the BMC and the drive temperatures with `smartctl`, computes a duty cycle for each IPMI fan zone from configurable curves, and applies it with Supermicro's raw IPMI commands. Drives in standby are never woken up.

<!-- ![Web UI](docs/screenshot.png) -->

## Features

- **Two fan zones**: zone 0 (`FAN1`–`FANx`, CPU/system) and zone 1 (`FANA`, peripheral — the usual place for drive cage fans).
- **Separate curves** for CPU, HDD, SSD and NVMe. Each zone follows any combination of them and uses the highest result.
- **Smooth behaviour**: speeds go up immediately and come down gradually, with hysteresis, so fans don't keep hunting.
- **Read-only first run**: the app starts in `dry_run` mode and only shows what it *would* do until you enable control.
- **Fail-safe**: repeated read errors drive the fans to full speed; stopping the container restores the original BMC fan mode.
- **Manual control**: switch any zone to *Manual* and set its speed with an on-screen knob (drag, scroll wheel or arrow keys), optionally returning to auto after 30 min, 1 h or 4 h.
- **Web UI** (port 8765): live temperatures and fan speeds, a one-hour chart, a curve editor and an advanced JSON editor. Works on phones and follows the system dark mode.
- **No dependencies** beyond `ipmitool`, `smartmontools` and the Python standard library.

## Supported hardware

| Board | Status |
|---|---|
| Supermicro X11SCA-F | Reference board, being tested |
| Other Supermicro X11 boards with IPMI | Expected to work (same raw commands), untested |
| X9 / X10 | May work, untested |
| Non-Supermicro boards | Not supported |

If you run it on another board, please open an issue with the output of `ipmitool sdr type Fan` and whether it worked.

## Installation on TrueNAS SCALE (24.10+)

1. **Create a dataset** for the configuration, e.g. `tank/fancontrol`.
2. Go to **Apps → Discover Apps → ⋮ → Install via YAML**, name the app `fancontrol` and paste:

   ```yaml
   services:
     fancontrol:
       image: ghcr.io/pulquegc/x11-fancontrol:latest
       container_name: x11-fancontrol
       restart: unless-stopped
       privileged: true
       stop_grace_period: 30s
       environment:
         TZ: Europe/Rome
         # UI_TOKEN: "change-me"
       ports:
         - "8765:8765"
       volumes:
         - /mnt/tank/fancontrol:/data
   ```

   Change the volume path to your dataset and `TZ` to your time zone. `latest` follows the newest version; to stay on a specific one, use its number instead, e.g. `:1.1.0` (see [Releases](https://github.com/pulquegc/x11-fancontrol/releases) and the [changelog](CHANGELOG.md)).
3. Open `http://<nas-ip>:8765`. The badge shows **Read-only**.
4. Leave it in read-only mode for a while (ideally during a scrub or a large transfer), check that CPU and drive temperatures look right, adjust the curves, then press **Enable control**.

`privileged: true` is required to reach `/dev/ipmi0` and to read drive SMART data.

## How it works

- Every `interval` seconds the controller reads the BMC temperature and fan sensors (`ipmitool sdr`).
- Every `disk_interval` seconds it scans drives with `smartctl --scan` and reads their temperature with `smartctl -n standby`, classifying each one as HDD, SSD or NVMe.
- For each zone it evaluates the curves of its sources (linear interpolation between points) and keeps the highest value, clamped to the zone's `min_duty`/`max_duty`.
- When control is enabled, it switches the BMC to **Full** mode (so the BMC stops overriding) and sets each zone with `ipmitool raw 0x30 0x70 0x66 0x01 <zone> <duty>`.
- A zone in **manual mode** keeps the speed you set, with one exception: if any of its source temperatures reaches the last point of its curve, the zone runs at the curve's speed until it cools down. Manual settings live in memory, so a container restart puts every zone back on auto.
- On shutdown it restores the BMC mode it found at startup (or `restore_mode_on_exit`, default **Optimal**).

## Configuration

Settings live in `/data/config.json` and can be edited from the UI. The main keys:

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | Read-only mode: compute but don't touch the fans |
| `interval` | `10` | Seconds between control cycles |
| `disk_interval` | `60` | Seconds between drive temperature reads |
| `hysteresis` | `2` | °C a temperature must drop before speeds go down |
| `max_step_down` | `5` | Max duty decrease (%) per cycle |
| `failsafe_duty` | `100` | Duty applied after repeated errors |
| `failsafe_after_errors` | `3` | Consecutive errors before fail-safe |
| `restore_mode_on_exit` | `2` | BMC mode on exit if the original was Full (0 Standard, 1 Full, 2 Optimal, 4 Heavy IO) |
| `manual_min_duty` | `15` | Lowest duty the manual knob allows |
| `cpu_sensor` | `"CPU Temp"` | BMC sensor used for the CPU temperature |
| `disks_exclude` | `[]` | Substrings of device paths to ignore, e.g. `["/dev/sdz"]` |
| `fan_lower_thresholds` | `null` | Lower BMC RPM thresholds applied when control starts, e.g. `[100, 200, 300]` |
| `zones` | see file | Per-zone name, `sources`, `min_duty`, `max_duty` |
| `curves` | see file | `[[temp_c, duty_pct], ...]` for `cpu`, `hdd`, `ssd`, `nvme` |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8765` | Web UI port |
| `DATA_DIR` | `/data` | Where `config.json` is stored |
| `UI_TOKEN` | *(empty)* | If set, saving from the UI requires this token |
| `IPMI_HOST`, `IPMI_USER`, `IPMI_PASS` | *(empty)* | Use a remote BMC over LAN instead of `/dev/ipmi0` |

## Troubleshooting

**Fans cycle between slow and full speed every few seconds.** This is the classic Supermicro issue: when a fan drops below the BMC's lower RPM threshold, the BMC assumes it failed and spins everything up. Low-RPM fans (e.g. Noctua) trigger it easily. Set `fan_lower_thresholds` to values below your fans' minimum speed, e.g. `[100, 200, 300]`, and toggle control off and on to apply them.

**`Sensor 'CPU Temp' not found`.** Run `ipmitool sdr type Temperature` on the host and put the right name in `cpu_sensor`.

**Drives are missing.** Make sure the container runs with `privileged: true`. Drives in standby are listed as such and ignored until they wake up.

## Security

The web UI has no login. Keep it on your LAN, and set `UI_TOKEN` if other people can reach it. The container needs privileged access to the BMC and the drives, so only run images you trust.

## Disclaimer

This software changes how your server is cooled. It is provided as-is, without warranty. Keep TrueNAS temperature alerts enabled and watch your temperatures after enabling control.

## License

[MIT](LICENSE)
