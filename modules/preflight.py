"""
ADRG - Startup preflight checks module.

Verifies that all required kernel features and system resources are
available before the governor starts. Exits with a clear, actionable
error message if anything is missing.

Checks:
  1. cgroup v2 unified hierarchy
  2. PSI (Pressure Stall Information)
  3. Docker socket accessibility
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("adrg.preflight")

_CGROUP_V2_MARKER = Path("/sys/fs/cgroup/cgroup.controllers")
_PSI_MEMORY = Path("/proc/pressure/memory")
_PSI_IO = Path("/proc/pressure/io")
_DOCKER_SOCK = Path("/var/run/docker.sock")


def run_preflight() -> None:
    """
    Run all preflight checks. Exits with a non-zero status if any
    hard requirement is unmet. Logs warnings for soft requirements.
    """
    errors = []
    warnings = []

    # ── Hard requirement: cgroup v2 ───────────────────────────────────
    if not _CGROUP_V2_MARKER.exists():
        errors.append(
            "cgroup v2 is not available — /sys/fs/cgroup/cgroup.controllers not found.\n"
            "\n"
            "  To enable cgroup v2 on Raspberry Pi OS / Debian:\n"
            "    Edit /boot/firmware/cmdline.txt and add:\n"
            "      cgroup_no_v1=all\n"
            "\n"
            "  On other systemd-based systems, add to the kernel command line:\n"
            "      systemd.unified_cgroup_hierarchy=1\n"
            "\n"
            "  Reboot after making this change."
        )

    # ── Soft requirement: PSI ─────────────────────────────────────────
    if not _PSI_MEMORY.exists() or not _PSI_IO.exists():
        warnings.append(
            "PSI (Pressure Stall Information) is not available.\n"
            "  Memory pressure and I/O pressure rules will be disabled.\n"
            "\n"
            "  To enable PSI, add 'psi=1' to your kernel command line\n"
            "  and reboot. PSI is enabled by default on kernel 5.13+."
        )

    # ── Hard requirement: Docker socket ──────────────────────────────
    if not _DOCKER_SOCK.exists():
        errors.append(
            "Docker socket not found at /var/run/docker.sock.\n"
            "  Ensure Docker is installed and running:\n"
            "    sudo systemctl start docker"
        )
    elif not os.access(str(_DOCKER_SOCK), os.R_OK | os.W_OK):
        errors.append(
            "Docker socket is not accessible (permission denied).\n"
            "  Run ADRG as root, or add your user to the docker group:\n"
            "    sudo usermod -aG docker $USER"
        )

    # ── Report ────────────────────────────────────────────────────────
    for warning in warnings:
        for line in warning.splitlines():
            logger.warning("PREFLIGHT: %s", line)

    if errors:
        logger.error("PREFLIGHT FAILED — ADRG cannot start:")
        for error in errors:
            for line in error.splitlines():
                logger.error("  %s", line)
        sys.exit(1)

    if warnings:
        logger.info("Preflight passed with warnings (see above)")
    else:
        logger.info("Preflight checks passed")
