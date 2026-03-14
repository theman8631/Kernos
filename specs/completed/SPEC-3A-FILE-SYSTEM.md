# SPEC-3A: Per-Space File System

**Status:** READY FOR REVIEW
**Depends on:** Phase 2 complete, compaction system (2C) operational
**Objective:** Give the agent the ability to create, read, and manage persistent text files within context spaces. Files are artifacts — drafts, configs, outputs, notes. The agent creates them, references them later, and the compaction system maintains awareness of what exists.

**What changes for the user:** The agent can produce persistent work products. "Draft an NDA for Henderson" creates a file in the Business space. "Show me the campaign notes" reads a file from the D&D space. Files survive across sessions — compaction knows they exist, the agent can find and read them on demand.

**What changes architecturally:** Four new kernel-managed tools (write_file, read_file, list_files, delete_file) routed through the ReasoningService intercept alongside remember(). Per-space file directories. File manifest tracked in the compaction Living State. Soft delete with recovery.

**What this is NOT:**
- Not binary file support (images, PDFs — separate backlog item, each format needs its own pipeline)
- Not cross-space file access (files belong to one space in 3A)
- Not file versioning (agent creates v2 explicitly if it needs history)
- Not MCP server configuration (that's 3B, which uses 3A's file primitives)

-----

## Component 1: File Storage

### Directory structure

```
data/{tenant_id}/spaces/{space_id}/files/
    henderson-sow-draft.md
    campaign-notes.md
    meeting-prep.txt
    .deleted/
        old-draft.md_2026-03-15T14:30:00
```

Directories are created lazily on first `write_file` call — no empty directories at space creation.

### Config hooks

```python
# On ContextSpace dataclass — config hooks for future enforcement
@dataclass
class ContextSpace:
    # ... existing fields ...
    max_file_size_bytes: int | None = None   # None = unlimited
    max_space_bytes: int | None = None       # None = unlimited
```

No enforcement logic in 3A. The hooks exist so usage tiers can be wired without schema changes. Default: unlimited.

### Text-only constraint

All files are UTF-8 text. The `write_file` tool validates content is text before writing. Binary data is rejected with an error message: "File system currently supports text files only. Binary support (images, PDFs) is coming in a future update."

-----

## Component 2: Four Agent Tools

**New file:** `kernos/kernel/files.py`

All four tools are kernel-managed — routed through the ReasoningService intercept, same pattern as `remember()`. They never reach MCP.

### Tool definitions

```python
FILE_TOOLS = [
    {
        "name": "write_file",
        "description": (
            "Create or update a text file in the current context space. "
            "Use this for drafts, notes, configs, research docs, or any "
            "persistent artifact. The description is required — it helps "
            "you find this file later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename including extension (e.g. 'henderson-sow.md', 'session-notes.txt')"
                },
                "content": {
                    "type": "string",
                    "description": "The full text content of the file"
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence description of what this file is — shown in file listings"
                }
            },
            "required": ["name", "content", "description"]
        }
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file in the current context space. "
            "Use list_files first if you're not sure what files exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename to read"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "list_files",
        "description": (
            "List all files in the current context space with their descriptions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "delete_file",
        "description": (
            "Delete a file from the current context space. "
            "The file is preserved for recovery but removed from listings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename to delete"
                }
            },
            "required": ["name"]
        }
    },
]
```

### FileService

