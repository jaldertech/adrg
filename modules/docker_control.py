"""
ADRG - Docker control module.

Wrapper around the Docker SDK for container lifecycle operations
(pause, unpause, restart) and container discovery.

All operations are wrapped in error handling — Docker being
temporarily unavailable must never crash the daemon.

DRY_RUN mode: set docker_control.DRY_RUN = True before starting the
governor to log all lifecycle operations without applying them.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound

logger = logging.getLogger("adrg.docker")

# Set to True via --dry-run to log operations without applying them.
DRY_RUN: bool = False


@dataclass
class ContainerInfo:
    """Lightweight snapshot of a running container."""
    name: str
    container_id: str
    status: str           # "running", "paused", "restarting", etc.
    memory_usage: int     # Current RSS in bytes (from Docker stats)


class DockerControl:
    """Manages the Docker client connection and container operations."""

    def __init__(self):
        self._client: Optional[docker.DockerClient] = None
        self._backoff: float = 1.0
        self._max_backoff: float = 60.0

    def _ensure_client(self) -> Optional[docker.DockerClient]:
        """Lazily connect to Docker, with exponential back-off on failure."""
        if self._client is not None:
            try:
                self._client.ping()
                self._backoff = 1.0
                return self._client
            except DockerException:
                logger.warning("Docker connection lost, reconnecting...")
                self._client = None

        try:
            self._client = docker.from_env()
            self._client.ping()
            self._backoff = 1.0
            logger.info("Connected to Docker daemon")
            return self._client
        except DockerException as exc:
            logger.error(
                "Cannot connect to Docker (retry in %.0fs): %s",
                self._backoff, exc,
            )
            self._backoff = min(self._backoff * 2, self._max_backoff)
            return None

    def list_running_containers(self) -> Dict[str, ContainerInfo]:
        """
        Return a dict of {container_name: ContainerInfo} for all
        running or paused containers.
        """
        client = self._ensure_client()
        if client is None:
            return {}

        result: Dict[str, ContainerInfo] = {}
        try:
            containers = client.containers.list(all=False)
            for c in containers:
                name = c.name
                try:
                    stats = c.stats(stream=False)
                    mem_usage = (
                        stats.get("memory_stats", {}).get("usage", 0)
                    )
                except (APIError, DockerException) as exc:
                    logger.debug("Could not get stats for %s: %s", name, exc)
                    mem_usage = 0

                result[name] = ContainerInfo(
                    name=name,
                    container_id=c.id,
                    status=c.status,
                    memory_usage=mem_usage,
                )
        except DockerException as exc:
            logger.error("Failed to list containers: %s", exc)

        return result

    def list_running_containers_fast(self) -> Dict[str, ContainerInfo]:
        """
        Return container info without per-container stats calls.

        Much faster than list_running_containers() — use this for the
        main Sentinel tick and only fetch stats when needed for OOM decisions.
        """
        client = self._ensure_client()
        if client is None:
            return {}

        result: Dict[str, ContainerInfo] = {}
        try:
            containers = client.containers.list(all=False)
            for c in containers:
                result[c.name] = ContainerInfo(
                    name=c.name,
                    container_id=c.id,
                    status=c.status,
                    memory_usage=0,
                )
        except DockerException as exc:
            logger.error("Failed to list containers: %s", exc)

        return result

    def get_container_memory(self, container_name: str) -> int:
        """Get current memory usage in bytes for a specific container."""
        client = self._ensure_client()
        if client is None:
            return 0
        try:
            container = client.containers.get(container_name)
            stats = container.stats(stream=False)
            return stats.get("memory_stats", {}).get("usage", 0)
        except (NotFound, APIError, DockerException) as exc:
            logger.debug("Could not get memory for %s: %s", container_name, exc)
            return 0

    def pause_container(self, container_name: str) -> bool:
        """Pause a container. Returns True on success."""
        if DRY_RUN:
            logger.info("[DRY RUN] Would pause container: %s", container_name)
            return True
        client = self._ensure_client()
        if client is None:
            return False
        try:
            container = client.containers.get(container_name)
            if container.status == "paused":
                logger.debug("%s already paused", container_name)
                return True
            container.pause()
            logger.info("Paused container: %s", container_name)
            return True
        except NotFound:
            logger.debug("Container not found (skipping): %s", container_name)
            return False
        except (APIError, DockerException) as exc:
            logger.error("Failed to pause %s: %s", container_name, exc)
            return False

    def unpause_container(self, container_name: str) -> bool:
        """Unpause a container. Returns True on success."""
        if DRY_RUN:
            logger.info("[DRY RUN] Would unpause container: %s", container_name)
            return True
        client = self._ensure_client()
        if client is None:
            return False
        try:
            container = client.containers.get(container_name)
            if container.status != "paused":
                logger.debug("%s not paused (status: %s)", container_name, container.status)
                return True
            container.unpause()
            logger.info("Unpaused container: %s", container_name)
            return True
        except NotFound:
            logger.debug("Container not found (skipping): %s", container_name)
            return False
        except (APIError, DockerException) as exc:
            logger.error("Failed to unpause %s: %s", container_name, exc)
            return False

    def restart_container(self, container_name: str, timeout: int = 30) -> bool:
        """
        Restart a container gracefully. Returns True on success.

        Uses Docker's built-in restart which sends SIGTERM, waits `timeout`
        seconds, then SIGKILL.
        """
        if DRY_RUN:
            logger.info("[DRY RUN] Would restart container: %s", container_name)
            return True
        client = self._ensure_client()
        if client is None:
            return False
        try:
            container = client.containers.get(container_name)
            if container.status == "paused":
                container.unpause()
            container.restart(timeout=timeout)
            logger.info("Restarted container: %s", container_name)
            return True
        except NotFound:
            logger.debug("Container not found (skipping restart): %s", container_name)
            return False
        except (APIError, DockerException) as exc:
            logger.error("Failed to restart %s: %s", container_name, exc)
            return False

    def restart_container_async(self, container_name: str, timeout: int = 10) -> None:
        """
        Fire-and-forget container restart in a background thread.

        record_restart() must be called before this so the cooldown is set
        immediately, preventing the next tick from queuing a second restart
        while this one is still in progress.
        """
        if DRY_RUN:
            logger.info("[DRY RUN] Would queue async restart for: %s", container_name)
            return
        thread = threading.Thread(
            target=self.restart_container,
            args=(container_name, timeout),
            daemon=True,
            name=f"adrg-restart-{container_name}",
        )
        thread.start()
        logger.info("Queued async restart for %s", container_name)

    def get_container_id(self, container_name: str) -> Optional[str]:
        """Get the full container ID for a given container name."""
        client = self._ensure_client()
        if client is None:
            return None
        try:
            container = client.containers.get(container_name)
            return container.id
        except NotFound:
            return None
        except (APIError, DockerException) as exc:
            logger.debug("Could not get ID for %s: %s", container_name, exc)
            return None

    def close(self):
        """Close the Docker client connection."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
