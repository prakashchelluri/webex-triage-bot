import hashlib
import hmac
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from triage_bot.llm import NoneProvider, load_provider
from triage_bot.webex import (
    WebexClient,
    make_handler,
    process_event,
    reply_for,
    verify_signature,
)

BOT_ID = "bot-person-id"


class FakeResponse(io.BytesIO):
    pass


class FakeWebex:
    """Stands in for urllib.request.urlopen: records requests, never touches the network."""

    def __init__(self, message_text="pod OOMKilled"):
        self.message_text = message_text
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), req.full_url, body, dict(req.header_items())))
        if req.full_url.endswith("/people/me"):
            return FakeResponse(json.dumps({"id": BOT_ID}).encode())
        if req.get_method() == "GET":
            return FakeResponse(json.dumps({"id": "m1", "text": self.message_text}).encode())
        return FakeResponse(b'{"id": "reply"}')

    @property
    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def event(person="user-1"):
    return {"resource": "messages", "event": "created",
            "data": {"id": "m1", "roomId": "room-1", "personId": person}}


def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def test_signature():
    body = b'{"a": 1}'
    assert verify_signature("s3cret", body, sign("s3cret", body))
    assert not verify_signature("s3cret", body, sign("other", body))
    assert not verify_signature("s3cret", body, None)


def test_process_event_fetches_and_replies(engine):
    fake = FakeWebex()
    assert process_event(event(), engine, WebexClient("tok", opener=fake))
    method, url, body, headers = fake.posts[0]
    assert url == "https://webexapis.com/v1/messages"
    assert body["roomId"] == "room-1"
    assert "`oom-killed`" in body["markdown"]
    assert headers["Authorization"] == "Bearer tok"
    assert any(r[1] == "https://webexapis.com/v1/messages/m1" for r in fake.requests)


def test_process_event_ignores_own_and_other_events(engine):
    fake = FakeWebex()
    client = WebexClient("tok", opener=fake)
    assert not process_event(event(person=BOT_ID), engine, client)
    assert not process_event({"resource": "rooms", "event": "created"}, engine, client)
    assert fake.posts == []


def test_reply_for_routes_commands(engine):
    sid = engine.suggest("CrashLoopBackOff").suggestions[0].id
    assert "Marked suggestion" in reply_for(engine, f"TriageBot feedback {sid} correct")
    assert "already marked" in reply_for(engine, f"feedback #{sid} wrong")
    assert "unknown suggestion" in reply_for(engine, "feedback 999 wrong")
    assert "Send me an issue" in reply_for(engine, "TriageBot help")


@pytest.fixture
def server(engine):
    fake = FakeWebex()
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(engine, WebexClient("t", fake), "s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/", fake
    httpd.shutdown()
    httpd.server_close()


def post(url, body, signature=None):
    headers = {"X-Spark-Signature": signature} if signature else {}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_http_server_checks_signature(server):
    url, fake = server
    body = json.dumps(event()).encode()
    assert post(url, body, sign("wrong", body)) == 403
    assert post(url, body) == 403
    assert fake.posts == []
    assert post(url, body, sign("s3cret", body)) == 200
    assert len(fake.posts) == 1


def test_http_server_rejects_bad_json(server):
    url, _ = server
    assert post(url, b"not json", sign("s3cret", b"not json")) == 400


def test_load_provider():
    assert isinstance(load_provider("none"), NoneProvider)
    assert load_provider("test_engine:FakeProvider").suggest("x") == "try turning it off and on"
    with pytest.raises(ValueError):
        load_provider("no_colon")
