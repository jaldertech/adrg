#!/usr/bin/env python3
"""
Aldertech Dynamic Resource Governor (ADRG)

Main daemon: runs the Sentinel monitoring loop, evaluates enforcer rules
(Media Mode, Thermal Protection, Memory Pressure, I/O Saturation),
and applies cgroup v2 constraints to Docker containers by tier.

Usage:
  adrg.py [--config PATH] [--dry-run] [--check-config] [--cleanup]

  --config PATH     Path to config.yaml (default: /etc/adrg/config.yaml)
  --dry-run         Log all actions without applying cgroup writes or
                    container lifecycle operations. Ideal for tuning config.
  --check-config    Validate config.yaml and check Docker container
                    availability, then exit.
  --cleanup         Remove all cgroup overrides and exit. Used by the
                    systemd ExecStopPost directive.
"""

import argparse
import fnmatch
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml
import docker as docker_sdk
from docker.errors import DockerException

from modules import cgroup, docker_control, sensors
from modules.docker_control import ContainerInfo, DockerControl
from modules.media_client import create_media_client
from modules.notifier import Notifier
from modules.preflight import run_preflight
from modules.qbittorrent_client import QBittorrentClient
from modules.state import StateManager
from modules.webhook_server import WebhookServer

# ── Constants ────────────────────────────────────────────────────────────

VERSION = "1.0.1"
MBPS = 1_000_000  # Bytes per second per megabit (decimal)
DEFAULT_CONFIG = "/etc/adrg/config.yaml"

# ── Logging setup ────────────────────────────────────────────────────────

logger = logging.getLogger("adrg")


def setup_logging(config: dict) -> None:
    """Configure rotating file + console logging."""
    general = config.get("general", {})
    log_file = general.get("log_file", "/var/log/adrg/adrg.log")
    max_bytes = general.get("log_max_bytes", 10 * 1024 * 1024)
    backup_count = general.get("log_backup_count", 3)

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger("adrg")
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


# ── Configuration ────────────────────────────────────────────────────────

def load_config(path: str = DEFAULT_CONFIG) -> dict:
    """Load and validate the YAML configuration file with env var expansion."""
    try:
        with open(path, "r") as f:
            content = f.read()
        content = os.path.expandvars(content)
        config = yaml.safe_load(content)
        if not isinstance(config, dict):
            raise ValueError("Config root must be a mapping")
        logger.info("Configuration loaded from %s", path)
        return config
    except (OSError, yaml.YAMLError, ValueError) as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)


# ── Tier helpers ─────────────────────────────────────────────────────────

def build_tier_map(config: dict) -> Tuple[Dict[str, int], List[Tuple[str, int]]]:
    """
    Build container-to-tier lookups from config.

    Returns:
        tier_map: {exact_container_name: tier_number} for O(1) lookups.
        patterns: [(glob_pattern, tier_number)] for wildcard matching.

    Container names containing *, ?, or [ are treated as glob patterns
    and matched against running container names at runtime.
    """
    tier_map: Dict[str, int] = {}
    patterns: List[Tuple[str, int]] = []

    for tier_key, tier_cfg in config.get("tiers", {}).items():
        tier_num = int(tier_key)
        for name in tier_cfg.get("containers", []):
            if any(c in name for c in ("*", "?", "[", "]")):
                patterns.append((name, tier_num))
            else:
                tier_map[name] = tier_num

    return tier_map, patterns


def get_container_tier(
    name: str,
    tier_map: Dict[str, int],
    patterns: List[Tuple[str, int]],
) -> Optional[int]:
    """
    Return the tier number for a container name.

    Checks exact matches first, then falls back to glob patterns.
    Returns None if the container is not managed by ADRG.
    """
    if name in tier_map:
        return tier_map[name]
    for pattern, tier_num in patterns:
        if fnmatch.fnmatch(name, pattern):
            return tier_num
    return None


def containers_in_tier(config: dict, tier: int) -> List[str]:
    """Return all container names/patterns belonging to a given tier."""
    tier_cfg = config.get("tiers", {}).get(
        tier, config.get("tiers", {}).get(str(tier), {})
    )
    return tier_cfg.get("containers", [])


def tier_config(config: dict, tier: int) -> dict:
    """Return the full tier configuration dict."""
    return config.get("tiers", {}).get(
        tier, config.get("tiers", {}).get(str(tier), {})
    )


