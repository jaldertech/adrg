# ADRG — Aldertech Dynamic Resource Governor

> **"Why is Jellyfin stuttering every time my downloads start?"**
>
> ADRG fixes that — automatically, at the kernel level.

ADRG is a lightweight daemon that watches your home server's real-time resource pressure (CPU, memory, I/O, and temperature) and dynamically throttles background containers so your interactive services always feel snappy.

No more Jellyfin stuttering because Tdarr decided to transcode something. No more Home Assistant lag because Kopia kicked off a backup. ADRG handles it silently, in the background, every five seconds.

---

## The Problem

A home server running 20–50 Docker containers on a Raspberry Pi 5 or an Intel N100 is constantly fighting itself. When a background task like a transcoder, unpacker, or backup agent wakes up, it competes for the same CPU, disk I/O, and RAM as your media server and dashboards.

The Linux kernel has no way of knowing that your Jellyfin stream matters more than your torrent client. It gives them equal weight — and your film buffers.

---

## The Solution

ADRG assigns your containers to **resource tiers** and enforces priorities using the Linux kernel's own cgroup v2 interface — the same mechanism Docker uses internally. No external agents, no wrappers, no polling overhead.

When ADRG detects a trigger (media playback, high temperature, memory pressure, or an I/O storm), it applies the appropriate constraints:

- **Tier 3 containers** (bulk tasks) are **paused** — completely frozen until the pressure clears.
- **Tier 2 containers** (background tasks) are **throttled** — CPU and I/O capped to a small fraction.
- **Tier 0/1 containers** are **untouched** — maximum priority, always.

When the trigger clears, everything is restored automatically.

---

## Features

- **Four enforcer rules:** Media Mode, Thermal Protection, Memory Pressure, I/O Saturation
- **PSI-based decisions:** Uses Linux Pressure Stall Information — far more accurate than load average
- **Media providers:** Jellyfin, Plex, or webhook (for any other source)
- **Download client throttle:** Automatically caps qBittorrent download speed during media playback
- **Protected containers:** Tier 0 and an explicit list are never touched under any circumstance
- **Glob pattern matching:** `tdarr*` in a tier matches `tdarr`, `tdarr_node`, `tdarr_node2`, etc.
- **Notifications:** Discord, NTFY, and Gotify — all optional, all configurable
- **HTTP API:** `/status` for live state, `/trigger` for external control (n8n, Home Assistant, scripts)
- **Dry-run mode:** Observe every decision without applying a single change
- **SIGHUP config reload:** Update `config.yaml` and reload without restarting the daemon
- **Designed for constrained hardware:** Pi 5, N100, and similar low-power multi-container hosts

---

## Requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Linux kernel | 4.20+ | For PSI support. 5.10+ recommended. |
| cgroup v2 | Required | Verify: `ls /sys/fs/cgroup/cgroup.controllers` |
| Docker | Any modern version | Uses the Docker SDK via `/var/run/docker.sock` |
| Python | 3.9+ | |
| Root access | Required | cgroup writes require root or `CAP_SYS_ADMIN` |

### Enabling cgroup v2

Most modern distros (Debian Bookworm, Ubuntu 22.04+, Raspberry Pi OS Bookworm) ship with cgroup v2 enabled by default.

If it is not enabled, add the following to your kernel command line and reboot:

**Raspberry Pi** — edit `/boot/firmware/cmdline.txt`:
```
cgroup_no_v1=all
```

**Other systemd systems** — add to kernel parameters:
```
systemd.unified_cgroup_hierarchy=1
```

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/jaldertech/adrg.git
cd adrg

# 2. Run the installer (requires root)
sudo bash setup.sh

# 3. Edit your config — assign containers to tiers
sudo nano /etc/adrg/config.yaml

# 4. Add your API keys
sudo nano /etc/adrg/adrg.env

# 5. Restart the daemon
sudo systemctl restart adrg

