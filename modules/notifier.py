"""
ADRG - Notification module.

Sends alerts for significant enforcer actions via one or more backends:
  - Discord (webhook)
  - NTFY (push notifications, self-hosted or ntfy.sh)
  - Gotify (self-hosted push notifications)

All notifications are also logged regardless of backend configuration.
"""

import logging
from typing import List, Optional

import requests

logger = logging.getLogger("adrg.notifier")

# Priority mappings for NTFY and Gotify
_PRIORITY_MAP = {
    "emergency": ("urgent", 10),   # (ntfy_priority, gotify_priority)
    "critical":  ("high",   8),
    "thermal":   ("high",   7),
    "media":     ("default", 4),
    "daemon":    ("low",    3),
}


def _event_priority(event: str):
    """Return (ntfy_priority_str, gotify_priority_int) for an event."""
    for key, val in _PRIORITY_MAP.items():
        if key in event:
            return val
    return ("default", 4)


class Notifier:
    """Sends notifications via Discord, NTFY, and/or Gotify."""

    def __init__(
        self,
        webhook_url: str = "",
        ntfy_url: str = "",
        ntfy_token: str = "",
        gotify_url: str = "",
        gotify_token: str = "",
        enabled_events: Optional[List[str]] = None,
    ):
        self._discord_url = webhook_url.strip() if webhook_url else ""
        self._ntfy_url = ntfy_url.strip() if ntfy_url else ""
        self._ntfy_token = ntfy_token.strip() if ntfy_token else ""
        self._gotify_url = gotify_url.strip().rstrip("/") if gotify_url else ""
        self._gotify_token = gotify_token.strip() if gotify_token else ""
        self._enabled_events = set(enabled_events or [])
        self._timeout = 5

    @property
    def any_backend_enabled(self) -> bool:
        return bool(self._discord_url or self._ntfy_url or self._gotify_url)

    def notify(self, event: str, message: str) -> None:
        """
        Send a notification for a given event type.

        Always logs the message. Only sends to configured backends if the
        event is in the enabled list (or the enabled list is empty = all events).
        """
        logger.info("[%s] %s", event, message)

        if not self.any_backend_enabled:
            return
        if self._enabled_events and event not in self._enabled_events:
            return

        if self._discord_url:
            self._send_discord(event, message)
        if self._ntfy_url:
            self._send_ntfy(event, message)
        if self._gotify_url and self._gotify_token:
            self._send_gotify(event, message)

    # ── Discord ───────────────────────────────────────────────────────

    def _send_discord(self, event: str, message: str) -> None:
        """Post a colour-coded embed to a Discord webhook."""
        payload = {
            "username": "ADRG",
            "embeds": [
                {
                    "title": f"ADRG: {event.replace('_', ' ').title()}",
                    "description": message,
                    "color": self._discord_colour(event),
                }
            ],
        }
        try:
            resp = requests.post(
                self._discord_url,
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Discord webhook returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.warning("Discord webhook failed: %s", exc)

    @staticmethod
    def _discord_colour(event: str) -> int:
        """Return a Discord embed colour based on event severity."""
        if "emergency" in event or "critical" in event:
            return 0xFF0000  # Red
        if "thermal" in event:
            return 0xFF8C00  # Orange
        if "media" in event:
            return 0x7289DA  # Blurple
        if "daemon" in event:
            return 0x43B581  # Green
        return 0x99AAB5    # Grey

    # ── NTFY ──────────────────────────────────────────────────────────

    def _send_ntfy(self, event: str, message: str) -> None:
        """
        POST a notification to an NTFY topic URL.

        self._ntfy_url should be the full topic URL, e.g.:
            https://ntfy.sh/my-adrg-alerts
            http://ntfy.local/adrg
        """
        ntfy_priority, _ = _event_priority(event)
        headers = {
            "Title": f"ADRG: {event.replace('_', ' ').title()}",
            "Priority": ntfy_priority,
            "Tags": "computer",
        }
        if self._ntfy_token:
            headers["Authorization"] = f"Bearer {self._ntfy_token}"
        try:
            resp = requests.post(
                self._ntfy_url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "NTFY returned %d: %s", resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.warning("NTFY notification failed: %s", exc)

    # ── Gotify ────────────────────────────────────────────────────────

    def _send_gotify(self, event: str, message: str) -> None:
        """
        POST a notification to a Gotify server.

        self._gotify_url should be the base URL, e.g.: http://gotify.local
        self._gotify_token is the application token from the Gotify UI.
        """
        _, gotify_priority = _event_priority(event)
        payload = {
            "title": f"ADRG: {event.replace('_', ' ').title()}",
            "message": message,
            "priority": gotify_priority,
        }
        try:
            resp = requests.post(
                f"{self._gotify_url}/message",
                json=payload,
                params={"token": self._gotify_token},
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Gotify returned %d: %s", resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.warning("Gotify notification failed: %s", exc)
