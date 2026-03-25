# KERNOS

<img width="350" height="350" alt="Kernos Logo" src="https://github.com/user-attachments/assets/121632b6-9811-4c8e-8126-4f44f2f1ca9f" />

*Kernel for Extensible Runtime, Networked Orchestration Services*

A personal intelligence that lives in the cloud, works 24/7, is reachable by
text message, and earns trust through thousands of correct small actions. Built
for non-technical users first, with kernel-level safety, memory, and
orchestration under the hood.

**Status:** 1218 tests passing. Core infrastructure shipped. Active development
focused on conversation continuity, compaction, and deep recall.

## What Kernos Does

- **Works across channels** — Discord and SMS share one brain. Switch channels mid-conversation without losing context
- **Manages your time** — Google Calendar integration, scheduled reminders, proactive awareness ("your dentist appointment is in 30 minutes")
- **Respects boundaries** — behavioral contracts (covenants) that the user defines and the system enforces. "Never send emails without asking" is infrastructure, not a suggestion
- **Builds long-term memory** — structured memory with entity resolution, fact dedup, and two-tier recall
- **Learns your preferences** — extracts behavioral patterns from conversation and adapts
- **Searches and browses** — web search and full page browsing built in
- **Schedules and reminds** — "Remind me to check on Henderson in 2 hours" just works

## Architecture

Kernos is built on five primitives: **Memory** (knowledge graph + entity resolution), **Context Spaces** (topic-based conversation routing), **Behavioral Contracts** (covenants the user defines), **Capabilities** (MCP-based tool integration), and **Awareness** (proactive signals from the environment).

The agent thinks freely. The kernel enforces safety. Tool calls go through a dispatch gate. Covenants and validation layers shape what the agent is allowed to do and say. The system prompt creates a confident agent — infrastructure handles the guardrails.

### Key Design Principles

- **Memory as the moat** — persistent, structured, evolving knowledge is the core differentiator
- **Ambient, not demanding** — works without requiring user presence
- **No destructive deletions** — shadow archive architecture
- **Multi-tenancy from day one** — every state piece keyed to `tenant_id`
- **Provider-flexible** — supports Anthropic (Claude) and OpenAI Codex (ChatGPT OAuth)

## Current State

Kernos is functional and under active development. The core infrastructure —
memory, context spaces, behavioral contracts, scheduling, proactive awareness,
and cross-channel communication — is in place. Current work is improving
conversation continuity, compaction pipelines, and deep recall.

## Documentation

| Document | Purpose |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Current project status and active decisions |
| [docs/TECHNICAL-ARCHITECTURE.md](docs/TECHNICAL-ARCHITECTURE.md) | As-built architecture — what exists in code right now |
| [docs/KERNEL-ARCHITECTURE-OUTLINE.md](docs/KERNEL-ARCHITECTURE-OUTLINE.md) | Kernel design: five primitives, three operational modes |
| [docs/](docs/) | Self-documentation system — capabilities, behaviors, architecture, identity |

### Historical Reference

| Document | Purpose |
|---|---|
| [docs/BLUEPRINT.md](docs/BLUEPRINT.md) | Original vision document (Feb 2026) — vision is current, implementation details evolved |
| [docs/ARCHITECTURE-NOTEBOOK.md](docs/ARCHITECTURE-NOTEBOOK.md) | Design rationale from Phases 1A–2 — some sections current, some superseded |

## What's Shipped

**Phase 1 — Foundation:**
SMS gateway, Discord adapter, Google Calendar MCP, basic persistence, event stream, reasoning service, capability graph, task engine, tenant isolation, memory projectors.

**Phase 2 — Memory + Context Intelligence:**
Entity resolution + fact dedup, context space routing (LLM router), compaction (Ledger + Living State), active retrieval + NL contract parser.

**Phase 3 — Agent Workspace + Safety Infrastructure:**
Per-space file system, tool scoping + MCP installation, proactive awareness, dispatch gate, self-documentation, covenants, scheduler, cross-channel instance identity, lazy tool loading, and per-space conversation logs.

**Current — Conversation Log Pipeline:**
Space-based conversation logs replacing channel-specific storage. P1 (write), P2 (context assembly), P3 (compaction) shipped. P4 (deep recall) in progress.

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js (required for MCP servers that run via `npx`)

### 1. Clone and install

```bash
git clone https://github.com/theman8631/Kernos.git
cd Kernos
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials. See `.env.example` for the full
list of supported variables.

Key variables:

| Variable | Purpose |
|---|---|
| `KERNOS_LLM_PROVIDER` | `anthropic` (default) or `openai-codex` |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) — required if using Anthropic |
| `DISCORD_BOT_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) |
| `KERNOS_INSTANCE_ID` | Instance identifier for cross-channel identity (e.g., `discord:YOUR_ID`) |
| `TWILIO_ACCOUNT_SID` | Twilio Console (optional — SMS adapter) |
| `TWILIO_AUTH_TOKEN` | Twilio Console (optional — SMS adapter) |
| `TWILIO_PHONE_NUMBER` | Twilio Console (optional — SMS adapter) |

### 3. Run

```bash
# Kernos server (Discord + SMS + awareness)
python kernos/server.py
```

## CLI Usage

The `./kernos-cli` wrapper runs CLI commands without needing to activate the venv.

```bash
./kernos-cli tenants                          # List all known tenants
./kernos-cli events <tenant_id>               # View recent events
./kernos-cli profile <tenant_id>              # View tenant profile
./kernos-cli soul <tenant_id>                 # Inspect agent soul
./kernos-cli knowledge <tenant_id>            # View knowledge entries
./kernos-cli contracts <tenant_id>            # View behavioral contract rules
./kernos-cli capabilities                     # View capability registry
./kernos-cli costs <tenant_id>                # View cost/token summary
./kernos-cli tasks <tenant_id>                # View task lifecycle
```

Each subcommand supports `--help` for full options.

## Google Calendar Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable the **Google Calendar API**
3. Create **OAuth client ID** (Desktop app) and download the JSON file
4. Set `GOOGLE_OAUTH_CREDENTIALS_PATH` in `.env` to that file's path
5. Run auth once:
   ```bash
   GOOGLE_OAUTH_CREDENTIALS=/path/to/gcp-oauth.keys.json npx @cocal/google-calendar-mcp auth
   ```
6. Authorize in browser. Tokens saved locally for future use.

## License

MIT
