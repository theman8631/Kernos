"""Migrate a tenant's JSON state to SQLite.

Usage:
    python -m kernos.tools.migrate_to_sqlite --tenant discord:364303223047323649
    python -m kernos.tools.migrate_to_sqlite --tenant discord:364303223047323649 --dry-run
"""
import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from kernos.utils import _safe_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def migrate_tenant(data_dir: str, tenant_id: str, dry_run: bool = False) -> dict:
    """Migrate a single tenant from JSON to SQLite.

    Returns a report dict with per-table counts.
    """
    from kernos.kernel.state_json import JsonStateStore
    from kernos.kernel.state_sqlite import SqliteStateStore

    json_store = JsonStateStore(data_dir)
    report: dict[str, dict] = {}

    tenant_dir = Path(data_dir) / _safe_name(tenant_id)
    if not tenant_dir.exists():
        logger.error("Tenant directory not found: %s", tenant_dir)
        return {"error": "tenant not found"}

    # Backup JSON files
    if not dry_run:
        backup_dir = tenant_dir / "json_backup"
        if not backup_dir.exists():
            state_dir = tenant_dir / "state"
            awareness_dir = tenant_dir / "awareness"
            if state_dir.exists():
                shutil.copytree(state_dir, backup_dir / "state")
                logger.info("Backed up state/ → json_backup/state/")
            if awareness_dir.exists():
                shutil.copytree(awareness_dir, backup_dir / "awareness")
                logger.info("Backed up awareness/ → json_backup/awareness/")

    sqlite_store = SqliteStateStore(data_dir)

    try:
        # --- Soul ---
        soul = await json_store.get_soul(tenant_id)
        if soul:
            if not dry_run:
                await sqlite_store.save_soul(soul)
            report["soul"] = {"json": 1, "sqlite": 1}
            logger.info("Soul: migrated")
        else:
            report["soul"] = {"json": 0, "sqlite": 0}

        # --- Tenant Profile ---
        profile = await json_store.get_tenant_profile(tenant_id)
        if profile:
            if not dry_run:
                await sqlite_store.save_tenant_profile(tenant_id, profile)
            report["tenant_profile"] = {"json": 1, "sqlite": 1}
            logger.info("Tenant profile: migrated")
        else:
            report["tenant_profile"] = {"json": 0, "sqlite": 0}

        # --- Knowledge ---
        all_ke = await json_store.query_knowledge(tenant_id, active_only=False, limit=10000)
        if not dry_run:
            for e in all_ke:
                await sqlite_store.add_knowledge(e)
        report["knowledge"] = {"json": len(all_ke), "sqlite": len(all_ke)}
        logger.info("Knowledge: %d entries", len(all_ke))

        # --- Covenants ---
        all_cov = await json_store.get_contract_rules(tenant_id, active_only=False)
        if not dry_run:
            for r in all_cov:
                await sqlite_store.add_contract_rule(r)
        report["covenants"] = {"json": len(all_cov), "sqlite": len(all_cov)}
        logger.info("Covenants: %d rules", len(all_cov))

        # --- Context Spaces ---
        all_spaces = await json_store.list_context_spaces(tenant_id)
        if not dry_run:
            for s in all_spaces:
                await sqlite_store.save_context_space(s)
        report["context_spaces"] = {"json": len(all_spaces), "sqlite": len(all_spaces)}
        logger.info("Context spaces: %d spaces", len(all_spaces))

        # --- Preferences ---
        all_prefs = await json_store.query_preferences(tenant_id, active_only=False)
        if not dry_run:
            for p in all_prefs:
                await sqlite_store.save_preference(p)
        report["preferences"] = {"json": len(all_prefs), "sqlite": len(all_prefs)}
        logger.info("Preferences: %d prefs", len(all_prefs))

        # --- Entities ---
        all_entities = await json_store.query_entity_nodes(tenant_id, active_only=False)
        if not dry_run:
            for e in all_entities:
                await sqlite_store.save_entity_node(e)
        report["entities"] = {"json": len(all_entities), "sqlite": len(all_entities)}
        logger.info("Entities: %d nodes", len(all_entities))

        # --- Whispers ---
        whispers_path = Path(data_dir) / _safe_name(tenant_id) / "awareness" / "whispers.json"
        whisper_count = 0
        if whispers_path.exists():
            try:
                raw = json.loads(whispers_path.read_text(encoding="utf-8"))
                from kernos.kernel.awareness import Whisper
                for d in raw:
                    if not dry_run:
                        w = Whisper(**{k: v for k, v in d.items()
                                      if k in {f.name for f in __import__('dataclasses').fields(Whisper)}})
                        await sqlite_store.save_whisper(tenant_id, w)
                    whisper_count += 1
            except Exception as exc:
                logger.warning("Whisper migration: %s", exc)
        report["whispers"] = {"json": whisper_count, "sqlite": whisper_count}
        logger.info("Whispers: %d", whisper_count)

        # --- Triggers ---
        triggers_path = Path(data_dir) / _safe_name(tenant_id) / "state" / "triggers.json"
        trigger_count = 0
        if triggers_path.exists():
            try:
                raw = json.loads(triggers_path.read_text(encoding="utf-8"))
                trigger_count = len(raw)
                if not dry_run:
                    from kernos.kernel.scheduler import Trigger
                    db = await sqlite_store._db(tenant_id)
                    for d in raw:
                        t_data = json.dumps(d, ensure_ascii=False)
                        await db.execute(
                            "INSERT OR REPLACE INTO triggers "
                            "(id, tenant_id, action_description, action_type, status, "
                            "recurrence, next_fire_at, source, data, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (d.get("trigger_id", ""), d.get("tenant_id", tenant_id),
                             d.get("action_description", ""), d.get("action_type", "notify"),
                             d.get("status", "active"), d.get("recurrence", ""),
                             d.get("next_fire_at", ""), d.get("source", ""),
                             t_data, d.get("created_at", ""), d.get("created_at", "")),
                        )
                    await db.commit()
            except Exception as exc:
                logger.warning("Trigger migration: %s", exc)
        report["triggers"] = {"json": trigger_count, "sqlite": trigger_count}
        logger.info("Triggers: %d", trigger_count)

    finally:
        await sqlite_store.close_all()

    # Summary
    logger.info("")
    logger.info("=== Migration Report ===")
    logger.info("Tenant: %s", tenant_id)
    logger.info("Mode: %s", "DRY RUN" if dry_run else "LIVE")
    for table, counts in report.items():
        status = "✓" if counts["json"] == counts["sqlite"] else "✗"
        logger.info("  %s %s: json=%d sqlite=%d", status, table, counts["json"], counts["sqlite"])

    return report


def main():
    parser = argparse.ArgumentParser(description="Migrate tenant JSON state to SQLite")
    parser.add_argument("--tenant", required=True, help="Tenant ID (e.g., discord:364303223047323649)")
    parser.add_argument("--data-dir", default="./data", help="Data directory")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    args = parser.parse_args()

    asyncio.run(migrate_tenant(args.data_dir, args.tenant, args.dry_run))


if __name__ == "__main__":
    main()
