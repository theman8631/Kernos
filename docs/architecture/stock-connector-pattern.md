# Stock connector pattern

A stock connector is Kernos's adapter to one external service that's universal enough to ship in source — Notion, Slack, GitHub, Gmail, Drive. The pattern below describes the shape every stock connector takes; the worked recipe in `docs/workshop/stock-connector-recipe.md` walks through the Notion implementation as illustration.

This document is service-agnostic. Reading it should let an engineer (or future-Kernos) build the next stock connector without re-deriving the architecture.

## The four components

A stock connector consists of four artifacts that compose with the primitive's machinery:

1. **A service descriptor** — declares the service to Kernos.
2. **One or more tool files** — the worked behaviour (read pages, post a message, etc.).
3. **An onboarding entry point** — how the operator gets credentials into Kernos.
4. **An audit and gate alignment** — which operations classify as read / soft_write / hard_write / delete.

Of these, only #1 and #2 ship as committed source. Onboarding flows through the existing `credentials_cli` for `api_token` services and the future device-code subsystem for OAuth services. Gate alignment is declarative (per-operation classification on the descriptor); no runtime code.

## Where each component lives

```
kernos/
├─ kernel/
│  ├─ services/
│  │  └─ <service>.service.json          # the service descriptor
│  └─ integrations/
│     └─ <service>/
│        ├─ <tool>.tool.json              # tool descriptor
│        └─ <tool>.py                     # tool implementation
└─ messages/handler.py                    # boot wiring loads both
```

The handler at boot calls `service_registry.load_stock_dir(kernos/kernel/services/)` followed by `workspace.register_stock_tools(kernos/kernel/integrations/)`. The first registers services; the second registers each tool against its declared service. No new entry points per connector.

## How the connector composes with the primitive

The primitive (WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE, see `docs/architecture/workshop-external-services.md`) gives every connector for free:

- **Per-member credentials.** Tokens stored under `(member_id, service_id)`, encrypted at rest, accessible at invocation through `context.credentials.get()`.
- **Auth-by-channel matrix.** The descriptor's `auth_type` plus the matrix in `kernos/kernel/services.py` decide which channels can run onboarding. Connectors don't re-implement this.
- **Per-operation classification + dispatch gate routing.** Per-operation declarations on the tool descriptor route through the gate as `read` / `soft_write` / `hard_write` / `delete`. The fail-closed default (`soft_write`) catches forgotten declarations.
- **Runtime enforcement.** Hash check, authority re-check, credential scope re-check, sandbox check fire before every invocation. Connector authors don't write any of this code.
- **Audit log.** Service-bound invocations get the workshop primitive's payload-digest plus normalized-category vocabulary written to the audit store automatically.

A connector author's job is to declare the service and write the tools. The substrate handles isolation, gating, audit, and enforcement.

## What the connector author actually writes

### The service descriptor

A small JSON file under `kernel/services/`. Required fields:

- `service_id` — stable lowercase identifier.
- `display_name` — human label.
- `auth_type` — `api_token` or `oauth_device_code`.
- `operations` — the operation vocabulary the service exposes. Tools declare their `authority` as a subset.
- `audit_category` — operator-readable category for the audit log. Defaults to `service_id`.
- `required_scopes` — auth-flow scope strings (OAuth services use this; api_token services may leave empty).
- `notes` — free-form description shown in `inspect_state`.

The Notion descriptor at `kernel/services/notion.service.json` is the reference shape.

### The tool descriptor + implementation

Tools live under `kernel/integrations/<service>/`. Each tool is a `(.tool.json, .py)` pair.

The descriptor extends the workshop tool format with the primitive's fields:

- `service_id` — links the tool to the service.
- `authority` — operations this tool invokes. Must be a subset of the service's `operations`.
- `operations` — per-operation classification (e.g., `read_pages → read`, `delete_pages → delete`).
- `audit_category` — defaults to the service's `audit_category`.
- `domain_hints` — optional list of strings for the future surfacing layer.

The implementation is a `.py` file exporting `execute(input_data, context)` returning a dict. The context is invocation-scoped (`member_id`, `data_dir`, `credentials`, `space_id`, `tool_id`); the implementation must use `context.credentials.get()` to obtain the invoking member's token rather than reading env vars or hardcoding identifiers. The authoring-pattern validator catches the obvious bypass attempts at registration.

### The onboarding entry point

For `api_token` services, the existing `credentials_cli` already handles onboarding — the operator runs `python -m kernos.kernel.credentials_cli onboard --service <service_id>`. The auth-by-channel matrix refuses anything but CLI for api_token paste, so adapter-side onboarding does not exist for this auth type by design.

For `oauth_device_code` services, the device-code subsystem ships separately (out of scope for the first stock connector); the onboarding entry then surfaces the device code through whichever adapter the operator initiates the flow from.

A connector author does NOT write a service-specific onboarding command. The CLI consumes the service descriptor and routes by `auth_type`.

### Gate and audit

Both flow from the descriptor. Per-operation classification declares which gate effect each operation routes through. The audit log gets `audit_category` from the descriptor and `normalized_category` mechanically from the presence of `service_id` (`tool.invocation.external_service`). No connector-specific code.

## How fresh-Kernos approaches a new connector

The procedure for adding a stock connector (Slack used as the example here, but the steps are service-agnostic):

1. **Read the service's API documentation.** Note the auth flow (api_token vs OAuth), the list of operations the connector will expose, and any rate-limit or quota caveats worth noting in the descriptor's `notes` field.

2. **Author the service descriptor** at `kernel/services/<service>.service.json`. Use the Notion descriptor as the reference shape.

3. **Author the worked tools.** One per primary operation is the right starting density (a `slack_send_message` tool, a `slack_read_channel` tool, etc.). Each is a `(.tool.json, .py)` pair under `kernel/integrations/<service>/`. Use `notion_read_page` as the reference shape.

4. **Run the primitive's tests.** The `test_workspace_service_dispatch.py` and `test_stock_tool_loader.py` patterns generalise — write a small test that asserts the new descriptor parses and the loader registers it. The primitive's existing tests cover everything else (gate routing, runtime enforcement, audit shape).

5. **Run a live test.** Onboard a real credential (or a fake-but-shaped token), invoke a tool against the real API, confirm the audit entry shape. Reuse the live-test scaffolding in `data/diagnostics/live-tests/STOCK-INTEGRATIONS-NOTION-live-test.md`.

That's the whole pattern. No dispatcher changes, no new error types, no new audit shapes. The primitive carries the load; the connector is configuration plus a worked tool.

## When the pattern doesn't fit

Some integration shapes won't fit cleanly:

- **OAuth redirect flows that need a public callback URL.** Server deployments often don't have one. Use OAuth device-code instead, or wait for a topology spec to address redirect-flow support.
- **WebSocket / streaming services.** The connector pattern assumes request/response operations. A streaming service (Discord gateway, Slack RTM) is a different shape — the existing adapter pattern is the closer fit.
- **Services without an HTTP API.** Use the browser primitive plus `BROWSER-COOKIE-IMPORT` (when it lands) instead of authoring a stock connector against a scraping target.

When a service doesn't fit, surface the gap. Forcing a poor shape into the connector pattern produces brittle tools and stretches the primitive in ways that hurt the next connector.
