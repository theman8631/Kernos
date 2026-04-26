"""drive_read_doc: read a Google Doc as markdown.

First stock instance using OAuth device-code (Slack and friends to
follow). Demonstrates the recipe under Google's quirks: PKCE
required, client_secret in token-exchange, env-var-sourced client
identifiers.

Output shape:

    {
        "file_id":      "<id>",
        "title":        "<doc title>",
        "mime_type":    "application/vnd.google-apps.document",
        "markdown":     "<rendered content>",
    }

Or on error:

    {"error": "<message>"}

The tool calls Drive v3 in two steps:
1. GET /drive/v3/files/{file_id}?fields=name,mimeType — metadata.
2. GET /drive/v3/files/{file_id}/export?mimeType=text/html — the
   document body as HTML, which we run through markdownify to
   produce structured markdown.

Drive's export endpoint does not support text/markdown directly;
we round-trip through HTML to preserve headings, lists, blockquotes,
code blocks, and inline annotations. text/plain export was
considered but loses too much structure for the agent's reasoning.

Non-Doc files (Sheets, Slides, regular files) are not supported by
this tool — Drive's export endpoint only works on native Google
Docs file types. The error path surfaces a clean message naming the
unsupported MIME type rather than a cryptic Drive API error.
"""

from __future__ import annotations

import httpx
from markdownify import markdownify

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOC_MIME = "application/vnd.google-apps.document"


def execute(input_data, context):
    payload = input_data or {}
    file_id = (payload.get("file_id") or "").strip()
    if not file_id:
        return {"error": "file_id is required"}

    try:
        credential = context.credentials.get()
    except Exception as exc:
        return {"error": f"credential not available: {exc}"}

    headers = {"Authorization": f"Bearer {credential.token}"}

    try:
        with httpx.Client(timeout=30.0) as client:
            meta_resp = client.get(
                f"{DRIVE_API}/files/{file_id}",
                headers=headers,
                params={"fields": "name,mimeType"},
            )
            if meta_resp.status_code >= 400:
                return _format_api_error(meta_resp, "metadata")
            meta = meta_resp.json()

            mime_type = meta.get("mimeType", "")
            if mime_type != DOC_MIME:
                return {
                    "error": (
                        f"file is not a Google Doc (mimeType={mime_type!r}). "
                        f"drive_read_doc only handles native Google Docs; "
                        f"Sheets, Slides, and other Drive file types need "
                        f"different export handling."
                    ),
                }

            export_resp = client.get(
                f"{DRIVE_API}/files/{file_id}/export",
                headers=headers,
                params={"mimeType": "text/html"},
            )
            if export_resp.status_code >= 400:
                return _format_api_error(export_resp, "export")
            html = export_resp.text
    except httpx.HTTPError as exc:
        return {"error": f"Drive request failed: {exc}"}

    return {
        "file_id": file_id,
        "title": meta.get("name", ""),
        "mime_type": mime_type,
        "markdown": markdownify(html, heading_style="ATX").strip(),
    }


def _format_api_error(resp, stage: str):
    try:
        body = resp.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        message = (
            error.get("message")
            if isinstance(error, dict)
            else str(error)
        ) or ""
    except Exception:
        message = resp.text[:200]
    return {
        "error": (
            f"Drive {stage} returned {resp.status_code}"
            f"{': ' + message if message else ''}"
        ),
    }
