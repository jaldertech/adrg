"""
ADRG - cgroup v2 interface module.

Reads and writes cgroup v2 control files for Docker containers.
Docker on modern Linux (cgroup v2 unified hierarchy) places container
cgroups at:
    /sys/fs/cgroup/system.slice/docker-<full_container_id>.scope/

A legacy fallback path is also checked for older cgroupfs setups:
    /sys/fs/cgroup/docker/<full_container_id>/

All operations are wrapped in error handling — a missing or
unwritable cgroup file must never crash the daemon.

DRY_RUN mode: set cgroup.DRY_RUN = True before starting the governor
to log all writes without applying them. Used by --dry-run.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adrg.cgroup")

CGROUP_BASE = Path("/sys/fs/cgroup")

# Set to True via --dry-run to log writes without applying them.
DRY_RUN: bool = False


def _container_cgroup_path(container_id: str) -> Optional[Path]:
    """
    Locate the cgroup directory for a Docker container.

    Docker under systemd typically places containers at:
        /sys/fs/cgroup/system.slice/docker-<id>.scope/
    but some setups use:
        /sys/fs/cgroup/docker/<id>/

    Returns the path if found, None otherwise.
    """
    # Primary: systemd slice layout
    scope_path = CGROUP_BASE / "system.slice" / f"docker-{container_id}.scope"
    if scope_path.is_dir():
        return scope_path

    # Fallback: legacy docker cgroup driver
    docker_path = CGROUP_BASE / "docker" / container_id
    if docker_path.is_dir():
        return docker_path

    logger.debug("cgroup path not found for container %s", container_id[:12])
    return None


def _read_cgroup_file(cgroup_path: Path, filename: str) -> Optional[str]:
    """Read a cgroup control file, returning its contents or None on failure."""
    filepath = cgroup_path / filename
    try:
        return filepath.read_text().strip()
    except (OSError, PermissionError) as exc:
        logger.warning("Failed to read %s: %s", filepath, exc)
        return None


def _write_cgroup_file(cgroup_path: Path, filename: str, value: str) -> bool:
    """Write a value to a cgroup control file. Returns True on success."""
    filepath = cgroup_path / filename
    if DRY_RUN:
        logger.info("[DRY RUN] Would write '%s' to %s", value, filepath)
        return True
    try:
        filepath.write_text(value)
        logger.debug("Wrote '%s' to %s", value, filepath)
        return True
    except (OSError, PermissionError) as exc:
        logger.error("Failed to write '%s' to %s: %s", value, filepath, exc)
        return False


# ── CPU controls ──────────────────────────────────────────────────────────

def set_cpu_weight(container_id: str, weight: int) -> bool:
    """
    Set cpu.weight for a container (1–10000, default 100).
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    weight = max(1, min(10000, weight))
    return _write_cgroup_file(cg, "cpu.weight", str(weight))


def set_cpu_max(container_id: str, quota_us: int, period_us: int = 100000) -> bool:
    """
    Set cpu.max (bandwidth limit).

    quota_us:  maximum CPU time in microseconds per period.
    period_us: period length in microseconds (default 100ms).

    To limit to 20% of one core: quota_us=20000, period_us=100000.
    To remove limit: pass quota_us=-1 which writes "max <period>".
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    quota_str = "max" if quota_us < 0 else str(quota_us)
    return _write_cgroup_file(cg, "cpu.max", f"{quota_str} {period_us}")


def remove_cpu_max(container_id: str) -> bool:
    """Remove cpu.max limit (set to unlimited)."""
    return set_cpu_max(container_id, quota_us=-1)


# ── I/O controls ─────────────────────────────────────────────────────────

def _get_block_device_major_minor() -> Optional[str]:
    """
    Determine the major:minor number of the primary block device.

    Reads /proc/mounts (or /host/proc/mounts in container) to find the
    device backing '/' and resolves its major:minor from /sys/class/block/.
    """
    mounts_path = "/host/proc/mounts" if os.path.exists("/host/proc/mounts") else "/proc/mounts"
    try:
        with open(mounts_path, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]

                    # Handle overlay or non-device roots (e.g. in containers)
                    if dev == "overlay" or not dev.startswith("/dev/"):
                        continue

                    # Resolve symlinks (e.g. /dev/root -> /dev/mmcblk0p2)
                    dev = os.path.realpath(dev)
                    dev_name = os.path.basename(dev)

                    # Strip partition number to get base device
                    # e.g. mmcblk0p2 -> mmcblk0, nvme0n1p1 -> nvme0n1, sda1 -> sda
                    base_dev = dev_name
                    if "mmcblk" in dev_name or "nvme" in dev_name:
                        idx = dev_name.rfind("p")
                        if idx > 0 and dev_name[idx + 1:].isdigit():
                            base_dev = dev_name[:idx]
                    else:
                        base_dev = dev_name.rstrip("0123456789")

                    dev_path = Path(f"/sys/class/block/{base_dev}/dev")
                    if dev_path.exists():
                        return dev_path.read_text().strip()

                    # If base device not found, try the partition device itself
                    dev_path = Path(f"/sys/class/block/{dev_name}/dev")
                    if dev_path.exists():
                        return dev_path.read_text().strip()

    except (OSError, IndexError) as exc:
        logger.warning("Failed to determine block device major:minor: %s", exc)

    return None


# Cache the block device — it won't change at runtime.
_BLOCK_DEV_MAJ_MIN: Optional[str] = None


def _get_cached_block_device() -> Optional[str]:
    """Return cached block device major:minor, resolving on first call."""
    global _BLOCK_DEV_MAJ_MIN
    if _BLOCK_DEV_MAJ_MIN is None:
        _BLOCK_DEV_MAJ_MIN = _get_block_device_major_minor()
        if _BLOCK_DEV_MAJ_MIN:
            logger.info("Root block device: %s", _BLOCK_DEV_MAJ_MIN)
        else:
            logger.warning("Could not determine root block device — io.max unavailable")
    return _BLOCK_DEV_MAJ_MIN


def set_io_weight(container_id: str, weight: int) -> bool:
    """
    Set io.weight for a container (1–10000, default 100).
    Writes to io.bfq.weight if BFQ scheduler is active, otherwise io.weight.
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    weight = max(1, min(10000, weight))

    # Try BFQ first (common on ARM/Bookworm), fall back to io.weight
    if (cg / "io.bfq.weight").exists():
        return _write_cgroup_file(cg, "io.bfq.weight", str(weight))
    return _write_cgroup_file(cg, "io.weight", f"default {weight}")


