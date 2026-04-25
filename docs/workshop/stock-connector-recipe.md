# Stock connector recipe

This is the procedure for adding a stock external-service connector to Kernos. Notion runs throughout as the worked example; the substitutions table at the end shows what changes for Slack, GitHub, or Drive.

The pattern this recipe instantiates is described service-agnostically in `docs/architecture/stock-connector-pattern.md`. Read that first if you want the why; this doc is the how.

## What you're producing

Six artifacts, in this order:

1. A service descriptor JSON file under `kernos/kernel/services/`.
2. One or more tool descriptor JSON files under `kernos/kernel/integrations/<service>/`.
3. The matching tool implementation `.py` files in the same directory.
4. A small test asserting the descriptor parses and the loader registers it.
5. An onboarding command run once per (operator, member, service) tuple.
6. A live test artifact under `data/diagnostics/live-tests/` recording the end-to-end check.

You write items 1–4. The handler boot wiring picks 1 and 2 up automatically. The onboarding flow (item 5) is provided by the existing `credentials_cli`. The live test (item 6) reuses the scaffolding from STOCK-INTEGRATIONS-NOTION.

## Step 1 — Author the service descriptor

Path: `kernos/kernel/services/notion.service.json`.

```json
{
  "service_id": "notion",
  "display_name": "Notion",
  "auth_type": "api_token",
  "operations": [
    "read_pages",
    "write_pages",
    "read_databases",
    "query_databases",
    "create_pages",
    "update_pages",
    "create_comments"
  ],
  "audit_category": "notion",
  "required_scopes": [],
  "notes": "Notion's integration-token model. The member creates an integration in their Notion workspace, shares the relevant pages or databases with it, and pastes the integration token via the api_token auth flow on CLI."
}
```

Field reference is in the pattern doc. Two practical notes:

- `operations` is the master vocabulary for the service. Tools declare their `authority` as a subset; the registry rejects tools that claim authority outside this list. Be deliberate about what you name here — adding an operation later is fine; renaming or removing one breaks any tool that depends on it.
- `auth_type` decides which channels can run onboarding. `api_token` is CLI-only (the matrix in `kernos/kernel/services.py` enforces this); `oauth_device_code` works on every channel. `cookie_upload` is reserved for `BROWSER-COOKIE-IMPORT` and not yet usable.

## Step 2 — Author the tool descriptor

Path: `kernos/kernel/integrations/notion/notion_read_page.tool.json`.

```json
{
  "name": "notion_read_page",
  "description": "Read a Notion page as markdown given its page id.",
  "input_schema": {
    "type": "object",
    "properties": {
      "page_id": {"type": "string"}
    },
    "required": ["page_id"]
  },
  "implementation": "notion_read_page.py",
  "service_id": "notion",
  "authority": ["read_pages"],
  "operations": [
    {"operation": "read_pages", "classification": "read"}
  ],
  "audit_category": "notion",
  "domain_hints": ["notion", "knowledge", "documentation"],
  "stateful": false
}
```

The fields you'll think about most:

- `authority` — the operations this tool actually invokes. A read-only tool declares `["read_pages"]`; a write tool would declare `["write_pages"]`. A tool that does both declares both, and the per-operation classifications fan out.
- `operations` — per-operation classification. `read_pages` is `read` (no gate). A `write_pages` operation would be `hard_write` (gate fires confirmation). A `delete_pages` operation would be `delete` (maps to hard_write in v1; runtime confirmation is the safety net). Tools that classify nothing fall back to the fail-closed `soft_write` default.
- `domain_hints` — strings the future surfacing layer reads to decide which agents see this tool. Service-bound tools also surface based on credential presence; hints help when the tool is one of several talking to the same service.

## Step 3 — Author the tool implementation

Path: `kernos/kernel/integrations/notion/notion_read_page.py`.

The contract is a single function:

```python
def execute(input_data, context):
    # input_data is the dict the agent passed (matches input_schema).
    # context is the invocation-scoped runtime context: member_id,
    # data_dir, credentials, space_id, tool_id.
    ...
    return {"page_id": ..., "title": ..., "markdown": ...}
```

Three rules to internalise:

