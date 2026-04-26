"""Tests for drive_read_doc — first oauth_device_code stock tool."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent
    / "kernos" / "kernel" / "integrations" / "google_drive"
)
sys.path.insert(0, str(_INTEGRATION_DIR))

import drive_read_doc as tool  # noqa: E402

sys.path.remove(str(_INTEGRATION_DIR))


# ---------------------------------------------------------------------------
# Mock httpx
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self._text = text

    def json(self):
        return self._body

    @property
    def text(self):
        if self._text:
            return self._text
        import json
        return json.dumps(self._body)


class _Client:
    def __init__(self, *, meta_resp=None, export_resp=None):
        self.meta_resp = meta_resp
        self.export_resp = export_resp
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        if "/export" in url:
            return self.export_resp
        return self.meta_resp


def _fake_credential():
    return SimpleNamespace(token="ya29.fake-google-token")


def _fake_context():
    creds = SimpleNamespace(get=lambda: _fake_credential())
    return SimpleNamespace(credentials=creds)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_execute_happy_path(monkeypatch):
    fake = _Client(
        meta_resp=_Resp(200, {
            "name": "Quarterly Plan",
            "mimeType": tool.DOC_MIME,
        }),
        export_resp=_Resp(200, text=(
            "<html><body>"
            "<h1>Quarterly Plan</h1>"
            "<p>Intro paragraph.</p>"
            "<ul><li>One</li><li>Two</li></ul>"
            "</body></html>"
        )),
    )
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake)

    result = tool.execute({"file_id": "doc-abc-123"}, _fake_context())
    assert result["file_id"] == "doc-abc-123"
    assert result["title"] == "Quarterly Plan"
    assert result["mime_type"] == tool.DOC_MIME
    md = result["markdown"]
    assert "# Quarterly Plan" in md
    assert "Intro paragraph." in md
    # markdownify renders <li> as "- " bullets.
    assert "* One" in md or "- One" in md


def test_execute_uses_bearer_auth_header(monkeypatch):
    fake = _Client(
        meta_resp=_Resp(200, {"name": "x", "mimeType": tool.DOC_MIME}),
        export_resp=_Resp(200, text="<html><body>x</body></html>"),
    )
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake)
    tool.execute({"file_id": "f"}, _fake_context())
    auth = fake.calls[0]["headers"]["Authorization"]
    assert auth == "Bearer ya29.fake-google-token"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_execute_missing_file_id():
    result = tool.execute({}, _fake_context())
    assert result == {"error": "file_id is required"}


def test_execute_credential_unavailable():
    bad_context = SimpleNamespace(
        credentials=SimpleNamespace(
            get=lambda: (_ for _ in ()).throw(RuntimeError("expired")),
        ),
    )
    result = tool.execute({"file_id": "f"}, bad_context)
    assert "credential not available" in result["error"]


def test_execute_metadata_404(monkeypatch):
    fake = _Client(
        meta_resp=_Resp(404, {"error": {"message": "File not found"}}),
        export_resp=_Resp(200, text=""),
    )
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake)
    result = tool.execute({"file_id": "ghost"}, _fake_context())
    assert "Drive metadata returned 404" in result["error"]
    assert "File not found" in result["error"]


def test_execute_rejects_non_doc_mime_type(monkeypatch):
    fake = _Client(
        meta_resp=_Resp(200, {
            "name": "Slides Deck",
            "mimeType": "application/vnd.google-apps.presentation",
        }),
        export_resp=_Resp(200, text=""),
    )
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake)
    result = tool.execute({"file_id": "slide-id"}, _fake_context())
    assert "not a Google Doc" in result["error"]
    assert "presentation" in result["error"]


def test_execute_export_failure(monkeypatch):
    fake = _Client(
        meta_resp=_Resp(200, {"name": "x", "mimeType": tool.DOC_MIME}),
        export_resp=_Resp(500, {"error": {"message": "Internal error"}}),
    )
    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: fake)
    result = tool.execute({"file_id": "f"}, _fake_context())
    assert "Drive export returned 500" in result["error"]


def test_execute_network_error(monkeypatch):
    class _ErrClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): raise tool.httpx.HTTPError("connection reset")

    monkeypatch.setattr(tool.httpx, "Client", lambda *a, **kw: _ErrClient())
    result = tool.execute({"file_id": "f"}, _fake_context())
    assert "Drive request failed" in result["error"]
    assert "connection reset" in result["error"]


# ---------------------------------------------------------------------------
# Stock-loader registration sanity
# ---------------------------------------------------------------------------


def test_real_drive_descriptor_registers_via_stock_loader(monkeypatch, tmp_path):
    """The shipped drive_read_doc descriptor parses cleanly under the
    stock-tool loader against the shipped google_drive service descriptor."""
    monkeypatch.setenv("KERNOS_CREDENTIAL_KEY",
                       __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())

    from kernos.kernel.services import ServiceRegistry
    from kernos.kernel.tool_catalog import ToolCatalog
    from kernos.kernel.workspace import WorkspaceManager

    catalog = ToolCatalog()
    registry = ServiceRegistry()
    services_dir = (
        Path(__file__).resolve().parent.parent
        / "kernos" / "kernel" / "services"
    )
    registry.load_stock_dir(services_dir)
    assert registry.has("google_drive")

    integrations_dir = (
        Path(__file__).resolve().parent.parent
        / "kernos" / "kernel" / "integrations"
    )
    ws = WorkspaceManager(
        data_dir=str(tmp_path),
        catalog=catalog,
        service_registry=registry,
    )
    count = ws.register_stock_tools(integrations_dir)
    assert count >= 1
    entry = catalog.get("drive_read_doc")
    assert entry is not None
    assert entry.service_id == "google_drive"
    assert entry.registration_hash != ""
