"""
ADRG - Media client module.

Provides a unified interface for polling active media streams from
different providers. Used by Media Mode to decide when to throttle
background containers.

Supported providers:
  - jellyfin  — Polls the Jellyfin /Sessions API
  - plex      — Polls the Plex /status/sessions API
  - webhook   — Stream state is controlled externally via POST /trigger
  - none      — Always returns 0 (media mode effectively disabled)

Usage:
    client = create_media_client(media_mode_config)
    streams = client.get_active_video_streams()  # returns int
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("adrg.media")


# ── Jellyfin ──────────────────────────────────────────────────────────────

class JellyfinClient:
    """Minimal Jellyfin API client for session monitoring."""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = 5

    def get_active_video_streams(self) -> int:
        """
        Return the number of currently active video streams.

        Queries GET /Sessions and counts sessions that have a
        NowPlayingItem with MediaType 'Video'.

        Returns 0 if Jellyfin is unreachable or the API key is invalid.
        """
        if not self._api_key:
            logger.debug("Jellyfin API key not configured — media mode disabled")
            return 0

        try:
            resp = requests.get(
                f"{self._base_url}/Sessions",
                headers={"Authorization": f'MediaBrowser Token="{self._api_key}"'},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            sessions = resp.json()
        except requests.ConnectionError:
            logger.warning("Jellyfin unreachable at %s", self._base_url)
            return 0
        except requests.Timeout:
            logger.warning("Jellyfin request timed out")
            return 0
        except requests.HTTPError as exc:
            logger.warning("Jellyfin API error: %s", exc)
            return 0
        except (ValueError, TypeError) as exc:
            logger.warning("Jellyfin returned invalid JSON: %s", exc)
            return 0

        count = sum(
            1 for s in sessions
            if s.get("NowPlayingItem", {}).get("MediaType") == "Video"
        )

        if count > 0:
            logger.debug("Jellyfin: %d active video stream(s)", count)

        return count


# ── Plex ─────────────────────────────────────────────────────────────────

class PlexClient:
    """Minimal Plex API client for session monitoring."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = 5

    def get_active_video_streams(self) -> int:
        """
        Return the number of currently active video streams.

        Queries GET /status/sessions and returns the session count
        from MediaContainer.size. Returns 0 if unreachable.
        """
        if not self._token:
            logger.debug("Plex token not configured — media mode disabled")
            return 0

        try:
            resp = requests.get(
                f"{self._base_url}/status/sessions",
                headers={
                    "X-Plex-Token": self._token,
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            count = data.get("MediaContainer", {}).get("size", 0)
        except requests.ConnectionError:
            logger.warning("Plex unreachable at %s", self._base_url)
            return 0
        except requests.Timeout:
            logger.warning("Plex request timed out")
            return 0
        except requests.HTTPError as exc:
            logger.warning("Plex API error: %s", exc)
            return 0
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Plex returned unexpected response: %s", exc)
            return 0

        if count > 0:
            logger.debug("Plex: %d active stream(s)", count)

        return count


# ── Factory ───────────────────────────────────────────────────────────────

def create_media_client(config: dict) -> Optional[object]:
    """
    Create the appropriate media client from a media_mode config block.

    Returns a client with a get_active_video_streams() method, or None
    if the provider is 'webhook' or 'none' (stream state is managed
    externally or media mode is disabled).
    """
    provider = config.get("provider", "none").lower().strip()
    url = config.get("url", "")
    api_key = config.get("api_key", "")

    if provider == "jellyfin":
        logger.info("Media provider: Jellyfin (%s)", url)
        return JellyfinClient(url, api_key)

    if provider == "plex":
        logger.info("Media provider: Plex (%s)", url)
        return PlexClient(url, api_key)  # api_key is the Plex token

    if provider == "webhook":
        logger.info("Media provider: webhook (controlled via POST /trigger)")
        return None  # Governor handles this via _webhook_media_active flag

    if provider == "none":
        return None

    logger.warning("Unknown media provider '%s' — media mode disabled", provider)
    return None
