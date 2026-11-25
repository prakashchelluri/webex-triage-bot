"""Webex webhook server and minimal REST client (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

from .engine import Engine, format_markdown

API = "https://webexapis.com/v1"
FEEDBACK_RE = re.compile(r"\bfeedback\s+#?(\d+)\s+(correct|wrong)\b", re.IGNORECASE)
HELP = (
    "Send me an issue title/description and I will suggest fixes from the team rules.\n"
    "Then reply `feedback <id> correct` or `feedback <id> wrong`."
)

log = logging.getLogger(__name__)


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Webex signs the raw body with HMAC-SHA1 and sends the hex digest in X-Spark-Signature."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


class WebexClient:
    def __init__(self, token: str, opener: Callable = urllib.request.urlopen) -> None:
        self.token = token
        self.opener = opener
        self._me: str | None = None

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}{path}",
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with self.opener(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")

    def bot_id(self) -> str:
        if self._me is None:
            self._me = self._call("GET", "/people/me")["id"]
        return self._me

    def get_message(self, message_id: str) -> dict:
        return self._call("GET", f"/messages/{message_id}")

    def post_markdown(self, room_id: str, markdown: str) -> dict:
        return self._call("POST", "/messages", {"roomId": room_id, "markdown": markdown})


def reply_for(engine: Engine, text: str) -> str:
    """Route a chat message: feedback command, help, or an issue to triage."""
    if m := FEEDBACK_RE.search(text):
        try:
            return engine.feedback(int(m.group(1)), m.group(2).lower())
        except (LookupError, ValueError) as exc:
            return f"Could not record feedback: {exc}"
    if re.fullmatch(r"\W*(\w+\s+)?help\W*", text.strip(), re.IGNORECASE):
        return HELP
    return format_markdown(engine.suggest(text))


def process_event(event: dict, engine: Engine, client: WebexClient) -> bool:
    """Handle one webhook payload. Returns True if a reply was posted."""
    if event.get("resource") != "messages" or event.get("event") != "created":
        return False
    data = event.get("data") or {}
    if not data.get("id") or not data.get("roomId"):
        return False
    if data.get("personId") == client.bot_id():  # ignore our own replies
        return False
    text = client.get_message(data["id"]).get("text", "")
    client.post_markdown(data["roomId"], reply_for(engine, text))
    return True


def make_handler(engine: Engine, client: WebexClient, secret: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if secret and not verify_signature(
                secret, body, self.headers.get("X-Spark-Signature")
            ):
                return self._send(403)
            try:
                event = json.loads(body)
            except ValueError:
                return self._send(400)
            try:
                process_event(event, engine, client)
            except Exception:
                log.exception("failed to process webhook")
                return self._send(500)
            self._send(200)

        def _send(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def serve(engine: Engine, client: WebexClient, secret: str | None, host: str, port: int) -> None:
    # ponytail: single-threaded server (one webhook at a time, one SQLite connection);
    # switch to ThreadingHTTPServer plus a connection per thread if volume grows.
    server = HTTPServer((host, port), make_handler(engine, client, secret))
    log.info("listening on %s:%d", host, port)
    server.serve_forever()
