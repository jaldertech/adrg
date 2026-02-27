"""
ADRG - State management module.

Tracks all active constraints on containers (paused_by, throttle reasons,
last restart timestamps). Persists to /run/adrg/state.json so the daemon
can recover after a restart without leaving containers in an unknown state.

Key principle: a constraint is only removed when EVERY rule that requested
it has cleared.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("adrg.state")


@dataclass
class ContainerState:
    """Tracked state for a single container."""
    paused: bool = False
    paused_by: Set[str] = field(default_factory=set)
    cpu_max_applied: bool = False
    cpu_max_applied_by: Set[str] = field(default_factory=set)
    io_max_applied: bool = False
    io_max_applied_by: Set[str] = field(default_factory=set)
    memory_high_override: Optional[int] = None
    memory_high_override_by: Set[str] = field(default_factory=set)
    min_memory_high_applied: Optional[int] = None  # Track lowest squeeze value for stability
    last_restart: float = 0.0  # Unix timestamp

    def to_dict(self) -> dict:
        """Serialise to JSON-compatible dict."""
        return {
            "paused": self.paused,
            "paused_by": sorted(self.paused_by),
            "cpu_max_applied": self.cpu_max_applied,
            "cpu_max_applied_by": sorted(self.cpu_max_applied_by),
            "io_max_applied": self.io_max_applied,
            "io_max_applied_by": sorted(self.io_max_applied_by),
            "memory_high_override": self.memory_high_override,
            "memory_high_override_by": sorted(self.memory_high_override_by),
            "min_memory_high_applied": self.min_memory_high_applied,
            "last_restart": self.last_restart,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContainerState":
        """Deserialise from a JSON-loaded dict."""
        return cls(
            paused=data.get("paused", False),
            paused_by=set(data.get("paused_by", [])),
            cpu_max_applied=data.get("cpu_max_applied", False),
            cpu_max_applied_by=set(data.get("cpu_max_applied_by", [])),
            io_max_applied=data.get("io_max_applied", False),
            io_max_applied_by=set(data.get("io_max_applied_by", [])),
            memory_high_override=data.get("memory_high_override"),
            memory_high_override_by=set(data.get("memory_high_override_by", [])),
            min_memory_high_applied=data.get("min_memory_high_applied"),
            last_restart=data.get("last_restart", 0.0),
        )


class StateManager:
    """
    Manages per-container constraint state with persistence.

    Usage:
        state = StateManager("/run/adrg/state.json")
        state.add_pause("tdarr_node", "media_mode")
        state.should_be_paused("tdarr_node")  # True
        state.remove_pause("tdarr_node", "media_mode")
        state.should_be_paused("tdarr_node")  # False (if no other reasons)
    """

    def __init__(self, state_file: str):
        self._state_file = Path(state_file)
        self._containers: Dict[str, ContainerState] = {}
        self._thermal_recovery_since: float = 0.0  # When temp first dropped below threshold
        self._media_cooldown_start: float = 0.0    # When streams dropped to zero
        self._load()

    def _get(self, name: str) -> ContainerState:
        """Get or create container state."""
        if name not in self._containers:
            self._containers[name] = ContainerState()
        return self._containers[name]

    # ── Pause tracking ────────────────────────────────────────────────

    def add_pause(self, container: str, reason: str) -> None:
        """Mark that `reason` wants this container paused."""
        cs = self._get(container)
        cs.paused_by.add(reason)
        cs.paused = True

    def remove_pause(self, container: str, reason: str) -> None:
        """Remove a pause reason. Container unpauses only when all reasons clear."""
        cs = self._get(container)
        cs.paused_by.discard(reason)
        if not cs.paused_by:
            cs.paused = False

    def should_be_paused(self, container: str) -> bool:
        """Check if any rule still wants this container paused."""
        return bool(self._get(container).paused_by)

    # ── CPU max tracking ──────────────────────────────────────────────

    def add_cpu_max(self, container: str, reason: str) -> None:
        """Mark that `reason` wants cpu.max applied to this container."""
        cs = self._get(container)
        cs.cpu_max_applied_by.add(reason)
        cs.cpu_max_applied = True

    def remove_cpu_max(self, container: str, reason: str) -> None:
        """Remove a cpu.max reason."""
        cs = self._get(container)
        cs.cpu_max_applied_by.discard(reason)
        if not cs.cpu_max_applied_by:
            cs.cpu_max_applied = False

    def should_have_cpu_max(self, container: str) -> bool:
        """Check if any rule still wants cpu.max on this container."""
        return bool(self._get(container).cpu_max_applied_by)

    # ── I/O max tracking ─────────────────────────────────────────────

    def add_io_max(self, container: str, reason: str) -> None:
        """Mark that `reason` wants io.max applied to this container."""
        cs = self._get(container)
        cs.io_max_applied_by.add(reason)
        cs.io_max_applied = True

    def remove_io_max(self, container: str, reason: str) -> None:
        """Remove an io.max reason."""
        cs = self._get(container)
        cs.io_max_applied_by.discard(reason)
        if not cs.io_max_applied_by:
            cs.io_max_applied = False

    def should_have_io_max(self, container: str) -> bool:
        """Check if any rule still wants io.max on this container."""
        return bool(self._get(container).io_max_applied_by)

    # ── Memory high override tracking ─────────────────────────────────

    def set_memory_high_override(self, container: str, value: int, reason: str) -> None:
        """Set a memory.high override value and track the reason."""
        cs = self._get(container)
        cs.memory_high_override = value
        cs.memory_high_override_by.add(reason)

    def clear_memory_high_override(self, container: str, reason: str) -> None:
        """Clear a memory.high override reason."""
        cs = self._get(container)
        cs.memory_high_override_by.discard(reason)
        if not cs.memory_high_override_by:
            cs.memory_high_override = None

    def has_memory_high_override(self, container: str) -> bool:
        """Check if memory.high is currently overridden."""
        return self._get(container).memory_high_override is not None

    # ── Restart cooldown tracking ─────────────────────────────────────

    def record_restart(self, container: str) -> None:
        """Record that a container was just restarted."""
        self._get(container).last_restart = time.time()

    def can_restart(self, container: str, cooldown_seconds: int) -> bool:
        """Check if enough time has passed since the last restart."""
        last = self._get(container).last_restart
        if last == 0.0:
            return True
        return (time.time() - last) >= cooldown_seconds

    # ── Thermal recovery tracking ─────────────────────────────────────

    @property
    def thermal_recovery_since(self) -> float:
        return self._thermal_recovery_since

    @thermal_recovery_since.setter
    def thermal_recovery_since(self, value: float) -> None:
        self._thermal_recovery_since = value

    def thermal_recovery_elapsed(self) -> float:
        """Seconds since temperature first dropped below recovery threshold."""
        if self._thermal_recovery_since == 0.0:
            return 0.0
        return time.time() - self._thermal_recovery_since

    # ── Media mode cooldown tracking ──────────────────────────────────

    @property
    def media_cooldown_start(self) -> float:
        return self._media_cooldown_start

    @media_cooldown_start.setter
    def media_cooldown_start(self, value: float) -> None:
        self._media_cooldown_start = value

    def media_cooldown_elapsed(self) -> float:
        """Seconds since streams dropped to zero."""
        if self._media_cooldown_start == 0.0:
            return 0.0
        return time.time() - self._media_cooldown_start

    # ── Persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Persist current state to the state file."""
        data = {
            "containers": {
                name: cs.to_dict() for name, cs in self._containers.items()
            },
            "thermal_recovery_since": self._thermal_recovery_since,
            "media_cooldown_start": self._media_cooldown_start,
            "saved_at": time.time(),
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_file)
        except OSError as exc:
            logger.error("Failed to save state to %s: %s", self._state_file, exc)

    def _load(self) -> None:
        """Load state from the state file if it exists."""
        if not self._state_file.exists():
            logger.info("No existing state file — starting fresh")
            return
        try:
            data = json.loads(self._state_file.read_text())
            for name, cs_data in data.get("containers", {}).items():
                self._containers[name] = ContainerState.from_dict(cs_data)
            self._thermal_recovery_since = data.get("thermal_recovery_since", 0.0)
            self._media_cooldown_start = data.get("media_cooldown_start", 0.0)
            logger.info(
                "Loaded state for %d containers from %s",
                len(self._containers), self._state_file,
            )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Failed to load state file (starting fresh): %s", exc)
            self._containers = {}

    def get_all_containers(self) -> Dict[str, ContainerState]:
        """Return the full state dict (read-only use)."""
        return dict(self._containers)

    def clear_all(self) -> None:
        """Reset all state — used during cleanup on daemon shutdown."""
        self._containers.clear()
        self._thermal_recovery_since = 0.0
        self._media_cooldown_start = 0.0
