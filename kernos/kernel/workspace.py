"""Agentic Workspace — manifest, artifact lifecycle, and tool registration.

Every space can be a workspace. When the agent builds something (a tool,
a script, a project), it's tracked here as an artifact following Kit's
four-layer model: Artifact → Descriptor → Surface → Store.

The workspace_manifest.json in each space's directory is the source of truth.
Descriptors (.tool.json files) are the canonical tool definitions.
The catalog reads from descriptors. One source, no divergence.
"""
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """A workspace-built capability following the four-layer model."""

    id: str                        # "artifact_{uuid8}"
    name: str                      # human-readable, matches catalog entry
    type: str                      # "data_tool" | "script" | "project"
    description: str               # one-line (used in catalog)
    files: dict[str, str]          # layer → filename: artifact, descriptor, implementation, store
    catalog_entry: str = ""        # tool name in ToolCatalog (empty = not registered)
    created_at: str = ""
    last_modified: str = ""
    version: int = 1
    status: str = "active"         # "active" | "archived"
    home_space: str = ""           # space where this artifact's data lives
    stateful: bool = True          # whether the tool needs its home space for execution


@dataclass
class WorkspaceManifest:
    """Per-space manifest tracking all built artifacts."""

    version: int = 1
    tenant_id: str = ""
    space_id: str = ""
    artifacts: list[Artifact] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

MANAGE_WORKSPACE_TOOL = {
    "name": "manage_workspace",
    "description": (
        "Manage workspace artifacts. List what's been built in this space, "
        "add new artifacts to the manifest after building them with execute_code, "
        "update versions after modifications, or archive artifacts. "
        "Tracks both TOOLS (callable capabilities registered in the catalog) "
        "and PROJECTS (bodies of work like books, websites, business plans — "
        "structured files that persist across sessions, not registered as tools)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "archive"],
                "description": "Operation to perform",
            },
            "artifact": {
                "type": "object",
                "description": "Artifact data (for add/update)",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["data_tool", "script", "project"]},
                    "description": {"type": "string"},
                    "files": {"type": "object"},
                    "catalog_entry": {"type": "string"},
                    "stateful": {"type": "boolean"},
                },
            },
            "artifact_id": {
                "type": "string",
                "description": "Artifact ID (for update/archive)",
            },
        },
        "required": ["action"],
    },
}

REGISTER_TOOL_TOOL = {
    "name": "register_tool",
    "description": (
        "Register a workspace-built tool in the universal catalog. "
        "The tool must have a .tool.json descriptor file in the current "
        "space's directory. After registration, the tool is callable "
        "from any space via intent-based surfacing. "
        "The descriptor defines name, description, input_schema, and implementation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "descriptor_file": {
                "type": "string",
                "description": "Filename of the .tool.json descriptor in the current space's directory.",
            },
        },
        "required": ["descriptor_file"],
    },
}


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------

