import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.database import close_pool, execute, fetch_all
from app.routers.runbooks import _build_runbook_draft_from_incident


async def _backfill(limit: int) -> int:
    incidents = await fetch_all(
        "SELECT * FROM incidents "
        "WHERE status IN ('resolved','closed') AND runbook_id IS NULL "
        "ORDER BY resolved_at DESC LIMIT %s",
        (limit,),
    )
    created = 0
    for incident in incidents:
        updates = await fetch_all(
            "SELECT update_text, update_type FROM incident_updates WHERE incident_id=%s ORDER BY created_at ASC",
            (incident["id"],),
        )
        draft = _build_runbook_draft_from_incident(incident, updates)
        rb_id = await execute(
            "INSERT INTO runbooks "
            "(title, author_id, source_incident_id, trigger_desc, trigger_keywords, symptoms, diagnosis, "
            "resolution, prevention, rollback, estimated_mttr, related_hosts, status, validation_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft','candidate')",
            (
                draft["title"],
                incident.get("owner_id"),
                incident["id"],
                draft["trigger_desc"],
                draft["trigger_keywords"],
                draft["symptoms"],
                draft["diagnosis"],
                draft["resolution"],
                draft["prevention"],
                draft["rollback"],
                None,
                draft["related_hosts"],
            ),
        )
        await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, incident["id"]))
        created += 1
        print(f"Linked INC-{incident['id']:04d} -> runbook {rb_id}")
    await close_pool()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Create draft runbooks from resolved incidents without coverage.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum incidents to backfill.")
    args = parser.parse_args()
    created = asyncio.run(_backfill(args.limit))
    print(f"Created {created} draft runbook(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
