"""Tests for the webhook receiver.

WORKFLOW-LOOP-PRIMITIVE C6. Pins:

  - HMAC signature verification (valid + invalid + missing header)
  - Bearer token verification
  - Schema validation rejects malformed bodies
  - Validated POST translates to a single event_stream.emit with
    ``event_type == "external.webhook"``
  - Unknown source_id → 404
  - Multi-tenancy: source's instance_id is what carries onto the
    event
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kernos.kernel import event_stream
from kernos.kernel.webhooks.receiver import (
    WebhookRegistry,
    WebhookSourceConfig,
    register_routes,
)


@pytest.fixture
async def app_and_writer(tmp_path):
    """Build a FastAPI app with the webhook routes mounted, plus a
    fresh event_stream writer so we can assert emitted events."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    registry = WebhookRegistry()
    app = FastAPI()
    register_routes(app, registry)
    yield app, registry
    await event_stream._reset_for_tests()


def _hmac_sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class TestHmacAuth:
    async def test_valid_signature_accepted(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="github",
            instance_id="inst_a",
            hmac_secret=b"deadbeef",
        ))
        body = b'{"action":"push","ref":"refs/heads/main"}'
        sig = _hmac_sig(b"deadbeef", body)
        client = TestClient(app)
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Webhook-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        await event_stream.flush_now()
        events = await event_stream.events_in_window(
            "inst_a",
            __import__("datetime").datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            __import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        webhook_events = [
            e for e in events if e.event_type == "external.webhook"
        ]
        assert len(webhook_events) == 1
        assert webhook_events[0].payload["source_id"] == "github"
        assert webhook_events[0].payload["body"]["action"] == "push"

    async def test_invalid_signature_rejected(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="github",
            instance_id="inst_a",
            hmac_secret=b"deadbeef",
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/github",
            content=b'{}',
            headers={"X-Webhook-Signature": "sha256=bogus"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "hmac_invalid"

    async def test_missing_signature_rejected(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="github",
            instance_id="inst_a",
            hmac_secret=b"x",
        ))
        client = TestClient(app)
        resp = client.post("/webhooks/github", content=b'{}')
        assert resp.status_code == 401


class TestBearerAuth:
    async def test_valid_bearer_accepted(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="ext",
            instance_id="inst_a",
            bearer_token="secret-token",
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/ext",
            content=b'{"x":1}',
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 200

    async def test_invalid_bearer_rejected(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="ext",
            instance_id="inst_a",
            bearer_token="real-token",
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/ext",
            content=b'{}',
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    async def test_malformed_authorization_header_rejected(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="ext",
            instance_id="inst_a",
            bearer_token="t",
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/ext",
            content=b'{}',
            headers={"Authorization": "NotBearer t"},
        )
        assert resp.status_code == 401


class TestSchemaValidation:
    async def test_schema_rejects_bad_body(self, app_and_writer):
        app, registry = app_and_writer

        def schema(body):
            if "required_field" not in body:
                raise ValueError("missing required_field")
            return body

        registry.register(WebhookSourceConfig(
            source_id="ext",
            instance_id="inst_a",
            bearer_token="t",
            schema=schema,
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/ext",
            content=b'{"other":"x"}',
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400
        assert "missing required_field" in resp.json()["detail"]

    async def test_schema_normalises_body(self, app_and_writer):
        app, registry = app_and_writer

        def schema(body):
            return {"normalised": True, **body}

        registry.register(WebhookSourceConfig(
            source_id="ext",
            instance_id="inst_a",
            bearer_token="t",
            schema=schema,
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/ext",
            content=b'{"x":1}',
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        await event_stream.flush_now()
        events = await event_stream.events_in_window(
            "inst_a",
            __import__("datetime").datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            __import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        wh = [e for e in events if e.event_type == "external.webhook"]
        assert wh[-1].payload["body"]["normalised"] is True


class TestUnknownSource:
    async def test_unknown_source_404(self, app_and_writer):
        app, _ = app_and_writer
        client = TestClient(app)
        resp = client.post("/webhooks/never-registered", content=b'{}')
        assert resp.status_code == 404


class TestRegistry:
    def test_register_duplicate_rejected(self):
        registry = WebhookRegistry()
        registry.register(WebhookSourceConfig(
            source_id="x", instance_id="i", bearer_token="t",
        ))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(WebhookSourceConfig(
                source_id="x", instance_id="i", bearer_token="t",
            ))

    def test_config_requires_exactly_one_auth(self):
        with pytest.raises(ValueError, match="exactly one"):
            WebhookSourceConfig(source_id="x", instance_id="i")
        with pytest.raises(ValueError, match="exactly one"):
            WebhookSourceConfig(
                source_id="x", instance_id="i",
                bearer_token="t", hmac_secret=b"s",
            )


class TestMultiTenancy:
    async def test_source_instance_id_carries_to_event(self, app_and_writer):
        app, registry = app_and_writer
        registry.register(WebhookSourceConfig(
            source_id="from-b",
            instance_id="inst_b",
            bearer_token="t",
        ))
        client = TestClient(app)
        resp = client.post(
            "/webhooks/from-b",
            content=b'{"x":1}',
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        await event_stream.flush_now()
        # inst_a sees nothing.
        a = await event_stream.events_in_window(
            "inst_a",
            __import__("datetime").datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            __import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        assert all(e.event_type != "external.webhook" for e in a)
        # inst_b sees the webhook event.
        b = await event_stream.events_in_window(
            "inst_b",
            __import__("datetime").datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
            __import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
        )
        wh = [e for e in b if e.event_type == "external.webhook"]
        assert len(wh) == 1
        assert wh[0].payload["source_id"] == "from-b"
