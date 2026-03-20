# Dispatch Gate

The dispatch gate guards every write/action tool call before execution. Read operations pass silently. Writes go through three-step authorization.

## How It Works

When the agent calls a tool classified as `soft_write` or `hard_write`, the gate fires:

### Step 1: Token Check
Programmatic approval tokens for API callers. If a valid, unused token matches the tool call, it passes immediately. This is the mechanism for confirmed pending actions.

### Step 2: Permission Override
Fast dictionary lookup on the tenant profile's `permission_overrides`. If the capability has an `"always-allow"` override, the gate is bypassed entirely. Permission overrides are mechanical — they are NOT in the covenant rules text shown to the model.

### Step 3: Model Evaluation
One cheap Haiku call sees:
- Recent user messages (last few turns)
- The agent's reasoning that led to the tool call
- The proposed tool call with arguments
- Active covenant rules

The model returns one of four verdicts:

- **EXPLICIT** — the user clearly asked for this action
- **AUTHORIZED** — a standing rule or clear context covers it
- **CONFLICT** — the user asked for it, but a `must_not` covenant applies. The agent tells the user about the conflict.
- **DENIED** — no authorization found

## What Happens When Blocked

When the gate returns DENIED or CONFLICT:

1. The action becomes a `PendingAction` stored on the reasoning service
2. The agent's response includes a `[CONFIRM:N]` tag (e.g., `[CONFIRM:1]`)
3. If the user replies confirming, the handler replays the tool call with an approval token
4. Pending actions expire after 1 hour

## Effect Classification

| Effect | Gate behavior | Examples |
|--------|-------------|----------|
| read | Bypass (no gate) | remember, list_files, read_file, read_soul, read_source, read_doc |
| soft_write | Gate evaluates | write_file, delete_file, update_soul, manage_covenants, evaluate (browser JS) |
| hard_write | Gate evaluates | create-event, send-email, delete-event |

Unknown tools default to `hard_write` (safe default).

## Code Locations

| Component | Path |
|-----------|------|
| Gate logic | `kernos/kernel/reasoning.py` (_gate_tool_call, _evaluate_gate) |
| GateResult, PendingAction | `kernos/kernel/reasoning.py` |
| Confirmation replay | `kernos/messages/handler.py` |
| Permission overrides | `kernos/kernel/state.py` (TenantProfile.permission_overrides) |
