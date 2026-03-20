# Per-Space File System

Every context space has its own file storage. The agent can create, read, list, and delete text files scoped to the active space.

## Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| write_file | soft_write | Create or update a text file. Input: name, content, description |
| read_file | read | Read file contents. Input: name |
| list_files | read | List all files with sizes and descriptions |
| delete_file | soft_write | Soft-delete a file (moves to shadow archive). Input: name |

## How It Works

- Files are stored per-space at `data/{tenant}/{spaces}/{space_id}/files/`
- Each space has a `.manifest.json` tracking file metadata (descriptions, sizes)
- File names must be alphanumeric with hyphens, underscores, and dots (no path traversal, no leading dots)
- Content must be valid UTF-8 text
- The file manifest is injected into compaction's Living State, so the agent knows what files exist even after compaction

## Shadow Archive

Files are never permanently deleted. `delete_file` moves the file to `.deleted/` within the space directory and removes it from the manifest. This is consistent with the system-wide rule: no destructive deletions.

## Code Locations

| Component | Path |
|-----------|------|
| FileService, FILE_TOOLS | `kernos/kernel/files.py` |