```python
class FileService:
    """Manages per-space file operations.

    All operations scoped to a single space. No cross-space access.
    Files are text-only. Binary rejected with error message.
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)

    def _space_files_dir(self, tenant_id: str, space_id: str) -> Path:
        return self.data_dir / _safe_name(tenant_id) / "spaces" / space_id / "files"

    def _deleted_dir(self, tenant_id: str, space_id: str) -> Path:
        return self._space_files_dir(tenant_id, space_id) / ".deleted"

    async def write_file(
        self, tenant_id: str, space_id: str,
        name: str, content: str, description: str,
    ) -> str:
        """Create or overwrite a text file. Returns confirmation message."""
        # Validate text content
        try:
            content.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return "Error: File system currently supports text files only."

        # Validate filename (no path traversal, no special chars)
        if not self._valid_filename(name):
            return f"Error: Invalid filename '{name}'. Use alphanumeric characters, hyphens, underscores, and dots."

        files_dir = self._space_files_dir(tenant_id, space_id)
        files_dir.mkdir(parents=True, exist_ok=True)

        file_path = files_dir / name
        is_update = file_path.exists()
        file_path.write_text(content, encoding="utf-8")

        # Save manifest entry
        await self._update_manifest(tenant_id, space_id, name, description)

        action = "Updated" if is_update else "Created"
        return f"{action} '{name}' ({len(content)} chars). Description: {description}"

    async def read_file(
        self, tenant_id: str, space_id: str, name: str,
    ) -> str:
        """Read a file's contents. Returns content or error message."""
        file_path = self._space_files_dir(tenant_id, space_id) / name
        if not file_path.exists():
            return f"Error: File '{name}' not found. Use list_files to see available files."
        return file_path.read_text(encoding="utf-8")

    async def list_files(
        self, tenant_id: str, space_id: str,
    ) -> str:
        """List all files with descriptions. Returns formatted text."""
        manifest = await self._load_manifest(tenant_id, space_id)
        if not manifest:
            return "No files in this space yet."

        lines = []
        for name, desc in sorted(manifest.items()):
            file_path = self._space_files_dir(tenant_id, space_id) / name
            size = file_path.stat().st_size if file_path.exists() else 0
            lines.append(f"  {name} ({size} bytes) — {desc}")

        return f"Files in this space ({len(manifest)}):\n" + "\n".join(lines)

    async def delete_file(
        self, tenant_id: str, space_id: str, name: str,
    ) -> str:
        """Soft delete — move to .deleted/, remove from manifest."""
        file_path = self._space_files_dir(tenant_id, space_id) / name
        if not file_path.exists():
            return f"Error: File '{name}' not found."

        # Move to .deleted/ with timestamp
        deleted_dir = self._deleted_dir(tenant_id, space_id)
        deleted_dir.mkdir(parents=True, exist_ok=True)
        dest = deleted_dir / f"{name}_{_now_iso().replace(':', '-')}"
        file_path.rename(dest)

        # Remove from manifest
        await self._remove_from_manifest(tenant_id, space_id, name)

        return f"Deleted '{name}'. File preserved for recovery."

    def _valid_filename(self, name: str) -> bool:
        """Validate filename — no path traversal, reasonable characters."""
        if not name or "/" in name or "\\" in name or ".." in name:
            return False
        if name.startswith("."):
            return False
        # Allow alphanumeric, hyphens, underscores, dots
        import re
        return bool(re.match(r'^[\w\-. ]+$', name))
```

### File manifest (per-space metadata)

The manifest is a lightweight JSON file tracking filenames and descriptions:

```
data/{tenant_id}/spaces/{space_id}/files/.manifest.json
```

```json
{
    "henderson-sow-draft.md": "SOW amendment for Henderson Ironclad operations expansion",
    "campaign-notes.md": "Running session notes for the Veloria D&D campaign",
    "meeting-prep.txt": "Thursday meeting prep — Henderson + two ops leads"
}
```

The manifest is the fast-path for `list_files()` — no directory scanning needed. It's also the source for the compaction Living State file awareness.

```python
    async def _load_manifest(self, tenant_id: str, space_id: str) -> dict[str, str]:
        manifest_path = self._space_files_dir(tenant_id, space_id) / ".manifest.json"
        if not manifest_path.exists():
            return {}
        return json.loads(manifest_path.read_text())

    async def _update_manifest(
        self, tenant_id: str, space_id: str, name: str, description: str,
    ) -> None:
        manifest = await self._load_manifest(tenant_id, space_id)
        manifest[name] = description
        manifest_path = self._space_files_dir(tenant_id, space_id) / ".manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

    async def _remove_from_manifest(
        self, tenant_id: str, space_id: str, name: str,
    ) -> None:
        manifest = await self._load_manifest(tenant_id, space_id)
        manifest.pop(name, None)
        manifest_path = self._space_files_dir(tenant_id, space_id) / ".manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
```

-----

## Component 3: Kernel Tool Routing

**Modified file:** `kernos/kernel/reasoning.py`

All four file tools route through the ReasoningService kernel intercept. Same pattern as `remember()`.