def resolve_tier_containers(
    tier_names: List[str],
    running: Dict[str, ContainerInfo],
) -> List[str]:
    """
    Resolve a tier's names/patterns against the set of running containers.

    Returns a deduplicated list of exact running container names that match
    any name or glob pattern in tier_names.
    """
    seen: Set[str] = set()
    result: List[str] = []

    for name in tier_names:
        if any(c in name for c in ("*", "?", "[", "]")):
            for running_name in running:
                if running_name not in seen and fnmatch.fnmatch(running_name, name):
                    seen.add(running_name)
                    result.append(running_name)
        else:
            if name in running and name not in seen:
                seen.add(name)
                result.append(name)

    return result


# ── systemd watchdog ─────────────────────────────────────────────────────

def _sd_notify(state: str) -> None:
    """Send an sd_notify message if running under systemd."""
    try:
        import systemd.daemon
        systemd.daemon.notify(state)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("sd_notify failed: %s", exc)


# ── Config validation ─────────────────────────────────────────────────────

def check_config_cmd(config: dict) -> bool:
    """
    Validate configuration and check the Docker environment.

    Prints a human-readable report and returns True if everything looks good.
    Used by the --check-config flag.
    """
    ok = True
    print(f"\nADRG {VERSION} — Configuration Check\n{'=' * 40}")

    # Tier structure
    tiers = config.get("tiers", {})
    if not tiers:
        print("ERROR: No tiers defined in config.")
        ok = False
    else:
        print(f"\nTiers ({len(tiers)} defined):")
        for tier_key, tier_cfg in tiers.items():
            containers = tier_cfg.get("containers", [])
            cpu_w = tier_cfg.get("cpu_weight", 100)
            if not (1 <= int(cpu_w) <= 10000):
                print(f"  ERROR: Tier {tier_key} cpu_weight {cpu_w} is out of range (1–10000)")
                ok = False
            print(f"  Tier {tier_key} ({tier_cfg.get('name', '?')}): "
                  f"{len(containers)} container(s), cpu_weight={cpu_w}")

    # Protected containers
    protected = config.get("protected_containers", [])
    if protected:
        print(f"\nProtected containers ({len(protected)}): {', '.join(protected)}")

    # Media mode
    media_cfg = config.get("media_mode", {})
    if media_cfg.get("enabled"):
        provider = media_cfg.get("provider", "none")
        api_key = media_cfg.get("api_key", "")
        print(f"\nMedia mode: enabled (provider={provider})")
        if provider in ("jellyfin", "plex") and not api_key:
            print(f"  WARNING: media_mode.api_key is empty — {provider} polling will not work.")

    # Docker check
    print("\nDocker:")
    try:
        client = docker_sdk.from_env()
        client.ping()
        running_names = {c.name for c in client.containers.list()}
        print(f"  Connected — {len(running_names)} container(s) currently running.")

        tier_map, patterns = build_tier_map(config)
        all_configured = set(tier_map.keys())
        for pat, _ in patterns:
            all_configured.add(pat)

        not_running = []
        for name in all_configured:
            if any(c in name for c in ("*", "?", "[", "]")):
                if not any(fnmatch.fnmatch(r, name) for r in running_names):
                    not_running.append(f"{name} (pattern, no match)")
            elif name not in running_names:
                not_running.append(name)

        if not_running:
            print(f"\n  WARNING: {len(not_running)} configured container(s) not currently running:")
            for name in sorted(not_running):
                print(f"    - {name}")
        else:
            print("  All configured containers are running.")

        client.close()
    except DockerException as exc:
        print(f"  ERROR: Cannot connect to Docker: {exc}")
        ok = False

    # Notification backends
    notif_cfg = config.get("notifications", {})
    backends = []
    if notif_cfg.get("discord_webhook_url", "").startswith("http"):
        backends.append("Discord")
    if notif_cfg.get("ntfy_url", "").startswith("http"):
        backends.append("NTFY")
    if notif_cfg.get("gotify_url", "").startswith("http"):
        backends.append("Gotify")
    if backends:
        print(f"\nNotification backends: {', '.join(backends)}")
    else:
        print("\nNotifications: none configured")

    print(f"\n{'=' * 40}")
    if ok:
        print("Config check passed.\n")
    else:
        print("Config check FAILED — see errors above.\n")

    return ok


# ── The Governor ─────────────────────────────────────────────────────────

