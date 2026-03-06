# KERNOS

<img width="350" height="350" alt="ChatGPT Image Mar 2, 2026, 11_15_22 PM" src="https://github.com/user-attachments/assets/121632b6-9811-4c8e-8126-4f44f2f1ca9f" />

*Kernel for Extensible Runtime, Networked Orchestration Services*

A personal agentic operating system. Text a phone number, get agents working
for you within an hour. No technical knowledge required.

**Status:** Phase 1B complete. Kernel operational with soul, memory projectors,
and behavioral contracts. Phase 2 preparation underway.

## Documentation

| Document | Purpose |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Current status, active spec, decision log. **Start here.** |
| [docs/BLUEPRINT.md](docs/BLUEPRINT.md) | Vision, architecture decisions, implementation phases |
| [docs/TECHNICAL-ARCHITECTURE.md](docs/TECHNICAL-ARCHITECTURE.md) | Living map of what exists — components, data flows, interfaces |
| [docs/KERNEL-ARCHITECTURE-OUTLINE.md](docs/KERNEL-ARCHITECTURE-OUTLINE.md) | The kernel design: five primitives, three operational modes |
| [docs/ARCHITECTURE-NOTEBOOK.md](docs/ARCHITECTURE-NOTEBOOK.md) | Design rationale, deferred decisions, brainstorming insights |
| [research/](research/) | Phase 2 preparation research papers |
| [specs/completed/](specs/completed/) | All completed implementation specs |

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js (required for the Google Calendar MCP server, which runs via `npx`)

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

Edit `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `DISCORD_BOT_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) |
| `TWILIO_ACCOUNT_SID` | Twilio Console (optional — SMS adapter) |
| `TWILIO_AUTH_TOKEN` | Twilio Console (optional — SMS adapter) |
| `TWILIO_PHONE_NUMBER` | Twilio Console (optional — SMS adapter) |
| `OWNER_PHONE_NUMBER` | Your phone number in E.164 format (optional) |

### 3. Run

```bash
# Discord bot (primary)
python kernos/discord_bot.py

# Or via FastAPI (for SMS webhook)
uvicorn kernos.app:app --reload --port 8000
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

## Health Check

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

## License

MIT