def set_io_max(container_id: str, read_bps: int, write_bps: int) -> bool:
    """
    Set io.max hard bandwidth limits for a container.

    read_bps/write_bps: bytes per second.
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    maj_min = _get_cached_block_device()
    if maj_min is None:
        return False
    value = f"{maj_min} rbps={read_bps} wbps={write_bps} riops=max wiops=max"
    return _write_cgroup_file(cg, "io.max", value)


def remove_io_max(container_id: str) -> bool:
    """Remove io.max limits (set all to max)."""
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    maj_min = _get_cached_block_device()
    if maj_min is None:
        return False
    value = f"{maj_min} rbps=max wbps=max riops=max wiops=max"
    return _write_cgroup_file(cg, "io.max", value)


# ── Memory controls ──────────────────────────────────────────────────────

def set_memory_high(container_id: str, limit_bytes: int) -> bool:
    """
    Set memory.high (soft limit — kernel reclaims aggressively above this).

    Pass -1 to remove the limit (writes "max").
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    value = "max" if limit_bytes < 0 else str(limit_bytes)
    return _write_cgroup_file(cg, "memory.high", value)


def set_memory_max(container_id: str, limit_bytes: int) -> bool:
    """
    Set memory.max (hard limit — OOM killer fires above this).

    Pass -1 to remove the limit (writes "max").
    """
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return False
    value = "max" if limit_bytes < 0 else str(limit_bytes)
    return _write_cgroup_file(cg, "memory.max", value)


def get_memory_current(container_id: str) -> Optional[int]:
    """Read memory.current (RSS) for a container in bytes."""
    cg = _container_cgroup_path(container_id)
    if cg is None:
        return None
    raw = _read_cgroup_file(cg, "memory.current")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Unexpected memory.current value: %s", raw)
        return None


# ── Bulk application ─────────────────────────────────────────────────────

def apply_tier_defaults(container_id: str, tier_config: dict) -> None:
    """
    Apply all baseline cgroup settings for a container from its tier config.

    Expected keys: cpu_weight, io_weight, memory_high (optional), memory_max (optional).
    """
    set_cpu_weight(container_id, tier_config.get("cpu_weight", 100))
    set_io_weight(container_id, tier_config.get("io_weight", 100))
    remove_cpu_max(container_id)
    remove_io_max(container_id)

    mem_high = tier_config.get("memory_high")
    if mem_high:
        set_memory_high(container_id, parse_memory_value(mem_high))
    else:
        set_memory_high(container_id, -1)

    mem_max = tier_config.get("memory_max")
    if mem_max:
        set_memory_max(container_id, parse_memory_value(mem_max))
    else:
        set_memory_max(container_id, -1)


def parse_memory_value(value) -> int:
    """
    Parse a memory value like '2G', '1.5G', '512M' into bytes.
    Also accepts raw integers (already in bytes).
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    value = str(value).strip().upper()
    multipliers = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if value[-1] in multipliers:
        return int(float(value[:-1]) * multipliers[value[-1]])
    return int(value)