class Governor:
    """
    The main ADRG governor — combines Sentinel (monitoring) and
    Enforcer (action) into a single loop.
    """

    def __init__(self, config: dict, config_path: str = DEFAULT_CONFIG):
        self.config = config
        self._config_path = config_path
        self.tier_map, self.tier_patterns = build_tier_map(config)
        self._start_time = time.monotonic()

        general = config.get("general", {})
        self.poll_interval = general.get("poll_interval_seconds", 5)
        state_file = general.get("state_file", "/run/adrg/state.json")

        # Explicit protected list from config
        self._protected_containers: Set[str] = set(
            config.get("protected_containers", [])
        )
        # Tier 0 containers are automatically protected — they are never
        # paused or restarted regardless of any pressure rule.
        # This is enforced via _is_protected() at rule evaluation time.

        self.state = StateManager(state_file)
        self.docker = DockerControl()

        notif_cfg = config.get("notifications", {})
        self.notifier = Notifier(
            webhook_url=notif_cfg.get("discord_webhook_url", ""),
            ntfy_url=notif_cfg.get("ntfy_url", ""),
            ntfy_token=notif_cfg.get("ntfy_token", ""),
            gotify_url=notif_cfg.get("gotify_url", ""),
            gotify_token=notif_cfg.get("gotify_token", ""),
            enabled_events=notif_cfg.get("notify_on", []),
        )

        # Media mode
        media_cfg = config.get("media_mode", {})
        self._media_enabled = media_cfg.get("enabled", False)
        self._media_provider = media_cfg.get("provider", "none").lower()
        self.media_client = create_media_client(media_cfg) if self._media_enabled else None

        # qBittorrent download throttle
        throttle_cfg = media_cfg.get("download_throttle", {})
        self._qb_client: Optional[QBittorrentClient] = None
        self._qb_limit_bytes = 0
        if self._media_enabled and throttle_cfg.get("enabled", False):
            self._qb_client = QBittorrentClient(
                url=throttle_cfg.get("url", "http://qbittorrent:8080"),
                username=throttle_cfg.get("username", "admin"),
                password=throttle_cfg.get("password", ""),
            )
            self._qb_limit_bytes = int(
                throttle_cfg.get("limit_mbps", 5) * MBPS
            )

        # Webhook / status HTTP server
        self._webhook_server: Optional[WebhookServer] = None
        http_cfg = general.get("http_server", {})
        if http_cfg.get("enabled", True):
            self._webhook_server = WebhookServer(
                governor=self,
                host=http_cfg.get("host", "127.0.0.1"),
                port=int(http_cfg.get("port", 8765)),
            )

        # Runtime state flags
        self._running = True
        self._media_active = False
        self._webhook_media_active = False  # Controlled via POST /trigger
        self._thermal_stage = 0             # 0=normal, 1=stage1, 2=stage2
        self._memory_throttled = False
        self._io_throttled = False
        self._io_recovery_since: Optional[float] = None
        self._applied_baselines: Dict[str, str] = {}  # name -> container_id

    # ── Signal handling ───────────────────────────────────────────────

    def _handle_sigterm(self, signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        self._running = False

    def _handle_sighup(self, signum, frame):
        logger.info("SIGHUP received — reloading configuration")
        try:
            new_config = load_config(self._config_path)
            self.config = new_config
            self.tier_map, self.tier_patterns = build_tier_map(new_config)
            self._protected_containers = set(new_config.get("protected_containers", []))

            media_cfg = new_config.get("media_mode", {})
            self._media_enabled = media_cfg.get("enabled", False)
            self._media_provider = media_cfg.get("provider", "none").lower()
            self.media_client = create_media_client(media_cfg) if self._media_enabled else None

            notif_cfg = new_config.get("notifications", {})
            self.notifier = Notifier(
                webhook_url=notif_cfg.get("discord_webhook_url", ""),
                ntfy_url=notif_cfg.get("ntfy_url", ""),
                ntfy_token=notif_cfg.get("ntfy_token", ""),
                gotify_url=notif_cfg.get("gotify_url", ""),
                gotify_token=notif_cfg.get("gotify_token", ""),
                enabled_events=notif_cfg.get("notify_on", []),
            )
            logger.info("Configuration reloaded successfully")
        except Exception as exc:
            logger.error("Config reload failed (keeping old config): %s", exc)

    # ── Protection check ─────────────────────────────────────────────

    def _is_protected(self, name: str) -> bool:
        """
        Return True if a container must never be paused or restarted.

        A container is protected if:
          - It appears in the protected_containers config list, OR
          - It belongs to Tier 0 (Core Infra — always implicitly protected).
        """
        if name in self._protected_containers:
            return True
        tier = get_container_tier(name, self.tier_map, self.tier_patterns)
        return tier == 0

    # ── Baseline application ──────────────────────────────────────────

    def apply_baselines(self, running: Dict[str, ContainerInfo]) -> None:
        """Apply baseline cgroup settings to all managed running containers."""
        for name, info in running.items():
            tier_num = get_container_tier(name, self.tier_map, self.tier_patterns)
            if tier_num is None:
                continue
            tc = tier_config(self.config, tier_num)
            cgroup.apply_tier_defaults(info.container_id, tc)

    # ── Status (for /status endpoint) ────────────────────────────────

    def get_status(self) -> dict:
        """Return current governor state as a JSON-serialisable dict."""
        containers = {}
        for name, cs in self.state.get_all_containers().items():
            tier = get_container_tier(name, self.tier_map, self.tier_patterns)
            containers[name] = {
                "tier": tier,
                "paused_by": sorted(cs.paused_by),
                "cpu_max_by": sorted(cs.cpu_max_applied_by),
                "io_max_by": sorted(cs.io_max_applied_by),
                "memory_high_override_bytes": cs.memory_high_override,
            }
        return {
            "version": VERSION,
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "dry_run": cgroup.DRY_RUN,
            "media_mode_active": self._media_active,
            "media_provider": self._media_provider,
            "thermal_stage": self._thermal_stage,
            "memory_throttled": self._memory_throttled,
            "io_throttled": self._io_throttled,
            "protected_containers": sorted(self._protected_containers),
            "note_tier0_auto_protected": "All Tier 0 containers are implicitly protected.",
            "containers": containers,
        }

    # ── Webhook trigger processing ────────────────────────────────────

    def _process_webhook_triggers(self, running: Dict[str, ContainerInfo]) -> None:
        """Drain and apply any pending webhook trigger events."""
        if self._webhook_server is None:
            return
        for event in self._webhook_server.drain_triggers():
            self._handle_webhook_trigger(event, running)

    def _handle_webhook_trigger(
        self,
        event: str,
        running: Dict[str, ContainerInfo],
    ) -> None:
        logger.info("Processing webhook trigger: %s", event)
        if event == "media_start":
            self._webhook_media_active = True
        elif event == "media_stop":
            self._webhook_media_active = False
        elif event == "tier3_pause":
            reason = "webhook_manual"
            for name in resolve_tier_containers(containers_in_tier(self.config, 3), running):
                if name not in self._protected_containers:
                    if not self.state.should_be_paused(name):
                        self.docker.pause_container(name)
                    self.state.add_pause(name, reason)
        elif event == "tier3_resume":
            reason = "webhook_manual"
            for name in resolve_tier_containers(containers_in_tier(self.config, 3), running):
                self.state.remove_pause(name, reason)
                if not self.state.should_be_paused(name) and name in running:
                    self.docker.unpause_container(name)

    # ── Rule 1: Media Mode ────────────────────────────────────────────

    def _evaluate_media_mode(self, running: Dict[str, ContainerInfo]) -> None:
        """Evaluate and enforce Media Mode based on active streams."""
        if not self._media_enabled:
            return

        media_cfg = self.config.get("media_mode", {})
        reason = "media_mode"

        # Determine active streams based on provider
        if self._media_provider == "webhook":
            active_streams = 1 if self._webhook_media_active else 0
        elif self.media_client is not None:
            active_streams = self.media_client.get_active_video_streams()
        else:
            return

        if active_streams > 0:
            if not self._media_active:
                self._media_active = True
                self.state.media_cooldown_start = 0.0
                self.notifier.notify(
                    "media_mode_activated",
                    f"{active_streams} active stream(s) — throttling Tier 2/3",
                )
                # Throttle download client if configured
                if self._qb_client is not None:
                    self._qb_client.set_download_limit(self._qb_limit_bytes)

            # Pause Tier 3 (excluding protected containers)
            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                if self._is_protected(name):
                    logger.debug("Media mode: skipping protected container %s", name)
                    continue
                if not self.state.should_be_paused(name):
                    self.docker.pause_container(name)
                self.state.add_pause(name, reason)

            # Throttle Tier 2: cpu.max + io.max (excluding protected containers)
            cpu_pct = media_cfg.get("tier2_cpu_max_percent", 20)
            num_cores = os.cpu_count() or 4
            quota_us = int((cpu_pct / 100.0) * 100000 * num_cores)

            io_read = media_cfg.get("tier2_io_max_read_mbps", 10) * MBPS
            io_write = media_cfg.get("tier2_io_max_write_mbps", 5) * MBPS

            for name in resolve_tier_containers(
                containers_in_tier(self.config, 2), running
            ):
                if self._is_protected(name):
                    logger.debug("Media mode: skipping protected container %s", name)
                    continue
                cid = running[name].container_id
                self.state.add_cpu_max(name, reason)
                cgroup.set_cpu_max(cid, quota_us)
                self.state.add_io_max(name, reason)
                cgroup.set_io_max(cid, io_read, io_write)

        else:
            # Streams stopped — start or continue cooldown
            if self._media_active:
                if self.state.media_cooldown_start == 0.0:
                    self.state.media_cooldown_start = time.time()
                    logger.info("Media Mode: streams ended, cooldown started")

                cooldown = media_cfg.get("cooldown_seconds", 60)
                if self.state.media_cooldown_elapsed() >= cooldown:
                    self._media_active = False
                    self.state.media_cooldown_start = 0.0

                    for name in resolve_tier_containers(
                        containers_in_tier(self.config, 3), running
                    ):
                        self.state.remove_pause(name, reason)
                        if not self.state.should_be_paused(name) and name in running:
                            self.docker.unpause_container(name)

                    for name in resolve_tier_containers(
                        containers_in_tier(self.config, 2), running
                    ):
                        self.state.remove_cpu_max(name, reason)
                        self.state.remove_io_max(name, reason)
                        if name in running:
                            cid = running[name].container_id
                            if not self.state.should_have_cpu_max(name):
                                cgroup.remove_cpu_max(cid)
                            if not self.state.should_have_io_max(name):
                                cgroup.remove_io_max(cid)

                    self.notifier.notify(
                        "media_mode_deactivated",
                        "Cooldown complete — Tier 2/3 restored",
                    )
                    # Remove download throttle
                    if self._qb_client is not None:
                        self._qb_client.remove_download_limit()

    # ── Rule 2: Thermal Protection ───────────────────────────────────

    def _evaluate_thermal(
        self, temp_c: float, running: Dict[str, ContainerInfo]
    ) -> None:
        """Evaluate thermal thresholds and pause tiers as needed."""
        thermal_cfg = self.config.get("thermal", {})
        if not thermal_cfg.get("enabled", False):
            return

        warn_c = thermal_cfg.get("warn_temp_c", 70)
        stage1_c = thermal_cfg.get("stage1_temp_c", 75)
        stage2_c = thermal_cfg.get("stage2_temp_c", 80)
        recovery_c = thermal_cfg.get("recovery_temp_c", 65)
        recovery_hold = thermal_cfg.get("recovery_hold_seconds", 30)

        prev_stage = self._thermal_stage

        if temp_c >= stage2_c:
            self._thermal_stage = 2
            self.state.thermal_recovery_since = 0.0
        elif temp_c >= stage1_c:
            self._thermal_stage = 1
            self.state.thermal_recovery_since = 0.0
        elif temp_c >= warn_c:
            if self._thermal_stage == 0:
                logger.warning("Thermal warning: %.1f°C", temp_c)
            self.state.thermal_recovery_since = 0.0
        elif temp_c < recovery_c:
            if self._thermal_stage > 0 and self.state.thermal_recovery_since == 0.0:
                self.state.thermal_recovery_since = time.time()
                logger.info(
                    "Thermal recovery started (%.1f°C < %d°C)", temp_c, recovery_c
                )
            if (self._thermal_stage > 0
                    and self.state.thermal_recovery_elapsed() >= recovery_hold):
                self._thermal_stage = 0
                self.state.thermal_recovery_since = 0.0
                logger.info("Thermal recovery complete — restoring containers")
        else:
            self.state.thermal_recovery_since = 0.0

        # Notify on stage changes
        if self._thermal_stage != prev_stage:
            if self._thermal_stage >= 1 and prev_stage < 1:
                self.notifier.notify(
                    "thermal_stage1",
                    f"Temperature {temp_c:.1f}°C — pausing Tier 3",
                )
            if self._thermal_stage >= 2 and prev_stage < 2:
                self.notifier.notify(
                    "thermal_stage2",
                    f"Temperature {temp_c:.1f}°C — pausing Tier 2 + 3",
                )

        # Enforce Tier 3 pause
        if self._thermal_stage >= 1:
            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                if self._is_protected(name):
                    continue
                if not self.state.should_be_paused(name):
                    self.docker.pause_container(name)
                self.state.add_pause(name, "thermal_stage1")

        # Enforce Tier 2 pause
        if self._thermal_stage >= 2:
            for name in resolve_tier_containers(
                containers_in_tier(self.config, 2), running
            ):
                if self._is_protected(name):
                    continue
                if not self.state.should_be_paused(name):
                    self.docker.pause_container(name)
                self.state.add_pause(name, "thermal_stage2")

        # Restore on recovery
        if self._thermal_stage < 1 and prev_stage >= 1:
            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                self.state.remove_pause(name, "thermal_stage1")
                if not self.state.should_be_paused(name) and name in running:
                    self.docker.unpause_container(name)

        if self._thermal_stage < 2 and prev_stage >= 2:
            for name in resolve_tier_containers(
                containers_in_tier(self.config, 2), running
            ):
                self.state.remove_pause(name, "thermal_stage2")
                if not self.state.should_be_paused(name) and name in running:
                    self.docker.unpause_container(name)

    # ── Rule 3: Memory Pressure ──────────────────────────────────────

    def _evaluate_memory_pressure(
        self,
        psi: sensors.PSIMetrics,
        running: Dict[str, ContainerInfo],
    ) -> None:
        """Evaluate memory PSI and apply two-stage OOM prevention."""
        mem_cfg = self.config.get("memory_pressure", {})
        if not mem_cfg.get("enabled", False):
            return

        pressure_threshold = mem_cfg.get("some_avg10_threshold", 50)
        critical_threshold = mem_cfg.get("critical_avg60_threshold", 40)
        emergency_threshold = mem_cfg.get("emergency_full_avg10_threshold", 25)
        cooldown = mem_cfg.get("restart_cooldown_seconds", 300)
        reduction = mem_cfg.get("memory_high_reduction_factor", 0.75)
        reason = "memory_pressure"

        # Stage 1: Squeeze memory.high on Tier 3
        if psi.some_avg10 > pressure_threshold:
            if not self._memory_throttled:
                logger.warning(
                    "Memory pressure: some avg10=%.2f > %d — squeezing Tier 3 memory.high",
                    psi.some_avg10, pressure_threshold,
                )
                self._memory_throttled = True

            tc = tier_config(self.config, 3)
            mem_max_raw = tc.get("memory_max", "3G")
            floor = int(cgroup.parse_memory_value(mem_max_raw) * 0.5)

            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                cid = running[name].container_id
                current_rss = cgroup.get_memory_current(cid)
                if current_rss and current_rss > 0:
                    target_high = int(current_rss * reduction)
                    new_high = max(target_high, floor)

                    cs = self.state._get(name)
                    if cs.min_memory_high_applied is None:
                        cs.min_memory_high_applied = new_high
                    else:
                        cs.min_memory_high_applied = min(cs.min_memory_high_applied, new_high)

                    if cs.memory_high_override != cs.min_memory_high_applied:
                        logger.warning(
                            "Memory Stage 1: Squeezing %s memory.high to %dMB (floor: %dMB)",
                            name,
                            cs.min_memory_high_applied // (1024 * 1024),
                            floor // (1024 * 1024),
                        )
                        self.state.set_memory_high_override(
                            name, cs.min_memory_high_applied, reason
                        )
                        cgroup.set_memory_high(cid, cs.min_memory_high_applied)
        else:
            if self._memory_throttled:
                logger.info("Memory pressure recovered — restoring Tier 3 memory.high")
                self._memory_throttled = False

            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                cs = self.state._get(name)
                cs.min_memory_high_applied = None
                if self.state.has_memory_high_override(name):
                    self.state.clear_memory_high_override(name, reason)
                    if not self.state.has_memory_high_override(name) and name in running:
                        tc = tier_config(self.config, 3)
                        mem_high = tc.get("memory_high")
                        cid = running[name].container_id
                        if mem_high:
                            cgroup.set_memory_high(
                                cid, cgroup.parse_memory_value(mem_high)
                            )
                        else:
                            cgroup.set_memory_high(cid, -1)

        # Stage 3: Emergency restart (checked before Stage 2)
        if psi.full_avg10 > emergency_threshold:
            target = self._find_restart_target(3, running, cooldown)
            if target:
                self.notifier.notify(
                    "memory_emergency_restart",
                    f"EMERGENCY: Memory PSI full avg10={psi.full_avg10:.2f} — "
                    f"restarting {target}",
                )
                self.state.record_restart(target)
                self.state.clear_memory_high_override(target, reason)
                self.docker.restart_container_async(target)
            else:
                target2 = self._find_restart_target(2, running, cooldown)
                if target2:
                    self.notifier.notify(
                        "memory_emergency_restart",
                        f"EMERGENCY ESCALATION: PSI full avg10={psi.full_avg10:.2f} — "
                        f"Tier 3 exhausted, restarting Tier 2 container {target2}",
                    )
                    self.state.record_restart(target2)
                    self.docker.restart_container_async(target2)

        # Stage 2: Critical restart
        elif psi.some_avg60 > critical_threshold:
            target = self._find_restart_target(3, running, cooldown)
            if target:
                self.notifier.notify(
                    "memory_critical_restart",
                    f"Memory PSI critical (some avg60={psi.some_avg60:.2f}) — "
                    f"restarting {target}",
                )
                self.state.record_restart(target)
                self.state.clear_memory_high_override(target, reason)
                self.docker.restart_container_async(target)

    def _find_restart_target(
        self,
        tier: int,
        running: Dict[str, ContainerInfo],
        cooldown: int,
    ) -> Optional[str]:
        """
        Find the highest-RSS container in a tier eligible for restart.

        Respects protected_containers and per-container restart cooldowns.
        """
        candidates = []
        for name in resolve_tier_containers(
            containers_in_tier(self.config, tier), running
        ):
            if self._is_protected(name):
                logger.debug(
                    "Skipping %s for restart (protected container)", name
                )
                continue
            if not self.state.can_restart(name, cooldown):
                logger.debug("Skipping %s (restart cooldown active)", name)
                continue
            mem = cgroup.get_memory_current(running[name].container_id) or 0
            candidates.append((name, mem))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # ── Rule 4: I/O Pressure ─────────────────────────────────────────

    def _evaluate_io_pressure(
        self,
        psi: sensors.PSIMetrics,
        running: Dict[str, ContainerInfo],
    ) -> None:
        """Evaluate I/O PSI and apply hard bandwidth caps to Tier 3."""
        io_cfg = self.config.get("io_pressure", {})
        if not io_cfg.get("enabled", False):
            return

        trigger = io_cfg.get("trigger_avg10_threshold", 60)
        recovery = io_cfg.get("recovery_avg10_threshold", 20)
        recovery_hold = io_cfg.get("recovery_hold_seconds", 30)
        reason = "io_saturation"

        if psi.some_avg10 > trigger:
            if not self._io_throttled:
                logger.warning(
                    "I/O saturation: some avg10=%.2f > %d — capping Tier 3 I/O",
                    psi.some_avg10, trigger,
                )
                self._io_throttled = True
            self._io_recovery_since = None

            io_read = io_cfg.get("tier3_io_max_read_mbps", 5) * MBPS
            io_write = io_cfg.get("tier3_io_max_write_mbps", 2) * MBPS

            for name in resolve_tier_containers(
                containers_in_tier(self.config, 3), running
            ):
                cid = running[name].container_id
                self.state.add_io_max(name, reason)
                cgroup.set_io_max(cid, io_read, io_write)

        elif self._io_throttled and psi.some_avg10 < recovery:
            if self._io_recovery_since is None:
                self._io_recovery_since = time.monotonic()
                logger.info(
                    "I/O recovery started (%.2f < %d)", psi.some_avg10, recovery
                )

            if time.monotonic() - self._io_recovery_since >= recovery_hold:
                self._io_throttled = False
                self._io_recovery_since = None
                logger.info("I/O pressure recovered — removing Tier 3 I/O caps")

                for name in resolve_tier_containers(
                    containers_in_tier(self.config, 3), running
                ):
                    self.state.remove_io_max(name, reason)
                    if not self.state.should_have_io_max(name) and name in running:
                        cgroup.remove_io_max(running[name].container_id)
        else:
            self._io_recovery_since = None

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """
        Remove all cgroup overrides and unpause all containers.
        Called on shutdown to avoid leaving containers in a throttled state.
        """
        logger.info("Cleanup: restoring all containers to defaults")
        running = self.docker.list_running_containers_fast()

        for name, info in running.items():
            tier_num = get_container_tier(name, self.tier_map, self.tier_patterns)
            if tier_num is None:
                continue
            if self.state.should_be_paused(name):
                self.docker.unpause_container(name)
            tc = tier_config(self.config, tier_num)
            cgroup.apply_tier_defaults(info.container_id, tc)

        if self._qb_client is not None and self._media_active:
            self._qb_client.remove_download_limit()

        self.state.clear_all()
        self.state.save()
        self.notifier.notify("daemon_stop", "ADRG daemon stopped — all overrides cleared")

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Main Sentinel loop."""
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)
        signal.signal(signal.SIGHUP, self._handle_sighup)

        if self._webhook_server is not None:
            self._webhook_server.start()

        self.notifier.notify("daemon_start", f"ADRG {VERSION} daemon started")
        _sd_notify("READY=1")

        running = self.docker.list_running_containers_fast()
        if running:
            self.apply_baselines(running)
            logger.info("Baselines applied to %d managed containers", len(running))

        while self._running:
            tick_start = time.monotonic()
            try:
                self._tick()
            except Exception as exc:
                logger.error("Tick failed: %s", exc, exc_info=True)

            _sd_notify("WATCHDOG=1")

            elapsed = time.monotonic() - tick_start
            sleep_time = max(0, self.poll_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Graceful shutdown
        if self._webhook_server is not None:
            self._webhook_server.stop()
        self.cleanup()
        self.docker.close()
        _sd_notify("STOPPING=1")

    def _tick(self) -> None:
        """Single iteration of the Sentinel loop."""
        sensor_data = sensors.read_all()
        running = self.docker.list_running_containers_fast()

        if not running:
            logger.debug("No containers running (or Docker unreachable)")
            return

        # Process any external trigger events before evaluating rules
        self._process_webhook_triggers(running)

        # Apply baselines to new or restarted containers
        self._applied_baselines = {
            name: cid
            for name, cid in self._applied_baselines.items()
            if name in running
        }
        for name, info in running.items():
            tier_num = get_container_tier(name, self.tier_map, self.tier_patterns)
            if tier_num is None:
                continue
            tracked_id = self._applied_baselines.get(name)
            if tracked_id != info.container_id:
                tc = tier_config(self.config, tier_num)
                cgroup.apply_tier_defaults(info.container_id, tc)
                self._applied_baselines[name] = info.container_id
                logger.info(
                    "Applied cgroup baselines to %s (%s)", name, info.container_id[:12]
                )

        # Evaluate rules — most critical first
        self._evaluate_thermal(sensor_data.cpu_temp_c, running)
        self._evaluate_memory_pressure(sensor_data.memory_psi, running)
        self._evaluate_io_pressure(sensor_data.io_psi, running)
        self._evaluate_media_mode(running)

        self.state.save()


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Aldertech Dynamic Resource Governor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  adrg.py                          # Run daemon (config from /etc/adrg/config.yaml)\n"
            "  adrg.py --config ./config.yaml   # Run with a custom config path\n"
            "  adrg.py --dry-run                # Observe without applying any changes\n"
            "  adrg.py --check-config           # Validate config and Docker environment\n"
            "  adrg.py --cleanup                # Remove all overrides and exit\n"
        ),
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log all actions without writing to cgroups or touching containers",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config.yaml and check Docker container availability, then exit",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove all cgroup overrides and exit (used by systemd ExecStopPost)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    if args.check_config:
        ok = check_config_cmd(config)
        sys.exit(0 if ok else 1)

    if args.dry_run:
        cgroup.DRY_RUN = True
        docker_control.DRY_RUN = True
        logger.info(
            "DRY RUN MODE: all cgroup writes and container operations will be "
            "logged but not applied."
        )

    if not args.cleanup and not args.dry_run:
        run_preflight()

    governor = Governor(config, args.config)

    if args.cleanup:
        governor.cleanup()
        return

    governor.run()


if __name__ == "__main__":
    main()