- **Use `context.credentials.get()` for the token.** Never read env vars directly. Never hardcode credentials. The authoring-pattern validator rejects either at registration.
- **Use `context.data_dir` for any persistent files.** Never write to absolute paths. The runtime sandbox check verifies any path you return resolves under your tool's per-member directory.
- **Return a dict.** Errors return `{"error": "..."}` rather than raising; the dispatcher logs your message in the audit entry and surfaces it to the agent. Exceptions are caught and audit-logged, but a clean error dict is friendlier.

The shipped Notion implementation (`kernos/kernel/integrations/notion/notion_read_page.py`) is the reference. It calls Notion's API with the member's token, parses the block tree, returns markdown plus title. Total length: under 150 lines including helpers.

## Step 4 — Add a registration test

Path: `tests/test_<service>_tools.py` (or extend an existing test file).

Two cases are sufficient:

```python
def test_real_<service>_descriptor_parses():
    catalog = ToolCatalog()
    services = ServiceRegistry()
    services.load_stock_dir(<services_dir>)
    ws = WorkspaceManager(
        data_dir="./data", catalog=catalog, service_registry=services,
    )
    count = ws.register_stock_tools(<integrations_dir>)
    assert count >= 1
    assert catalog.get("<tool_name>") is not None
```

Plus pure-logic helpers (markdown rendering, etc.) get straight unit tests with mocked httpx. The Notion test file demonstrates both.

The primitive's existing tests cover gate routing, audit-log shape, runtime enforcement, and credential scoping — you do not need to re-test those.

## Step 5 — Onboard a credential

Once the source files are committed and Kernos is running, the operator (or member, on multi-member installs) onboards a credential:

```
python -m kernos.kernel.credentials_cli onboard \
    --service notion \
    --instance discord:OWNER_ID \
    --member mem_alice
```

The CLI prompts for the token via getpass (no echo, no shell history). The token lands encrypted in `data/<instance>/credentials/<member>/notion.enc.json`.

This step is service-agnostic; the same command swaps Slack or GitHub in for Notion. The CLI consults the service descriptor's `auth_type` and routes accordingly.

## Step 6 — Invoke and verify

The agent calls the tool by name like any other:

```
notion_read_page(page_id="abc-123-def")
```

The dispatcher:

1. Looks up the tool, sees `service_id: notion`, routes through service-bound dispatch.
2. Runs the four runtime checks (hash, authority, credential, sandbox).
3. Builds the runtime context with the invoking member's identity.
4. Calls `execute(input_data, context)`.
5. Writes an audit entry to the audit store with payload digest + normalized category.
6. Returns the JSON result.

Verification checklist:

- The audit entry under `data/<instance>/audit/<date>.json` contains an entry with `tool_name: notion_read_page`, `service_id: notion`, `normalized_category: tool.invocation.external_service`, and a non-empty `payload_digest`.
- The tool returned a dict with the expected shape.
- The runtime context's `data_dir` was created at `data/<instance>/members/<member>/tools/notion_read_page/`.

## Substitutions for other services

The recipe shape is identical for any stock connector. The substitutions for the next four:

| Service | service_id | auth_type | typical operations | first tool |
|---|---|---|---|---|
| Slack | `slack` | `oauth_device_code` | `read_messages`, `post_message`, `update_status` | `slack_post_message` |
| GitHub | `github` | `api_token` (PAT) | `read_repo`, `read_pr`, `comment_pr`, `create_issue` | `github_read_pr` |
| Drive + Docs | `google_drive` | `oauth_device_code` (shares Gmail's scope) | `read_doc`, `create_doc`, `list_files` | `drive_read_doc` |
| Gmail upgrade | `gmail` | `oauth_device_code` | `read_message`, `send_message`, `manage_label` | `gmail_read_message` |

Each ships under `kernos/kernel/services/<service>.service.json` plus tool files under `kernos/kernel/integrations/<service>/`. The auth-by-channel matrix decides where onboarding can run; `oauth_device_code` services (Slack, Drive, Gmail) onboard from any adapter once the device-code subsystem ships.

## When you hit a snag

If the descriptor won't parse, the validator's error names the offending field. If `register_stock_tools` skips your tool, the boot log has `STOCK_TOOL_LOAD_FAILED` with a reason. If the dispatcher rejects an invocation, the runtime enforcement error names which of the four checks failed.

If you find yourself writing service-specific dispatcher code, gate code, audit code, or onboarding code, stop. The substrate should carry it. That's a kick-back signal — surface it before the codebase grows a per-connector accretion.