# 6. Watch it work
journalctl -u adrg -f
```

---

## Configuration

The full annotated template is installed at `/etc/adrg/config.yaml`. The key sections are:

### Tiers

Assign your containers to tiers. Names support glob patterns (`tdarr*`).

```yaml
tiers:
  0:
    name: "Core Infra"           # Never touched. Ever.
    containers: ["pihole", "nginx-proxy-manager", "homeassistant"]
    cpu_weight: 1000
    io_weight: 1000

  1:
    name: "Interactive"          # High priority. Protected during pressure.
    containers: ["jellyfin", "komga", "homepage"]
    cpu_weight: 800
    io_weight: 800
    memory_high: "3G"
    memory_max: "4G"

  2:
    name: "Background"           # Throttled during media playback.
    containers: ["sonarr", "radarr", "prowlarr", "qbittorrent"]
    cpu_weight: 100
    io_weight: 100
    memory_high: "1.5G"
    memory_max: "2G"

  3:
    name: "Bulk"                 # Paused during media playback. Restarted during memory emergencies.
    containers: ["tdarr*", "unpackerr", "kopia"]
    cpu_weight: 10
    io_weight: 10
    memory_high: "2G"
    memory_max: "3G"
```

> **Note:** Tier 0 containers are **always implicitly protected** from any pause or restart action — you do not need to add them to `protected_containers` as well.

### Protected Containers

An explicit list of containers that ADRG will never pause, throttle, or restart, regardless of any pressure rule. Useful for containers in Tier 2/3 that you still want to safeguard (e.g. a database that a background worker writes to).

```yaml
protected_containers:
  - postgres
  - redis
```

### Media Mode

When active streams are detected, Tier 3 is paused and Tier 2 is throttled.

```yaml
media_mode:
  enabled: true
  provider: jellyfin        # jellyfin | plex | webhook | none
  url: "http://jellyfin:8096"
  api_key: "${ADRG_MEDIA_API_KEY}"
  tier2_cpu_max_percent: 20
  tier2_io_max_read_mbps: 10
  tier2_io_max_write_mbps: 5
  cooldown_seconds: 60

  download_throttle:        # Optional: cap download speed during playback
    enabled: true
    provider: qbittorrent
    url: "http://qbittorrent:8080"
    username: "admin"
    password: "${ADRG_QB_PASSWORD}"
    limit_mbps: 5
```

For `provider: webhook`, stream state is controlled externally via `POST /trigger` — useful for Plex users, Emby, or any custom trigger.

### Secrets

Store API keys in `/etc/adrg/adrg.env` (created by `setup.sh`):

```bash
ADRG_MEDIA_API_KEY=your_jellyfin_api_key
ADRG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ADRG_NTFY_URL=https://ntfy.sh/your-topic
ADRG_QB_PASSWORD=your_qbittorrent_password
```

These are referenced in `config.yaml` as `${ADRG_MEDIA_API_KEY}` etc. and expanded at startup.

---

## The Four Rules

Rules are evaluated every 5 seconds (configurable) in priority order.

### 1. Thermal Protection

Reads the maximum temperature across all thermal zones. Two escalating stages with hysteresis on recovery.

| Threshold | Action |
|---|---|
| `warn_temp_c` (default 70°C) | Log warning only |
| `stage1_temp_c` (default 75°C) | Pause all Tier 3 containers |
| `stage2_temp_c` (default 80°C) | Pause Tier 2 + 3 containers |
| Below `recovery_temp_c` for `recovery_hold_seconds` | Restore all containers |

### 2. Memory Pressure

Uses Linux PSI (`/proc/pressure/memory`) for accurate, kernel-reported pressure rather than crude RSS polling.

| Trigger | Action |
|---|---|
| `pressure_avg10` > 50% | Squeeze `memory.high` on Tier 3 containers (Stage 1) |
| `some_avg60` > 40% | Restart highest-RSS Tier 3 container (Stage 2) |
| `full_avg10` > 25% | Emergency restart — escalates to Tier 2 if Tier 3 exhausted (Stage 3) |

### 3. I/O Pressure

Uses Linux PSI (`/proc/pressure/io`). When I/O is saturated, applies hard bandwidth caps to Tier 3 via `io.max`.

### 4. Media Mode

Polls your media server (or listens for a webhook trigger) and enforces playback-priority throttling on demand.

---

## CLI Reference

```bash
# Run the daemon
python3 adrg.py --config /etc/adrg/config.yaml

