"""Per-space file system for KERNOS agents.

Files are persistent text artifacts (drafts, notes, configs, outputs) scoped
to a single context space. The agent creates and manages them via four
kernel-managed tools: write_file, read_file, list_files, delete_file.
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

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
                    "description": "Filename including extension (e.g. 'henderson-sow.md', 'session-notes.txt')",
                },
                "content": {
                    "type": "string",
                    "description": "The full text content of the file",
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence description of what this file is — shown in file listings",
                },
            },
            "required": ["name", "content", "description"],
        },
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
                    "description": "Filename to read",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_files",
        "description": "List all files in the current context space with their descriptions.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
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
                    "description": "Filename to delete",
                }
            },
            "required": ["name"],
        },
    },
]


# ---------------------------------------------------------------------------
# FileService
# ---------------------------------------------------------------------------


class FileService:
    """Manages per-space file operations.

    All operations scoped to a single space. No cross-space access.
    Files are text-only. Binary content is rejected with an error message.
    Soft deletes: files move to .deleted/ — never permanently removed.
    """

    def __init__(self, data_dir: str) -> None:
        from kernos.utils import _safe_name
        self.data_dir = Path(data_dir)
        self._safe_name = _safe_name

    def _space_files_dir(self, tenant_id: str, space_id: str) -> Path:
        return (
            self.data_dir
            / self._safe_name(tenant_id)
            / "spaces"
            / space_id
            / "files"
        )

    def _deleted_dir(self, tenant_id: str, space_id: str) -> Path:
        return self._space_files_dir(tenant_id, space_id) / ".deleted"

    def _manifest_path(self, tenant_id: str, space_id: str) -> Path:
        return self._space_files_dir(tenant_id, space_id) / ".manifest.json"

    async def write_file(
        self,
        tenant_id: str,
        space_id: str,
        name: str,
        content: str,
        description: str,
    ) -> str:
        """Create or overwrite a text file. Returns confirmation message."""
        # Validate text content
        try:
            content.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return "Error: File system currently supports text files only. Binary support (images, PDFs) is coming in a future update."

        # Validate filename
        if not self._valid_filename(name):
            return (
                f"Error: Invalid filename '{name}'. "
                "Use alphanumeric characters, hyphens, underscores, and dots."
            )

        files_dir = self._space_files_dir(tenant_id, space_id)
        files_dir.mkdir(parents=True, exist_ok=True)

        file_path = files_dir / name
        is_update = file_path.exists()
        file_path.write_text(content, encoding="utf-8")

        await self._update_manifest(tenant_id, space_id, name, description)

        action = "Updated" if is_update else "Created"
        logger.info("FILE_WRITE space=%s name=%s size=%d", space_id, name, len(content))
        return f"{action} '{name}' ({len(content)} chars). Description: {description}"

    async def read_file(
        self,
        tenant_id: str,
        space_id: str,
        name: str,
    ) -> str:
        """Read a file's contents. Returns content or error message."""
        if not self._valid_filename(name):
            return f"Error: Invalid filename '{name}'."

        file_path = self._space_files_dir(tenant_id, space_id) / name
        exists = file_path.exists()
        logger.info("FILE_READ space=%s name=%s exists=%s", space_id, name, exists)
        if not exists:
            return f"Error: File '{name}' not found. Use list_files to see available files."
        return file_path.read_text(encoding="utf-8")

    async def list_files(
        self,
        tenant_id: str,
        space_id: str,
    ) -> str:
        """List all files with descriptions. Returns formatted text."""
        manifest = await self._load_manifest(tenant_id, space_id)
        logger.info("FILE_LIST space=%s count=%d", space_id, len(manifest))
        if not manifest:
            return "No files in this space yet."

        lines = []
        for name, desc in sorted(manifest.items()):
            file_path = self._space_files_dir(tenant_id, space_id) / name
            size = file_path.stat().st_size if file_path.exists() else 0
            lines.append(f"  {name} ({size} bytes) — {desc}")

        return f"Files in this space ({len(manifest)}):\n" + "\n".join(lines)

    async def delete_file(
        self,
        tenant_id: str,
        space_id: str,
        name: str,
    ) -> str:
        """Soft delete — move to .deleted/, remove from manifest."""
        if not self._valid_filename(name):
            return f"Error: Invalid filename '{name}'."

        file_path = self._space_files_dir(tenant_id, space_id) / name
        if not file_path.exists():
            logger.info("FILE_DELETE space=%s name=%s exists=False", space_id, name)
            return f"Error: File '{name}' not found."

        logger.info("FILE_DELETE space=%s name=%s exists=True", space_id, name)
        deleted_dir = self._deleted_dir(tenant_id, space_id)
        deleted_dir.mkdir(parents=True, exist_ok=True)

        # Timestamp in filename — replace colons for filesystem compatibility
        ts = _now_iso().replace(":", "-")
        dest = deleted_dir / f"{name}_{ts}"
        file_path.rename(dest)

        await self._remove_from_manifest(tenant_id, space_id, name)

        return f"Deleted '{name}'. File preserved for recovery."

    def _valid_filename(self, name: str) -> bool:
        """Validate filename — no path traversal, reasonable characters."""
        if not name or "/" in name or "\\" in name or ".." in name:
            return False
        if name.startswith("."):
            return False
        return bool(re.match(r'^[\w\-. ]+$', name))

    # --- Manifest CRUD ---

    async def _load_manifest(self, tenant_id: str, space_id: str) -> dict[str, str]:
        """Load the file manifest for a space.

        On JSON parse failure: rebuilds the manifest from actual files on disk
        (descriptions are lost but files are preserved). Logs a WARNING so the
        corruption event is visible in logs.
        """
        manifest_path = self._manifest_path(tenant_id, space_id)
        if not manifest_path.exists():
            return {}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "MANIFEST_CORRUPT: Failed to parse manifest for %s/%s: %s — "
                "rebuilding from disk. Descriptions will be empty.",
                tenant_id, space_id, exc,
            )
            return await self._rebuild_manifest(tenant_id, space_id)

    async def _rebuild_manifest(
        self, tenant_id: str, space_id: str
    ) -> dict[str, str]:
        """Rebuild manifest by scanning the files directory.

        Called when the manifest JSON is corrupt. Descriptions are lost.
        Writes the recovered manifest back to disk atomically.
        """
        files_dir = self._space_files_dir(tenant_id, space_id)
        if not files_dir.exists():
            return {}
        manifest: dict[str, str] = {}
        for p in files_dir.iterdir():
            if p.is_file() and not p.name.startswith("."):
                manifest[p.name] = "(description unavailable — rebuilt after corruption)"
        logger.warning(
            "MANIFEST_REBUILD: recovered %d file(s) for %s/%s",
            len(manifest), tenant_id, space_id,
        )
        manifest_path = self._manifest_path(tenant_id, space_id)
        await self._write_manifest_atomic(manifest_path, manifest)
        return manifest

    def _write_manifest_atomic(self, manifest_path: Path, manifest: dict) -> None:
        """Write manifest JSON atomically: temp file → os.replace.

        Prevents partial writes from corrupting the manifest during
        concurrent writes or process interruptions (e.g. 429 retry delays).
        """
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=manifest_path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(manifest, indent=2))
            os.replace(tmp_path, str(manifest_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def load_manifest(self, tenant_id: str, space_id: str) -> dict[str, str]:
        """Public alias for _load_manifest (used by CompactionService)."""
        return await self._load_manifest(tenant_id, space_id)

    async def _update_manifest(
        self, tenant_id: str, space_id: str, name: str, description: str
    ) -> None:
        manifest = await self._load_manifest(tenant_id, space_id)
        manifest[name] = description
        manifest_path = self._manifest_path(tenant_id, space_id)
        self._write_manifest_atomic(manifest_path, manifest)

    async def _remove_from_manifest(
        self, tenant_id: str, space_id: str, name: str
    ) -> None:
        manifest = await self._load_manifest(tenant_id, space_id)
        manifest.pop(name, None)
        manifest_path = self._manifest_path(tenant_id, space_id)
        self._write_manifest_atomic(manifest_path, manifest)