```python
# In ReasoningService, tool call handling:

KERNEL_TOOLS = {"remember", "write_file", "read_file", "list_files", "delete_file"}

if tool_name in KERNEL_TOOLS:
    if tool_name == "remember":
        result = await self.retrieval.search(...)
    elif tool_name == "write_file":
        result = await self.files.write_file(
            tenant_id, active_space_id,
            tool_args["name"], tool_args["content"], tool_args["description"],
        )
    elif tool_name == "read_file":
        result = await self.files.read_file(
            tenant_id, active_space_id, tool_args["name"],
        )
    elif tool_name == "list_files":
        result = await self.files.list_files(tenant_id, active_space_id)
    elif tool_name == "delete_file":
        # Principle enforcement: "never delete data without owner awareness"
        # Delete is only allowed when the user explicitly requested it
        # in the current message. The agent cannot self-initiate deletion.
        # This is a hardcoded principle, not a covenant rule lookup.
        # user_message comes from request.input_text — already in scope
        # during the tool-use loop.
        delete_allowed = self._check_delete_allowed(request.input_text)
        if not delete_allowed:
            result = (
                "I can't delete files on my own — that's a standing principle. "
                "If you'd like me to remove a file, just ask directly "
                "(e.g. 'delete old-draft.md')."
            )
        else:
            result = await self.files.delete_file(
                tenant_id, active_space_id, tool_args["name"],
            )
    # Return result as tool_result to the LLM
```

### delete_file principle enforcement

Delete is blocked by default — not by covenant rules, but by a hardcoded principle. The check verifies that the user explicitly requested deletion in the current message. The agent cannot self-initiate file deletion regardless of covenant state.

```python
def _check_delete_allowed(self, user_message: str) -> bool:
    """Delete only allowed when the user explicitly requested it.

    Checks the current user message for delete intent.
    The agent cannot self-initiate deletion — this is a kernel principle,
    not a model instruction.
    """
    delete_signals = [
        "delete", "remove", "get rid of", "trash",
        "clean up", "clear out", "throw away", "discard",
        "drop", "nuke", "wipe", "erase",
    ]
    msg_lower = user_message.lower()
    return any(signal in msg_lower for signal in delete_signals)
```

**How `user_message` reaches the intercept:** The ReasoningService's `reason()` method receives a `ReasoningRequest` which contains `input_text` — the user's current message. At tool call intercept time (inside the tool-use loop), the reasoning service has access to `request.input_text`. This is the same field used for all other request processing. No new wiring needed — the user message is already in scope when tool calls are evaluated.

This means:
- User says "delete the old draft" → agent calls delete_file → kernel checks user message → "delete" found → allowed
- Agent decides on its own to clean up files → calls delete_file → kernel checks user message (about something else) → no delete signal → blocked
- No covenant rule lookup needed. No fresh-space gap. No TTL. The principle is always enforced.

-----

## Component 4: Compaction Integration

### Manifest in Living State

The compaction model sees file create/delete/update events in the conversation and maintains the file manifest in the Living State section.

Add to the compaction system prompt's Living State guidance:

```
If this context space has files (created via write_file), include a FILES 
section in the Living State listing each file's name and description. 
When files are created, updated, or deleted in the new messages, update 
the FILES section accordingly. Do not include file contents — only names 
and descriptions.
```

This means the agent in month 3 knows "there's a henderson-sow-draft.md in the Business space" even if the conversation where it was created fell out of the context window.

### Manifest injection at compaction time

When compaction runs, the service reads the current manifest and includes it in the compaction input alongside the new messages:

```python
# In CompactionService.compact(), before building the user_content:

manifest = await self.files.load_manifest(tenant_id, space_id)
if manifest:
    manifest_text = "Current files in this space:\n"
    for name, desc in manifest.items():
        manifest_text += f"  - {name}: {desc}\n"
    # Append to the space definition or new messages section
    space_definition += f"\n\n{manifest_text}"
```

The compaction model reads this and reflects it in the Living State. No file contents are ever sent to compaction — only names and descriptions.

-----

## Component 5: User File Uploads

When the user sends a text file attachment via Discord (or future adapters), it lands in the active space's file directory.

### Upload handling