# Observe all decisions without applying any changes
python3 adrg.py --dry-run

# Validate your config and check which containers are running
python3 adrg.py --check-config

# Remove all active overrides and exit (called automatically by systemd on stop)
python3 adrg.py --cleanup

# Reload config without restarting the daemon
kill -HUP $(pidof adrg)
```

---

## HTTP API

ADRG exposes a lightweight HTTP server on `127.0.0.1:8765` by default (configurable).

### `GET /status`

Returns current governor state as JSON.

```bash
curl http://127.0.0.1:8765/status
```

```json
{
  "version": "1.0.0",
  "uptime_seconds": 3600.0,
  "dry_run": false,
  "media_mode_active": true,
  "media_provider": "jellyfin",
  "thermal_stage": 0,
  "memory_throttled": false,
  "io_throttled": false,
  "protected_containers": ["pihole"],
  "containers": {
    "tdarr": { "tier": 3, "paused_by": ["media_mode"], "cpu_max_by": [], "io_max_by": [] }
  }
}
```

### `POST /trigger`

Push an external event. Useful for Home Assistant automations, n8n workflows, or custom scripts.

```bash
# Force media mode on (e.g. from a Home Assistant automation when you sit down to watch something)
curl -X POST http://127.0.0.1:8765/trigger \
  -H "Content-Type: application/json" \
  -d '{"event": "media_start"}'

# Clear it when you're done
curl -X POST http://127.0.0.1:8765/trigger \
  -d '{"event": "media_stop"}'
```

| Event | Effect |
|---|---|
| `media_start` | Force media mode on (overrides provider polling) |
| `media_stop` | Clear the media mode override |
| `tier3_pause` | Manually pause all Tier 3 containers |
| `tier3_resume` | Resume all Tier 3 containers |

---

## Notifications

All backends are optional and independent. Configure any combination.

| Backend | Config key | Notes |
|---|---|---|
| Discord | `discord_webhook_url` | Standard Discord webhook URL |
| NTFY | `ntfy_url` + `ntfy_token` | Full topic URL, e.g. `https://ntfy.sh/my-topic` |
| Gotify | `gotify_url` + `gotify_token` | Base URL + application token |

---

## Security

### Host Install (systemd)

ADRG runs as root. It requires root to write cgroup control files — there is no privilege-separated alternative for this on Linux. The attack surface is limited to `config.yaml` and `adrg.env`. Both files are installed with `640` permissions (root-readable only) by `setup.sh`.

### Docker Install

```
--privileged
```

Running ADRG as a Docker container requires `--privileged`. This grants the container the same capabilities as a root process on the host — specifically, the ability to write to `/sys/fs/cgroup/`. This is a property of the Linux cgroup interface, not a flaw in ADRG.

**Recommendation:** Read the source before running any privileged container. ADRG is fully open source for this reason. The only files it writes to are cgroup control files under `/sys/fs/cgroup/` and its own log and state files.

### Docker Socket

ADRG mounts `/var/run/docker.sock` to pause, unpause, and restart containers. Anyone with access to the Docker socket has effective root on the host — this is a standard, well-understood trade-off for any Docker management tool.

---

## Tested Hardware

| Hardware | OS | Status |
|---|---|---|
| Raspberry Pi 5 (4GB / 8GB) | Raspberry Pi OS Bookworm (64-bit) | ✅ Primary platform |
| Intel N100 | Debian Bookworm | ✅ Confirmed working |

ADRG should work on any Linux system meeting the requirements. If you get it running on other hardware, feel free to open a PR to add it to this table.

---

## Licence

MIT — see `LICENCE` file.

---

*Built by [Aldertech](https://aldertech.uk)*
