# Live Verification: Phase 1A.4 — Persistence

**Deliverable:** 1A.4
**Status:** PENDING
**Last tested:** not yet

## Prerequisites

- 1A.3 live verified (calendar MCP working)
- Full setup complete (uvicorn running, Discord adapter connected)
- `KERNOS_DATA_DIR` set or defaulting to `./data`

## Setup

1. Start the server: `uvicorn kernos.app:app --reload --port 8000`
2. Ensure Discord bot is running and connected
3. If `data/` directory exists from testing, consider removing it for a clean start (or test with existing data to verify continuity)

## Agent Awareness (Cold Session)

Start a fresh conversation after a restart. These must be the first messages.

| # | Ask (exact message) | What you're checking | Good answer | Bad answer | Status |
|---|---|---|---|---|---|
| 1 | "What are you and what can you do?" | Identity + capabilities | Identifies as Kernos, mentions calendar access. Does NOT claim email or other tools it doesn't have | Missing capabilities or hallucinated ones | ⬜ |
| 2 | "Do you remember our previous conversations?" | Memory awareness | Honest answer — should acknowledge it has conversation memory (no longer disclaims it) | Claims no memory (old system prompt language) | ⬜ |
| 3 | "What tools do you have?" | Tool inventory | Calendar. Nothing else. | Hallucinates extra tools | ⬜ |

**If any of these fail:** Fix system prompt before proceeding.

## Conversation Memory Tests

| # | Action | Expected | Status |
|---|---|---|---|
| 4 | Send "My name is [name]" | Acknowledges your name | ⬜ |
| 5 | Send "What's my name?" | Responds with your name from message 4 | ⬜ |
| 6 | Send "What's on my schedule today?" | Calendar tool works — no regression from 1A.3 | ⬜ |
| 7 | Send "What did I just ask you about?" | References the schedule question (history works within session) | ⬜ |

## Persistence Across Restart

| # | Action | Expected | Status |
|---|---|---|---|
| 8 | Kill uvicorn (Ctrl+C), restart it | Server comes back up cleanly | ⬜ |
| 9 | Send "What's my name?" | Still responds with your name from message 4 (survived restart!) | ⬜ |
| 10 | Send "What were we talking about before?" | References calendar/schedule from message 6 | ⬜ |

**This is THE test.** If message 9 works — if the agent remembers your name across a restart — persistence is real.

## Auto-Provisioning Test

| # | Action | Expected | Status |
|---|---|---|---|
| 11 | Have a different Discord user (alt account, friend) send a message | System responds normally. No errors. | ⬜ |
| 12 | Check `data/` directory | New tenant directory created for the second user | ⬜ |
| 13 | Send a message from the original account | Original tenant's context is intact, separate from the new user | ⬜ |

## Data Verification (Manual Checks)

| # | Check | Expected | Status |
|---|---|---|---|
| 14 | Open `data/{tenant_id}/tenant.json` | Contains tenant_id, status: "active", created_at, capabilities | ⬜ |
| 15 | Open `data/{tenant_id}/conversations/{conversation_id}.json` | Array of entries with role, content, timestamp, platform. User messages and assistant responses only — NO tool calls | ⬜ |
| 16 | Open `data/{tenant_id}/audit/{date}.json` | Contains tool_call entries from the calendar query (tool name, input, output) | ⬜ |
| 17 | Check `data/{tenant_id}/archive/` | Directory exists with subdirectories (conversations, email, files, calendar, contacts, memory, agents). All empty — that's correct. | ⬜ |

## The Real Test (Use It For a Day)

The acceptance criteria above are mechanical. The real completion criteria — the one that tells you the product works — is when you message Kernos in the morning and Kernos says something that proves it remembers yesterday. Not because it was programmed to greet you with a summary, but because the conversation history naturally contains yesterday's context and Claude uses it. That's the moment it stops being a demo and starts being useful.

## Troubleshooting

- **"Something went wrong" on every message:** Check that `KERNOS_DATA_DIR` is writable and the path exists (or can be created).
- **Agent claims it can't remember:** System prompt still has the old "cannot remember previous conversations" text. Check `_build_system_prompt()`.
- **Restart loses memory:** Check that conversation files are being written to `data/`. Check file permissions. Check that the data directory isn't inside a temp folder.
- **Calendar stopped working:** Persistence changes shouldn't affect MCP. Check that MCPClientManager is still being initialized properly in app.py/discord_bot.py.
- **Different users see each other's data:** tenant_id derivation is wrong. Check derive_tenant_id() produces different IDs for different senders.