```python
# In the Discord adapter or handler, when an attachment is present:

async def _handle_file_upload(
    self, tenant_id: str, active_space_id: str,
    filename: str, content: str,
) -> str:
    """Handle a user-uploaded text file.

    Same storage as agent-created files. Same read_file() interface.
    """
    # Validate text content
    try:
        content.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        return "I can only handle text files right now — images and PDFs are coming soon."

    description = f"Uploaded by user on {_now_iso()[:10]}"
    result = await self.files.write_file(
        tenant_id, active_space_id, filename, content, description,
    )
    return f"File received: {filename}. I can read it with read_file if you need me to look at it."
```

The agent accesses uploaded files via the same `read_file()` tool — identical interface regardless of origin. The description defaults to "Uploaded by user on [date]" — the agent can update it later with a `write_file` call that overwrites with a better description.

### Platform adapter integration

The Discord adapter already receives attachment metadata. The handler needs to:
1. Check if the message has attachments
2. Download text-compatible attachments (`.txt`, `.md`, `.py`, `.json`, `.csv`, `.yaml`, `.toml`, `.html`, `.css`, `.js`, etc.)
3. Pass content to `_handle_file_upload()`
4. Include the upload notification in the message context so the agent knows a file arrived

Binary attachments (`.png`, `.jpg`, `.pdf`, etc.) get a polite rejection: "I can only handle text files right now."

-----

## Component 6: System Prompt Addition

Add file tool awareness to the operating principles:

```
You have file tools for creating and managing persistent artifacts in each 
context space. Use write_file to create drafts, notes, configs, or any document 
that should persist. Use read_file to access existing files. Use list_files to 
see what's available. Files persist across sessions — you can always come back 
to them.
```

-----

## Implementation Notes

**`_now_iso()` in FileService:** Define locally in `files.py` as `datetime.now(timezone.utc).isoformat()`. Do not import from `engine.py`. (Kit)

**Manifest race condition:** `_load_manifest` then `_update_manifest` are two separate disk operations with no locking. Acceptable for single-user in 3A. Flag for 3B when concurrent operations become possible. Track in Audit & Cleanup as Watching. (Kit)

-----

## Implementation Order

1. **ContextSpace model** — add `max_file_size_bytes` and `max_space_bytes` fields (default None)
2. **FileService** — write_file, read_file, list_files, delete_file, manifest CRUD, filename validation
3. **Tool definitions** — FILE_TOOLS constant with all four schemas
4. **ReasoningService routing** — KERNEL_TOOLS set expanded, file tool dispatch, delete covenant check
5. **Compaction integration** — manifest injection into compaction input, Living State guidance addition
6. **User upload handling** — Discord adapter attachment processing, text validation, rejection for binary
7. **System prompt** — file tool awareness in operating principles
8. **CLI** — `kernos-cli files <tenant_id> <space_id>` showing manifest + file sizes
9. **Tests** — FileService CRUD, filename validation, soft delete + recovery path, manifest consistency, covenant enforcement on delete, kernel tool routing, compaction manifest injection, upload handling
10. **Live test**

-----

## What Claude Code MUST NOT Change

- Compaction system (2C) — only the prompt guidance text and manifest injection are added
- Retrieval system (2D) — remember() stays separate from file tools
- Router logic (2B-v2)
- Entity resolution (2A)
- Soul data model
- Existing kernel tool routing for remember() — only extend the KERNEL_TOOLS set

-----

## Acceptance Criteria

1. **write_file creates a file.** Agent calls write_file("draft.md", content, "First draft"). File exists on disk at the correct path. Manifest updated. Verified.

2. **read_file returns content.** Agent calls read_file("draft.md"). Returns the exact content that was written. Verified.

3. **list_files shows manifest.** Agent calls list_files(). Returns all files with descriptions and sizes. Verified.

4. **delete_file soft deletes.** Agent calls delete_file("draft.md"). File moves to .deleted/ with timestamp. Removed from manifest. list_files no longer shows it. Original file preserved. Verified.

5. **delete_file principle enforcement.** Agent calls delete_file without the user requesting deletion → kernel blocks it. User says "delete old-draft.md" → agent calls delete_file → kernel checks user message → allowed. Verified by sending a message about something else while the agent tries to self-initiate deletion.

6. **write_file description required.** If description is omitted, the tool call fails at the schema validation level. Verified.

