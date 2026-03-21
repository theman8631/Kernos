# CLI Chat

Command-line interface for interacting with Kernos directly. Same handler, same spaces, same knowledge as Discord or SMS.

## Usage

```bash
source .venv/bin/activate
python -m kernos.chat                          # interactive tenant picker
python -m kernos.chat -t "discord:123..."      # connect to existing tenant
python -m kernos.chat -n "cli:testing"         # create fresh tenant
python -m kernos.chat -q                       # quiet (suppress logs)
python -m kernos.chat --script messages.txt    # send file of messages
```

## Key Properties

- **Same tenant, different door** — connecting to an existing tenant shares all state
- **New tenants start clean** — `--new` triggers full onboarding
- **Script mode** — one message per line, `#` for comments, sequential send with responses
- **No outbound capability** — CLI is interactive only, cannot push messages

## Channel Status

Appears in `manage_channels list` as "CLI Terminal" with `can_send_outbound = False`.
