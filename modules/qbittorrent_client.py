"""
ADRG - qBittorrent client module.

Controls the qBittorrent Web API to throttle download speeds when
media playback is detected. Activated/deactivated by Media Mode.

Authentication uses the qBittorrent cookie-based session (SID).
The session is re-established automatically if it expires.

Usage:
    client = QBittorrentClient(url, username, password)
    client.set_download_limit(5 * 1024 * 1024)  # 5 MB/s
    client.remove_download_limit()               # unlimited
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("adrg.qbittorrent")


class QBittorrentClient:
    """Minimal qBittorrent Web API client for download speed management."""

    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._logged_in = False
        self._timeout = 5

    def _login(self) -> bool:
        """Authenticate and store the SID session cookie."""
        try:
            resp = self._session.post(
                f"{self._base_url}/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
                timeout=self._timeout,
            )
            if resp.text.strip() == "Ok.":
                self._logged_in = True
                logger.debug("qBittorrent login successful")
                return True
            logger.warning(
                "qBittorrent login failed (check credentials): %s", resp.text[:100]
            )
            return False
        except requests.ConnectionError:
            logger.warning("qBittorrent unreachable at %s", self._base_url)
            return False
        except requests.RequestException as exc:
            logger.warning("qBittorrent login error: %s", exc)
            return False

    def _ensure_logged_in(self) -> bool:
        """Return True if authenticated, attempting login if necessary."""
        if not self._logged_in:
            return self._login()
        return True

    def set_download_limit(self, limit_bytes_per_sec: int) -> bool:
        """
        Set the global download speed limit.

        limit_bytes_per_sec: bytes/sec. Pass 0 to remove the limit.
        Returns True on success.
        """
        if not self._ensure_logged_in():
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/v2/transfer/setDownloadLimit",
                data={"limit": limit_bytes_per_sec},
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                if limit_bytes_per_sec == 0:
                    logger.info("qBittorrent download limit removed")
                else:
                    logger.info(
                        "qBittorrent download limit set to %d KB/s",
                        limit_bytes_per_sec // 1024,
                    )
                return True
            if resp.status_code == 403:
                # Session expired — re-authenticate and retry once
                logger.debug("qBittorrent session expired, re-authenticating")
                self._logged_in = False
                if self._login():
                    return self.set_download_limit(limit_bytes_per_sec)
            logger.warning("qBittorrent setDownloadLimit returned %d", resp.status_code)
            return False
        except requests.RequestException as exc:
            logger.warning("qBittorrent API error: %s", exc)
            self._logged_in = False
            return False

    def remove_download_limit(self) -> bool:
        """Remove the global download speed limit (set to unlimited)."""
        return self.set_download_limit(0)

    def get_download_limit(self) -> Optional[int]:
        """
        Return the current global download speed limit in bytes/sec.
        Returns 0 if unlimited, None on error.
        """
        if not self._ensure_logged_in():
            return None
        try:
            resp = self._session.get(
                f"{self._base_url}/api/v2/transfer/downloadLimit",
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                return int(resp.text.strip())
            return None
        except (requests.RequestException, ValueError) as exc:
            logger.warning("qBittorrent getDownloadLimit error: %s", exc)
            return None