class WorkspaceManager:
    """Manages workspace manifests, artifact lifecycle, and tool registration.

    One instance per handler. Manifests are lazy-loaded on space entry —
    no boot-time scan, no cost for unvisited spaces.
    """

    def __init__(self, data_dir: str, catalog: Any = None) -> None:
        self._data_dir = Path(data_dir)
        self._catalog = catalog  # ToolCatalog reference
        self._loaded_manifests: dict[str, WorkspaceManifest] = {}  # "tenant:space" → manifest

    def set_catalog(self, catalog: Any) -> None:
        """Set the ToolCatalog reference (called after construction)."""
        self._catalog = catalog

    # --- Path helpers ---

    def _space_dir(self, tenant_id: str, space_id: str) -> Path:
        return self._data_dir / _safe_name(tenant_id) / "spaces" / space_id / "files"

    def _manifest_path(self, tenant_id: str, space_id: str) -> Path:
        return self._space_dir(tenant_id, space_id) / "workspace_manifest.json"

    # --- Manifest I/O ---

    async def load_manifest(self, tenant_id: str, space_id: str) -> WorkspaceManifest:
        """Load or create a workspace manifest. Caches in memory."""
        key = f"{tenant_id}:{space_id}"
        if key in self._loaded_manifests:
            return self._loaded_manifests[key]

        path = self._manifest_path(tenant_id, space_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                artifacts = [
                    Artifact(**{k: v for k, v in a.items() if k in Artifact.__dataclass_fields__})
                    for a in raw.get("artifacts", [])
                ]
                manifest = WorkspaceManifest(
                    version=raw.get("version", 1),
                    tenant_id=tenant_id,
                    space_id=space_id,
                    artifacts=artifacts,
                )
                logger.info("WORKSPACE_MANIFEST: space=%s loaded artifacts=%d active=%d archived=%d",
                    space_id, len(artifacts),
                    sum(1 for a in artifacts if a.status == "active"),
                    sum(1 for a in artifacts if a.status == "archived"))
            except Exception as exc:
                logger.warning("WORKSPACE_MANIFEST: corrupt manifest in %s: %s", space_id, exc)
                manifest = WorkspaceManifest(tenant_id=tenant_id, space_id=space_id)
        else:
            manifest = WorkspaceManifest(tenant_id=tenant_id, space_id=space_id)

        self._loaded_manifests[key] = manifest
        return manifest

    async def save_manifest(self, tenant_id: str, space_id: str, manifest: WorkspaceManifest) -> None:
        """Persist manifest to disk."""
        path = self._manifest_path(tenant_id, space_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": manifest.version,
            "tenant_id": manifest.tenant_id,
            "space_id": manifest.space_id,
            "artifacts": [asdict(a) for a in manifest.artifacts],
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Artifact CRUD ---

    async def list_artifacts(self, tenant_id: str, space_id: str) -> str:
        """List all active artifacts in the workspace. Returns formatted text."""
        manifest = await self.load_manifest(tenant_id, space_id)
        active = [a for a in manifest.artifacts if a.status == "active"]
        if not active:
            return "No artifacts built in this space yet. Use execute_code to build something."

        tools = [a for a in active if a.type in ("data_tool", "script")]
        projects = [a for a in active if a.type == "project"]

        lines = [f"**Workspace** ({len(active)} artifacts)\n"]
        if tools:
            lines.append("**Tools:**")
            for a in tools:
                registered = f" [catalog: {a.catalog_entry}]" if a.catalog_entry else " [not yet registered]"
                lines.append(
                    f"- **{a.name}** ({a.type}, v{a.version}){registered}\n"
                    f"  {a.description}\n"
                    f"  Files: {', '.join(f'{k}={v}' for k, v in a.files.items() if v)}"
                )
        if projects:
            lines.append("\n**Projects:**")
            for a in projects:
                lines.append(
                    f"- **{a.name}** (v{a.version})\n"
                    f"  {a.description}\n"
                    f"  Files: {', '.join(f'{k}={v}' for k, v in a.files.items() if v)}"
                )
        return "\n".join(lines)

    async def add_artifact(
        self, tenant_id: str, space_id: str, artifact_data: dict,
    ) -> tuple[str, Artifact]:
        """Add a new artifact to the manifest. Returns (message, artifact)."""
        manifest = await self.load_manifest(tenant_id, space_id)
        now = utc_now()

        artifact = Artifact(
            id=f"artifact_{uuid.uuid4().hex[:8]}",
            name=artifact_data.get("name", "untitled"),
            type=artifact_data.get("type", "script"),
            description=artifact_data.get("description", ""),
            files=artifact_data.get("files", {}),
            catalog_entry=artifact_data.get("catalog_entry", ""),
            created_at=now,
            last_modified=now,
            version=1,
            status="active",
            home_space=space_id,
            stateful=artifact_data.get("stateful", True),
        )

        manifest.artifacts.append(artifact)
        await self.save_manifest(tenant_id, space_id, manifest)

        logger.info("WORKSPACE_ADD: space=%s artifact=%s type=%s version=%d",
            space_id, artifact.name, artifact.type, artifact.version)

        return f"Added artifact '{artifact.name}' ({artifact.id}) to workspace.", artifact

    async def update_artifact(
        self, tenant_id: str, space_id: str, artifact_id: str, updates: dict,
    ) -> str:
        """Update an existing artifact. Increments version."""
        manifest = await self.load_manifest(tenant_id, space_id)
        target = next((a for a in manifest.artifacts if a.id == artifact_id), None)
        if not target:
            return f"Artifact '{artifact_id}' not found."
        if target.status != "active":
            return f"Artifact '{artifact_id}' is archived."

        # Apply updates
        for key in ("name", "description", "type", "files", "catalog_entry", "stateful"):
            if key in updates:
                setattr(target, key, updates[key])

        target.version += 1
        target.last_modified = utc_now()
        await self.save_manifest(tenant_id, space_id, manifest)

        logger.info("WORKSPACE_UPDATE: space=%s artifact=%s version=%d",
            space_id, target.name, target.version)
        return f"Updated '{target.name}' to version {target.version}."

    async def archive_artifact(
        self, tenant_id: str, space_id: str, artifact_id: str,
    ) -> str:
        """Archive an artifact. Removes from catalog but preserves files."""
        manifest = await self.load_manifest(tenant_id, space_id)
        target = next((a for a in manifest.artifacts if a.id == artifact_id), None)
        if not target:
            return f"Artifact '{artifact_id}' not found."

        target.status = "archived"
        target.last_modified = utc_now()

        # Remove from catalog if registered
        if target.catalog_entry and self._catalog:
            self._catalog.unregister(target.catalog_entry)

        await self.save_manifest(tenant_id, space_id, manifest)
        logger.info("WORKSPACE_ARCHIVE: space=%s artifact=%s", space_id, target.name)
        return f"Archived '{target.name}'. Files preserved on disk."

    # --- Tool Registration ---

    async def register_tool(
        self, tenant_id: str, space_id: str, descriptor_file: str | dict,
    ) -> str:
        """Validate a descriptor and register the tool in the catalog.

        The descriptor (.tool.json) is the source of truth. The catalog
        reads from it. The manifest tracks it.
        """
        # Guard: LLM may send a dict instead of a string
        if isinstance(descriptor_file, dict):
            descriptor_file = descriptor_file.get("descriptor_file", descriptor_file.get("name", str(descriptor_file)))
        descriptor_file = str(descriptor_file).strip()
        if not descriptor_file:
            return "Error: descriptor_file must be a filename string."

        space_dir = self._space_dir(tenant_id, space_id)

        # 1. Validate descriptor filename (no path traversal)
        if "/" in descriptor_file or "\\" in descriptor_file or ".." in descriptor_file:
            return "Descriptor filename must not contain path separators or '..'."

        # 2. Load descriptor
        desc_path = space_dir / descriptor_file
        if not desc_path.exists():
            return f"Descriptor file '{descriptor_file}' not found in space directory."

        try:
            descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return f"Invalid JSON in descriptor: {exc}"

        # 3. Validate required fields
        required = ["name", "description", "input_schema", "implementation"]
        missing = [f for f in required if f not in descriptor]
        if missing:
            return f"Descriptor missing required fields: {', '.join(missing)}"

        name = descriptor["name"]
        impl = descriptor["implementation"]

        # 4. Validate name (snake_case, no special chars)
        if not name or not re.match(r'^[a-z][a-z0-9_]*$', name):
            return f"Tool name '{name}' must be snake_case (lowercase letters, digits, underscores)."

        # 5. Validate implementation filename (no traversal, must be .py)
        if "/" in impl or "\\" in impl or ".." in impl:
            return "Implementation filename must not contain path separators or '..'."
        if not impl.endswith(".py"):
            return f"Implementation '{impl}' must be a .py file."

        # 6. Check implementation exists and is a file
        impl_path = space_dir / impl
        if not impl_path.is_file():
            return f"Implementation file '{impl}' not found."

        # 7. Check name uniqueness in catalog
        existing = self._catalog.get(name) if self._catalog else None
        if existing and existing.source != "workspace":
            return f"Name '{name}' conflicts with an existing {existing.source} tool."

        # 6. Validate input_schema
        schema = descriptor.get("input_schema", {})
        if not isinstance(schema, dict) or "type" not in schema:
            return "input_schema must be a valid JSON Schema object with a 'type' field."

        # 7. Register in catalog
        if self._catalog:
            self._catalog.register(
                name=name,
                description=descriptor["description"],
                source="workspace",
            )
            # Store workspace metadata on the catalog entry
            entry = self._catalog.get(name)
            if entry:
                entry.home_space = space_id
                entry.implementation = impl
                entry.stateful = descriptor.get("stateful", True)

        logger.info("TOOL_REGISTER: name=%s space=%s source=workspace", name, space_id)

        # 8. Auto-add to manifest if not already tracked
        manifest = await self.load_manifest(tenant_id, space_id)
        existing_artifact = next(
            (a for a in manifest.artifacts if a.catalog_entry == name and a.status == "active"),
            None,
        )
        if not existing_artifact:
            await self.add_artifact(tenant_id, space_id, {
                "name": name,
                "type": descriptor.get("type", "data_tool"),
                "description": descriptor["description"],
                "files": {
                    "descriptor": descriptor_file,
                    "implementation": impl,
                    "store": descriptor.get("store", ""),
                },
                "catalog_entry": name,
                "stateful": descriptor.get("stateful", True),
            })

        return f"Registered tool '{name}'. It's now available across all spaces via the universal catalog."

    # --- Workspace Tool Execution ---

    async def execute_workspace_tool(
        self, tenant_id: str, tool_name: str, tool_input: dict, data_dir: str,
    ) -> str:
        """Execute a workspace-built tool by calling its implementation."""
        if not self._catalog:
            return json.dumps({"error": "Catalog not available"})

        entry = self._catalog.get(tool_name)
        if not entry or entry.source != "workspace":
            return json.dumps({"error": f"Unknown workspace tool: {tool_name}"})

        home_space = getattr(entry, "home_space", "")
        implementation = getattr(entry, "implementation", "")
        if not home_space or not implementation:
            return json.dumps({"error": f"Tool '{tool_name}' missing home_space or implementation"})

        # Validate implementation filename
        if "/" in implementation or "\\" in implementation or ".." in implementation:
            return json.dumps({"error": "Implementation path contains traversal sequences"})
        if not implementation.endswith(".py"):
            return json.dumps({"error": "Implementation must be a .py file"})

        # Write input data to a unique temp file (avoids collision on concurrent calls)
        import tempfile as _tf
        space_dir = self._space_dir(tenant_id, home_space)
        space_dir.mkdir(parents=True, exist_ok=True)
        fd, input_path = _tf.mkstemp(suffix=".json", prefix="_tool_input_", dir=str(space_dir))
        input_filename = os.path.basename(input_path)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tool_input, f)
        except Exception:
            os.close(fd)
            raise

        module_name = implementation.replace(".py", "")
        exec_code = (
            "import json, sys, os\n"
            "sys.path.insert(0, '.')\n"
            f"from {module_name} import execute\n"
            f"with open('{input_filename}') as f:\n"
            "    input_data = json.load(f)\n"
            f"os.unlink('{input_filename}')\n"
            "result = execute(input_data)\n"
            "print(json.dumps(result))\n"
        )

        from kernos.kernel.code_exec import execute_code
        result = await execute_code(
            tenant_id=tenant_id,
            space_id=home_space,
            code=exec_code,
            timeout_seconds=30,
            data_dir=data_dir,
        )

        logger.info("TOOL_DISPATCH: name=%s type=workspace home=%s success=%s",
            tool_name, home_space, result.get("success"))

        if result.get("success"):
            stdout = result.get("stdout", "").strip()
            try:
                return json.dumps(json.loads(stdout))
            except json.JSONDecodeError:
                return json.dumps({"output": stdout}) if stdout else json.dumps({"status": "completed"})
        else:
            error = result.get("stderr", "") or result.get("error", "Execution failed")
            return json.dumps({"error": error[:500]})

    # --- Lazy Registration on Space Entry ---

    async def ensure_registered(self, tenant_id: str, space_id: str) -> None:
        """On space entry, ensure all active artifacts with catalog entries are registered.

        This is the lazy-load mechanism — manifests load and register tools
        on first visit, not at boot. No cost for unvisited spaces.
        """
        manifest = await self.load_manifest(tenant_id, space_id)
        for artifact in manifest.artifacts:
            if artifact.status != "active" or not artifact.catalog_entry:
                continue
            if self._catalog and not self._catalog.get(artifact.catalog_entry):
                # Not yet in catalog — load descriptor and register
                desc_file = artifact.files.get("descriptor", "")
                if desc_file:
                    desc_path = self._space_dir(tenant_id, space_id) / desc_file
                    if desc_path.exists():
                        try:
                            descriptor = json.loads(desc_path.read_text(encoding="utf-8"))
                            self._catalog.register(
                                name=artifact.catalog_entry,
                                description=descriptor.get("description", artifact.description),
                                source="workspace",
                            )
                            entry = self._catalog.get(artifact.catalog_entry)
                            if entry:
                                entry.home_space = artifact.home_space or space_id
                                entry.implementation = descriptor.get("implementation", "")
                                entry.stateful = descriptor.get("stateful", artifact.stateful)
                            logger.info("WORKSPACE_REGISTER: artifact=%s catalog_entry=%s source=workspace",
                                artifact.name, artifact.catalog_entry)
                        except Exception as exc:
                            logger.warning("WORKSPACE_REGISTER: failed for %s: %s", artifact.name, exc)
