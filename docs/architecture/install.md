# Install for Stock Connectors

Closes the four gaps surfaced by the install diagnostic
(2026-04-26): credential-key first-run timing, hidden CLI surface,
self_update blind to non-Python substrate, no service-discovery
surface for the operator.

This doc captures the substrate added by INSTALL-FOR-STOCK-CONNECTORS.
Companion to `docs/workshop/external-services.md` and the WORKSHOP
primitive — neither is changed; this layers per-install state and
operator surfacing on top.

## Conceptual shape

All stock connectors install by default (code present, descriptor
registered) but are **disabled by default per install**. The
operator chooses which to enable — at first-run setup or any time
later via `kernos services enable <id>`. Disabled means: code
present, descriptor registered, **NOT surfaced to the agent**, AND
**dispatch refuses invocation**. Later enable does not trigger a
build or download — the substrate is already there.

## ServiceState + ServiceStateStore

Per-install state lives at `<data_dir>/install/service_state.json`.
One file per Kernos install, regardless of how many instances live
under the data dir.

```
ServiceState {
  service_id: str
  enabled: bool
  source: "default" | "operator" | "setup" | "migration"
  updated_at: ISO 8601 UTC
  updated_by: "operator" | "system" | "migration"
  reason: str
}
```

`ServiceStateStore` is the single source of truth. All reads
through the store; all writes atomic (write-temp + rename); cache
invalidates on every write so the surfacing layer and dispatch
layer pick up changes immediately. Conservative default:
`is_enabled` returns False for unknown services.

## Two-layer enforcement

Disabled services are enforced at two layers (mirrors the WORKSHOP
primitive's pattern: surfacing reduces visibility; invocation
enforces authority).

