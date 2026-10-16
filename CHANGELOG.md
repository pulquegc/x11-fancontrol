# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

When a new `## [x.y.z]` section is pushed to `main`, the release workflow creates the `vx.y.z` tag, publishes a GitHub release with that section as notes, and pushes the `ghcr.io/pulquegc/x11-fancontrol:x.y.z` image. A `<!-- commit: SHA -->` line pins the release to an earlier commit; without it the release points to the commit that added the section.

## [1.1.0] - 2026-10-16
<!-- commit: beba08fe04780c3d449ed6cc530da4ecf4dc7af9 -->

### Added
- **Manual fan control.** Each fan zone has an Auto / Manual switch. In manual mode an on-screen knob sets the speed: drag it, use the scroll wheel, or the arrow keys (PageUp/PageDown for steps of 10).
- Optional timer to return a manual zone to auto after 30 minutes, 1 hour or 4 hours.
- While a zone is manual, the UI shows what the curves would set, for comparison.
- `manual_min_duty` setting (default 15%): the lowest speed the knob allows, so fans never stall.

### Safety
- If a temperature in a manual zone reaches the last point of its curve, the zone follows the curve until it cools down, and the UI says why.
- Manual settings are kept in memory only: restarting the container puts every zone back on auto.
- Manual mode is disabled while the app is in read-only mode.

## [1.0.0] - 2026-10-14
<!-- commit: 9c68562a88cbcb5bcd1a5bb20cbf17720fa28501 -->

First release.

### Added
- Control of both IPMI fan zones on Supermicro X11 boards: zone 0 (`FAN1`–`FANx`, CPU/system) and zone 1 (`FANA`, peripheral).
- Separate temperature curves for CPU, HDD, SSD and NVMe. Drives in standby are never woken up.
- Smooth behaviour: fast ramp-up, gradual ramp-down with hysteresis.
- Read-only mode on first start, until control is enabled.
- Fail-safe: repeated read errors drive the fans to full speed; stopping the container restores the original BMC fan mode.
- Web UI on port 8765 with live readings, a one-hour chart, a curve editor, an advanced JSON editor and dark mode.
- App icon, used as favicon, home-screen icon and README logo.
- Docker image on GitHub Container Registry and a ready-to-paste TrueNAS YAML.
