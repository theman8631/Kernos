# Secret storage: OS keychain, hardened `.env`, or plaintext `.env`

Kernos offers three storage backends for provider API keys. You pick one at `kernos setup llm`; you can switch later by re-running setup.

The choice is recorded in `config/storage_backend.yml` so that subsequent runs pick up the right backend automatically.

## Why not just use `.env`?

A plain `.env` file on disk is fine when you know the machine is yours and nobody else has a copy. But:

* Backups and Time Machine / snapshot systems often copy it unmodified.
* A checked-out editor, a synced directory, or a stolen laptop image reveal the contents.
* Plain `.env` is hard to rotate safely — you tend to overwrite in place and hope.

The three options below trade off convenience and exposure. Default: **OS keychain** when available, **hardened `.env`** otherwise. Plaintext requires explicit opt-in.

## 1. OS keychain (recommended)

Kernos calls into the OS credential store via the Python `keyring` library:

| Platform | Underlying store |
|---|---|
| macOS | Keychain |
| Linux | Secret Service API (GNOME Keyring, KWallet via `secretstorage`) |
| Windows | Credential Locker |

Keys never land on disk as files. Kernos namespaces its entries under the service name `kernos`, one entry per env-var name (e.g. `ANTHROPIC_API_KEY`).

**Pros.** Backups don't capture the keys. Other processes can't just `cat` a file to read them. Revocation is a single `keyring delete` call.

**Cons.** Requires a functioning credential store. In minimal Linux containers (e.g. a stripped-down CI image without `libsecret`), it may not be available. When the store isn't available, `kernos setup llm` falls back to offering the hardened `.env` option.

## 2. Hardened `.env`

Kernos writes to `.env` with file mode `0600` (user read/write only) and ensures the parent directory is `0700`. This is the same on-disk format you already use, but with tighter permissions.

**Pros.** Always available, survives reboots, easy to inspect / edit by hand.

**Cons.** Still on disk. If something else on the system has root or reads your home directory, the keys are there.

**Hazard to know.** Backups and snapshots (Time Machine, restic, rsync, file-level cloud sync) will capture `.env`. If you enable one of those, assume anyone with access to the backup has the keys.

## 3. Plaintext `.env` (explicit opt-in)

A normal `.env` file — no special permissions, no keychain. Offered for the cases where the hardened option causes friction (e.g. a container that mounts a read-only `.env` from another location; dev workflows where you intentionally want the file inspectable).

**Opt-in phrase.** At `kernos setup llm`, picking plaintext requires typing the exact string `yes, I accept plaintext storage` — no shortcut, no accidental enable.

## Switching backends later

Re-run `kernos setup llm` and pick a different storage option. The switch runs in a specific order so keys never leak across backends:

1. **Write** every known secret to the target backend.
2. **Verify** by reading each one back from the target.
3. **Remove** each secret from the old backend, only after step 2 succeeds.

If step 2 fails — even for one secret — the switch **aborts**. The old backend is untouched, the partial writes on the target are cleaned up, and `kernos setup llm` reports the error. You can retry.

This ordering is not optional. It's explicit in `kernos/setup/storage_backend.py::switch_storage_backend` and covered by the `test_storage_backend_switch_*` pytest tests, one per pair.

## Reading stored keys at runtime

You don't need to. On startup, Kernos runs a binary health check (does each named chain have at least one provider with a stored credential?) and — if the check passes — copies the relevant secrets from the active backend into `os.environ` so the existing `build_chains_from_env()` path finds them. Existing env-var values win (dev workflows that export an explicit key for a session keep working).

## Changing the default

The startup helper that resolves "which backend should a fresh install offer first?" lives in `kernos/setup/storage_backend.py::detect_default_backend`. It prefers the OS keychain when available, hardened `.env` otherwise. If a platform ships a different kind of secret store we want to default to, update that function.