| Layer | What it does |
|---|---|
| **Surfacing** | Catalog entries stay registered (so `kernos services list` can show them). Disabled tools never appear in agent-facing tool lists; `assemble.py` pre-populates its exclude set with `ToolCatalog.disabled_tool_names(disabled_service_ids)` so the surfacer never offers them. |
| **Dispatch** | `enforce_invocation` runs `check_service_enabled` before credential scope. A disabled service refuses dispatch with `ServiceDisabledError`; the credential store is never queried. Audit emit lands under `install.dispatch_refused_disabled_service` (distinct from the workshop's tool.invocation.* shape). |

Surfacing is UX. Dispatch is the security boundary — a leaked
tool ID via logs, stale state, or test code can't invoke a
disabled service.

## CLI surface

| Command | What it does |
|---|---|
| `kernos services list` | Joins registry against state store; shows enabled / disabled / unset per service with totals. |
| `kernos services enable <id>` | Writes state with source=operator, updated_by=operator. Optional `--reason`. |
| `kernos services disable <id>` | Same as enable but enabled=False. Reminds operator to revoke credentials separately. |
| `kernos services info <id>` | Descriptor (auth type, operations, scopes, audit_category, notes), install state with full provenance, channel matrix per auth_type, credential key state, install_health summary, onboarding next step. |
| `kernos credentials onboard/revoke/list/oauth ...` | Top-level wrapper around the existing module-level CLI. Backwards-compat: `python -m kernos.kernel.credentials_cli` keeps working. |
| `kernos setup` | First-run service-state setup. TTY → interactive prompts; non-TTY → auto-bootstrap all-disabled with logged enable instructions. |
| `kernos setup llm` | Existing LLM setup console (unchanged). |

## First-run flow

| Context | Behavior |
|---|---|
| TTY, no service_state.json | Stdlib `input()` prompts per service; suggests prior value as default; persists with source=setup, updated_by=operator. |
| Non-TTY, no service_state.json | Auto-bootstrap all-disabled. Logs enable instructions. Never blocks. |
| `--non-interactive --enable-services X,Y` | Enables only the named services; rejects unknown service ids without writing partial state. |
| `--all-enabled` | Enables every shipped service (development / test installs). |
| Existing service_state.json | TTY: re-prompts with prior values as defaults. Non-TTY: skips, logs "state exists" message, never blocks. |

**Daemon never invokes setup.** If `service_state.json` doesn't
exist, the daemon boot path treats this as "all disabled, surface
nothing" and continues. Setup is an explicit operator surface,
not something the daemon hijacks.

No TUI dependency in v1 — stdlib `input()` and `print()` only.

## Credential key

Generation is exclusively an operator-driven event:

- `kernos setup`
- `kernos credentials onboard <service>`
- First credential write

Permissions enforced: parent directory locked to `0700`, key file
to `0600`. Generation refuses on filesystems that don't honor POSIX
permissions (FAT, certain network mounts) — operators set
`KERNOS_CREDENTIAL_KEY` explicitly in those cases.

`kernos services info <id>` surfaces key state: source
(env-var-managed vs. auto-generated per-instance), permissions
policy, backup recommendation. The original single warning log at
generation time now appears in three independent places:
`install.credential_key_generated` audit entry, `services info`,
generation-time stdout.

**Install hooks may NOT generate, rotate, or overwrite the
credential key.** Two-layer refusal:

- Registration: `HookDescriptor.attempts_credential_key_generation=True` rejected by the registry with a future-spec landing pointer.
- Runtime: `refuse_credential_key_generation` context wraps every hook's check/apply via thread-local guard. A hook that slips past registration and calls `_resolve_key` raises immediately with the offending hook_id named.

## Install hook runner

Shared module called from BOTH `kernos setup` (fresh install) and
`self_update.py` (updates). Same registry; same hook set; same
status store.

```
HookDescriptor {
  hook_id: snake_case
  check: (HookContext) -> CheckResult(needs_apply, status, details)
  apply: (HookContext) -> ApplyResult(success, message, details)
  phase: pre_setup | post_setup | post_update | None
  order_after: tuple[hook_id, ...]
  idempotent: True              # required in v1
  attempts_credential_key_generation: False  # required False
}
```

Execution rules:

- All hooks must declare `idempotent=True`. Non-idempotent
  rejected at registration.
- `order_after` declares dependencies. Topological order honored;
  cycles + unknown dependencies rejected at registration. Stable
  tie-break on registration order.
- Each hook's `check` runs first. `apply` runs only if `check`
  returns `needs_apply=True`. `check=False` records
  `skipped_check`; the runner does not invoke apply.
- Failed hook is loud (error to operator with last_error
  message) and non-fatal (other hooks continue; install
  completes). Failed hooks persist in `data/install/hook_status.json`
  and surface via `kernos services info` install_health.

Shipped hooks (`build_default_registry()`):

- `service_state_init` — creates `data/install/` with 0700.
- `credential_key_path` — runs after `service_state_init`; walks
  instance dirs and tightens any `credentials/` subdir to 0700.
  Never generates keys.

Subsequent specs add hooks by calling `.register(descriptor)` on
the registry returned by `build_default_registry()`.

## Migration of existing installs

Operators with a pre-spec install (data dir contains instance
subdirs but no `service_state.json`) get a synthetic state with
`source=migration, updated_by=migration` and **all services
enabled** (preserves current behavior). Headless-safe:

- TTY: one-time review prompt next CLI invocation.
- Non-TTY: silent migration; recommendation logged for later
  review. Never blocks.

`install_appears_existing(data_dir)` is the heuristic: True iff
the data dir contains at least one subdirectory other than
`install/`.

## Audit categories

All install events compose with the existing `tool.*`, `cohort.*`,
`integration.*` categories. New categories per Section 11:

- `install.first_run_completed`
- `install.service_state_changed`
- `install.credential_key_generated`
- `install.hook_executed`
- `install.migration_completed`
- `install.dispatch_refused_disabled_service`

Member-scoped where applicable; install-scoped otherwise.

## Architectural placement

```
kernos/setup/
├── service_state.py            # ServiceState + ServiceStateStore + migration
├── services_cli.py             # `kernos services` subcommand handlers
├── first_run.py                # `kernos setup` first-run flow
└── install_hooks/
    ├── runner.py               # HookRunner, HookDescriptor, HookStatusStore
    └── hooks/
        ├── service_state_init.py
        └── credential_key_path.py

kernos/kernel/
├── tool_runtime_enforcement.py # check_service_enabled, ServiceDisabledError
├── tool_catalog.py             # disabled_tool_names()
└── credentials_member.py       # refuse_credential_key_generation context

kernos/cli.py                   # `services` + `credentials` + `setup` subparsers
kernos/messages/phases/assemble.py  # surfacing-layer disabled filter
kernos/setup/self_update.py     # run_post_update_hooks() after pip install
```

## Composition

- **WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE** unchanged. ServiceState
  layered on top.
- **All shipped stock connectors** ship as-is. The enabled flag
  is per-install, not per-connector-spec.
- **Surfacing primitives** (universal catalog, three-tier
  surfacing, lazy promotion, context-space pinning): gain the
  disabled-filter as a hard override before relevance-surfacing
  applies.
- **OAUTH-DEVICE-CODE-SUBSYSTEM** unchanged. Top-level CLI wiring
  exposes the existing flows.
- **Future TOOL-SURFACING-RELEVANCE**: composes cleanly. Disabled
  is hard; relevance is soft.
- **Future TOOL-COHORT-PROMOTION**: unaffected. Promotion happens
  within the enabled set.

## Path forward

Once shipped:

- Fresh operator can run Kernos without codebase awareness.
  Onboarding is a discoverable surface.
- Future stock connectors ship without operator-side install
  ceremony beyond `kernos services enable <service>`.
- Install hooks pattern means future substrate that needs install-
  time work has a clear architectural home — and the historical
  Playwright pattern (single-failure-point in self_update) is no
  longer the failure mode.
- Per-member service overlay (future spec) has an install-level
  base to layer on top of.
