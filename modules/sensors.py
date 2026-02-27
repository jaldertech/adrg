"""
ADRG - System sensor readings module.

Reads CPU temperature, CPU load, and Pressure Stall Information (PSI)
directly from /proc and /sys. No psutil dependency.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adrg.sensors")


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class PSIMetrics:
    """Parsed PSI (Pressure Stall Information) values."""
    some_avg10: float = 0.0
    some_avg60: float = 0.0
    some_avg300: float = 0.0
    full_avg10: float = 0.0
    full_avg60: float = 0.0
    full_avg300: float = 0.0


@dataclass
class SystemSensors:
    """Snapshot of all system sensor readings for one tick."""
    cpu_temp_c: float = 0.0
    cpu_load_percent: float = 0.0
    memory_psi: PSIMetrics = field(default_factory=PSIMetrics)
    io_psi: PSIMetrics = field(default_factory=PSIMetrics)


# ── CPU Temperature ──────────────────────────────────────────────────────

_THERMAL_ZONE_DIR = Path("/sys/class/thermal")


def read_cpu_temp() -> float:
    """
    Read CPU temperature in degrees Celsius.

    Takes the maximum across all thermal zones to catch the hottest spot.
    Returns 0.0 if no zones are readable (logged as warning).
    """
    max_temp = 0.0
    zones_found = False

    try:
        if not _THERMAL_ZONE_DIR.exists():
            logger.warning("Thermal zone directory not found: %s", _THERMAL_ZONE_DIR)
            return 0.0

        for entry in _THERMAL_ZONE_DIR.iterdir():
            if not entry.name.startswith("thermal_zone"):
                continue
            temp_file = entry / "temp"
            if not temp_file.exists():
                continue
            try:
                raw = temp_file.read_text().strip()
                temp_c = int(raw) / 1000.0
                max_temp = max(max_temp, temp_c)
                zones_found = True
            except (ValueError, OSError) as exc:
                logger.debug("Failed to read %s: %s", temp_file, exc)
    except OSError as exc:
        logger.warning("Error scanning thermal zones: %s", exc)

    if not zones_found:
        logger.warning("No thermal zones readable")

    return max_temp


# ── CPU Load ─────────────────────────────────────────────────────────────

# Previous tick's CPU counters for delta calculation.
_prev_idle: int = 0
_prev_total: int = 0


def read_cpu_load() -> float:
    """
    Calculate CPU utilisation as a percentage since the last call.

    Reads /proc/stat and computes delta between ticks.
    First call returns 0.0 (no previous baseline).
    """
    global _prev_idle, _prev_total

    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
    except OSError as exc:
        logger.warning("Failed to read /proc/stat: %s", exc)
        return 0.0

    if not line.startswith("cpu "):
        logger.warning("Unexpected /proc/stat format: %s", line[:40])
        return 0.0

    # Fields: user, nice, system, idle, iowait, irq, softirq, steal, ...
    parts = line.split()
    try:
        values = [int(x) for x in parts[1:]]
    except ValueError:
        logger.warning("Non-integer values in /proc/stat")
        return 0.0

    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values)

    delta_total = total - _prev_total
    delta_idle = idle - _prev_idle

    _prev_idle = idle
    _prev_total = total

    if delta_total <= 0:
        return 0.0

    return round((1.0 - delta_idle / delta_total) * 100.0, 1)


# ── PSI (Pressure Stall Information) ─────────────────────────────────────

def _parse_psi(filepath: str) -> PSIMetrics:
    """
    Parse a PSI file (e.g. /proc/pressure/memory).

    Format:
        some avg10=0.00 avg60=0.00 avg300=0.00 total=0
        full avg10=0.00 avg60=0.00 avg300=0.00 total=0
    """
    metrics = PSIMetrics()
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue

                prefix = parts[0]  # "some" or "full"
                values = {}
                for part in parts[1:]:
                    if "=" in part:
                        key, val = part.split("=", 1)
                        try:
                            values[key] = float(val)
                        except ValueError:
                            pass

                if prefix == "some":
                    metrics.some_avg10 = values.get("avg10", 0.0)
                    metrics.some_avg60 = values.get("avg60", 0.0)
                    metrics.some_avg300 = values.get("avg300", 0.0)
                elif prefix == "full":
                    metrics.full_avg10 = values.get("avg10", 0.0)
                    metrics.full_avg60 = values.get("avg60", 0.0)
                    metrics.full_avg300 = values.get("avg300", 0.0)
    except OSError as exc:
        logger.warning("Failed to read PSI file %s: %s", filepath, exc)

    return metrics


def read_memory_psi() -> PSIMetrics:
    """Read memory pressure from /proc/pressure/memory."""
    return _parse_psi("/proc/pressure/memory")


def read_io_psi() -> PSIMetrics:
    """Read I/O pressure from /proc/pressure/io."""
    return _parse_psi("/proc/pressure/io")


# ── Combined snapshot ────────────────────────────────────────────────────

def read_all() -> SystemSensors:
    """Take a complete sensor snapshot for one Sentinel tick."""
    return SystemSensors(
        cpu_temp_c=read_cpu_temp(),
        cpu_load_percent=read_cpu_load(),
        memory_psi=read_memory_psi(),
        io_psi=read_io_psi(),
    )
