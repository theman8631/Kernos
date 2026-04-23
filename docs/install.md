# Install Guide

Complete setup for running Kernos locally.

## Requirements

- **Python 3.11 or later**
- **Node.js** (optional — required for MCP servers that run via `npx`, including Google Calendar integration)
- **An LLM provider credential.** One of:
  - Anthropic API key
  - OpenAI Codex (ChatGPT OAuth credentials)
  - Ollama (local models — no external credential needed, but requires Ollama running locally)
- **At least one messaging adapter credential.** One of:
  - Discord bot token + Discord user ID for the owner
  - Twilio account credentials + phone numbers
  - Telegram bot token

A minimal setup runs on Anthropic + Discord and needs nothing else. Everything beyond that is optional capability expansion.

## Quick install

```
git clone https://github.com/theman8631/Kernos.git
cd Kernos
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` to fill in your credentials. See the [Environment Configuration](#environment-configuration) section below for what each variable does.

Start the server:

```
python kernos/server.py
```

On first run, Kernos creates its data directory (default `./data`) and initializes the SQLite databases for instance state, knowledge facts, relational messages, and conversation logs.

## Environment Configuration

### LLM Provider

Pick one provider by setting `KERNOS_LLM_PROVIDER`:

**Anthropic (default)**
```
KERNOS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

**OpenAI Codex (ChatGPT OAuth)**
```
KERNOS_LLM_PROVIDER=openai-codex
# Either place credentials at .credentials/openai-codex.json, or set via env:
OPENAI_CODEX_ACCESS_TOKEN=...
OPENAI_CODEX_REFRESH_TOKEN=...
OPENAI_CODEX_EXPIRES=...
OPENAI_CODEX_ACCOUNT_ID=...
OPENAI_CODEX_MODEL=gpt-5.4
```

**Ollama (local)** — configure via fallback-chain environment variables. See the provider chain section below.

### Messaging Adapters

Kernos supports multiple messaging channels simultaneously. Each adapter is independent; configure any you want to use.

**Discord**
```
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_OWNER_ID=your-discord-user-id
KERNOS_INSTANCE_ID=discord:your-discord-user-id
```

The `KERNOS_INSTANCE_ID` ties your Discord and SMS identities to the same Kernos instance. Format is `discord:USER_ID`.

**Twilio SMS** (optional)
```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
OWNER_PHONE_NUMBER=+1...
```

**Telegram** (optional)
```
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
```

When `TELEGRAM_BOT_TOKEN` is set, Kernos starts its Telegram poller on server startup; when unset, the Telegram adapter is skipped cleanly.

### Google Calendar (optional)

For calendar integration via MCP:

```
GOOGLE_OAUTH_CREDENTIALS_PATH=/path/to/your/gcp-oauth.keys.json
```

Create OAuth credentials at Google Cloud Console → APIs & Services → Credentials. Node.js is required because the Google Calendar MCP server runs via `npx`.

### Optional Services

```
BRAVE_API_KEY=...          # Web search via Brave Search API
VOYAGE_API_KEY=...         # Enhanced embeddings (falls back to hash-only dedup)
```

These are not required. Missing credentials degrade cleanly — search falls back to what's available, embeddings fall back to hash-based dedup.

### Storage

```
KERNOS_DATA_DIR=./data     # Root directory for all persistent data
```

Kernos creates subdirectories under this for instances, spaces, members, conversation logs, and diagnostics.

### Provider Fallback Chains

Kernos uses three named provider chains (`primary`, `simple`, `cheap`) built from environment configuration. Default construction uses the primary provider (Anthropic or Codex) for all three chains, but chains can be customized. See `kernos/providers/chains.py` for chain construction.

Ollama endpoints are configured through chain-specific variables.

### Debug & Testing

```
# Override compaction threshold for testing
# (default 8000 estimated tokens; set 500 to trigger after ~5-6 exchanges)
KERNOS_COMPACTION_THRESHOLD=500
```

## Verifying Install

After `python kernos/server.py` starts, Kernos begins listening on its configured adapters. Send a message to your Discord bot or SMS number. The first interaction triggers onboarding — Kernos will ask you your name, then guide you through a conversational hatching sequence that produces your per-member agent.

Run the test suite to verify the local install:

```
pytest -q
```

A clean install with all dependencies present should show a green test run.

## Running in Production

Kernos is a single-process runtime. Deployment options:

- **Local machine** — run under `tmux` or similar; simple, low-overhead
- **Always-on VPS** — run under systemd or supervisor; recommended for actual personal use
- **Container** — Dockerfile forthcoming; build your own from the quick-install steps

Kernos has no database server requirement (SQLite is embedded) and no external orchestration requirement. A 1GB-RAM VPS running Python 3.11+ is sufficient.

Do not run Kernos behind a public HTTP endpoint without careful consideration — adapters like Discord and Telegram are designed for authenticated-user messaging, not public HTTP surfaces. The current architecture assumes the server process is reachable by its message adapters, not by arbitrary internet traffic.

## Troubleshooting

**Import errors on first run.** Confirm the virtual environment is activated and `pip install -e .` completed without errors. On some systems, Python 3.11 isn't the default; use `python3.11` explicitly.

**"No LLM provider configured" errors.** Check that `KERNOS_LLM_PROVIDER` matches a configured provider credential. The default is `anthropic`; if you're using Codex, set the variable explicitly.

**Discord bot doesn't respond.** Check that `DISCORD_BOT_TOKEN` is valid and the bot has been invited to a server or direct-messaged by the configured `DISCORD_OWNER_ID`. The bot needs message-read and message-send permissions.

**Compaction triggers incorrectly.** The default compaction threshold is tuned for production context windows. If testing with very short sessions, either wait for natural turn counts or override `KERNOS_COMPACTION_THRESHOLD`.

**MCP servers fail to connect.** Node.js must be installed and `npx` must be on the PATH. Check that the MCP server packages can be invoked directly before debugging Kernos-side integration.

## Getting Help

Kernos is a single-maintainer project at early-access stage. Report issues at the [GitHub repo](https://github.com/theman8631/Kernos).

## Related

- **[README](../README.md)** — project overview
- **[Roadmap](roadmap.md)** — what's coming next