7. **Filename validation blocks traversal.** write_file("../../etc/passwd", ...) returns an error. write_file("../secrets.txt", ...) returns an error. write_file(".hidden", ...) returns an error. Verified.

8. **Binary content rejected.** Attempting to write non-UTF-8 content returns "text files only" error. Verified.

9. **Compaction knows about files.** After write_file, trigger compaction. The Living State includes a FILES section with the filename and description. Verified by inspecting the compaction document.

10. **Files survive compaction.** After multiple compaction cycles, list_files still shows all files. File contents unchanged on disk. Verified.

11. **User upload works.** User sends a .txt attachment via Discord. File lands in active space directory. Agent can read_file() it. Manifest updated. Verified.

12. **Cross-space isolation.** Files created in the D&D space are not visible via list_files in the Business space. Verified.

13. **Overwrite works.** write_file with an existing filename overwrites content and updates description. Verified.

14. **No files = clean state.** list_files in a space with no files returns "No files in this space yet." No errors, no empty manifests. Verified.

15. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send: "Create a file with my D&D campaign notes so far" | Agent calls write_file with campaign summary. File created in D&D space. |
| 2 | Send: "What files do I have?" | Agent calls list_files. Shows the campaign notes file with description. |
| 3 | Send: "Read the campaign notes" | Agent calls read_file. Returns the content it wrote. |
| 4 | Send: "Update the campaign notes with what happened in our last session" | Agent calls write_file (overwrite). Content updated. Description may update. |
| 5 | Send: "Delete the campaign notes" | Covenant rule blocks delete. Agent reports it can't delete without owner confirmation. File untouched. |
| 6 | Switch to Business space. Send: "What files do I have?" | list_files returns "No files" — D&D files not visible here. |
| 7 | Send: "Draft an NDA template for Henderson" | Agent calls write_file in Business space. File created. |
| 8 | Switch back to D&D. Send: "What files do I have?" | Only shows campaign notes. Henderson NDA not visible. |
| 9 | Trigger compaction in D&D space. Inspect Living State. | FILES section present with campaign-notes file listed. |
| 10 | `kernos-cli files <tenant_id> <space_id>` | Shows manifest, file sizes, .deleted directory status. |

Write results to `tests/live/LIVE-TEST-3A.md`.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Files are artifacts, remember() is for facts | Clean separation | Different persistence needs. Files are whole documents. Knowledge entries are atomic facts. Blurring creates retrieval confusion. |
| Four kernel-managed tools | Not MCP tools | File operations are kernel infrastructure. Same routing pattern as remember(). The kernel owns the filesystem. |
| Enforcement at kernel layer | Principle-based, not covenant-based | delete_file checks user message for delete intent. The agent cannot self-initiate deletion. Hardcoded principle — no covenant rule gap on fresh spaces. (Kit review fix) |
| description required via schema | Provider validation catches omission | No special handling needed in our code. Same pattern as remember(query). (Kit) |
| Soft delete to .deleted/ | File moves, manifest entry removed | Preserved for recovery but invisible to the agent. Compaction sees "deleted" and removes from Living State. No flags, no ghost entries. (Kit) |
| Manifest in Living State | Not auto-injected into every message | Agent knows what files exist via compaction awareness. Reads specific files on demand. Contents never auto-injected — that would consume the context window. |
| Text-only in 3A | Binary support is separate scope | Each binary format needs its own processing pipeline (OCR, image description, PDF parsing). Text covers configs, drafts, notes, code — everything the agent creates. (Kit) |
| No hardcoded size limits | Config hooks with None defaults | max_file_size_bytes and max_space_bytes exist on ContextSpace. Enforcement wires when usage tiers matter. No overhead now. (Kit) |
| Cross-space access blocked | Easy to lift later, hard to add after | Space isolation is a principle. Cross-space file access is a future feature, not a 3A concern. |
| No versioning | Agent creates v2 explicitly | henderson-draft-v2.md is the version system. Simple, visible, no hidden state. |
| User uploads go to active space | Active space at upload time is the right signal | If you're in Business and send a contract PDF, it belongs in Business. Don't over-think routing. (Kit) |
| Lazy directory creation | No empty directories at space creation | Directories appear when needed. Clean filesystem state. |
