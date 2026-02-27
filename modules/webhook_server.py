"""
ADRG - HTTP webhook and status server module.

Provides two endpoints on a configurable port (default 8765):

  GET  /status   — Returns current governor state as JSON.
                   Useful for monitoring and debugging.

  POST /trigger  — Accepts a JSON body to push external trigger events.
                   Allows n8n, Home Assistant, scripts, etc. to control
                   ADRG without needing a media provider API.

Trigger events (POST /trigger body: {"event": "<event_name>"}):
  media_start    — Force media mode on (overrides provider polling)
  media_stop     — Clear the media mode override
  tier3_pause    — Manually pause all Tier 3 containers
  tier3_resume   — Resume all Tier 3 containers

Trigger events are queued and processed on the main governor thread
during the next tick, avoiding cross-thread state mutation.

Security note: bind to 127.0.0.1 (default) to restrict access to
the local machine. Only expose on 0.0.0.0 in trusted networks.
"""

import http.server
import json
import logging
import queue
import threading
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    pass  # Governor type hint is kept as a string to avoid circular imports

logger = logging.getLogger("adrg.http")

VALID_TRIGGER_EVENTS = frozenset({
    "media_start",
    "media_stop",
    "tier3_pause",
    "tier3_resume",
})


class _ADRGHTTPServer(http.server.HTTPServer):
    """HTTPServer subclass carrying references the handler needs."""

    def __init__(self, server_address, governor, trigger_queue):
        self.governor = governor
        self.trigger_queue = trigger_queue
        super().__init__(server_address, _RequestHandler)


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles /status and /trigger requests."""

    server: _ADRGHTTPServer

    def log_message(self, format, *args):
        """Route access log through the ADRG logger at DEBUG level."""
        logger.debug("HTTP %s - %s", self.address_string(), format % args)

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        if self.path == "/trigger":
            self._handle_trigger()
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    def _handle_status(self):
        try:
            status = self.server.governor.get_status()
            self._send_json(200, status)
        except Exception as exc:
            logger.warning("Error building status response: %s", exc)
            self._send_json(500, {"error": "internal error"})

    def _handle_trigger(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        event = data.get("event", "").strip()
        if not event:
            self._send_json(400, {"error": "missing 'event' field"})
            return
        if event not in VALID_TRIGGER_EVENTS:
            self._send_json(400, {
                "error": f"unknown event '{event}'",
                "valid_events": sorted(VALID_TRIGGER_EVENTS),
            })
            return

        self.server.trigger_queue.put(event)
        logger.info("Webhook trigger received: %s", event)
        self._send_json(200, {"queued": event})

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebhookServer:
    """
    Manages the lifecycle of the ADRG HTTP server.

    Start with start(), stop with stop(). Trigger events are collected
    in trigger_queue and should be drained each governor tick via
    drain_triggers().
    """

    def __init__(self, governor, host: str = "127.0.0.1", port: int = 8765):
        self._governor = governor
        self._host = host
        self._port = port
        self._server: _ADRGHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.trigger_queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        """Start the HTTP server in a daemon background thread."""
        try:
            self._server = _ADRGHTTPServer(
                (self._host, self._port),
                self._governor,
                self.trigger_queue,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="adrg-http",
            )
            self._thread.start()
            logger.info(
                "HTTP server listening on %s:%d (/status, /trigger)",
                self._host, self._port,
            )
        except OSError as exc:
            logger.error(
                "Failed to start HTTP server on %s:%d — %s",
                self._host, self._port, exc,
            )

    def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def drain_triggers(self) -> List[str]:
        """
        Return and clear all pending trigger events.
        Called by the governor on each tick to process externally pushed events.
        """
        events: List[str] = []
        while True:
            try:
                events.append(self.trigger_queue.get_nowait())
            except queue.Empty:
                break
        return events
