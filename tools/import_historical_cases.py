import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.database import close_pool, execute
from app.routers.runbooks import _build_runbook_draft_from_incident


VALID_UPDATE_TYPES = {"status", "finding", "action", "escalation", "resolution"}
VALID_INCIDENT_STATUSES = {"open", "investigating", "resolved", "closed"}
VALID_EMPLOYEES = {"aria", "nexus", "cipher", "vega"}


def _read_rows(path: Path, fmt: str) -> list[dict]:
    mode = fmt
    if mode == "auto":
        mode = "json" if path.suffix.lower() == ".json" else "csv"

    if mode == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("JSON input must be an array of incident objects")
        return [row for row in data if isinstance(row, dict)]

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def _parse_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_updates(value) -> list[dict]:
    if not value:
        return []
    if isinstance(value, list):
        raw_updates = value
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            raw_updates = json.loads(text)
        except Exception:
            return [{"update_text": text, "update_type": "finding"}]

    updates = []
    for item in raw_updates:
        if isinstance(item, str):
            updates.append({"update_text": item, "update_type": "finding"})
            continue
        if not isinstance(item, dict):
            continue
        update_type = str(item.get("update_type") or "finding").strip().lower()
        if update_type not in VALID_UPDATE_TYPES:
            update_type = "finding"
        updates.append(
            {
                "employee_id": str(item.get("employee_id") or "").strip() or None,
                "update_text": str(item.get("update_text") or item.get("text") or "").strip(),
                "update_type": update_type,
                "created_at": str(item.get("created_at") or "").strip() or None,
            }
        )
    return [update for update in updates if update["update_text"]]


def _normalize_incident(row: dict) -> dict:
    status = str(row.get("status") or "resolved").strip().lower()
    if status not in VALID_INCIDENT_STATUSES:
        status = "resolved"

    owner_id = str(row.get("owner_id") or "aria").strip().lower()
    if owner_id not in VALID_EMPLOYEES:
        owner_id = "aria"

    severity = _parse_int(row.get("severity"), 3)
    if severity < 1 or severity > 5:
        severity = 3

    return {
        "title": str(row.get("title") or "").strip()[:300],
        "description": str(row.get("description") or "").strip() or None,
        "owner_id": owner_id,
        "severity": severity,
        "status": status,
        "host": str(row.get("host") or "").strip() or None,
        "zabbix_event_id": str(row.get("zabbix_event_id") or "").strip() or None,
        "created_by": str(row.get("created_by") or "historical_import").strip()[:50] or "historical_import",
        "started_at": str(row.get("started_at") or "").strip() or None,
        "resolved_at": str(row.get("resolved_at") or "").strip() or None,
        "rca": str(row.get("rca") or "").strip() or None,
        "source": str(row.get("source") or "historical_import").strip()[:50] or "historical_import",
        "updates": _parse_updates(row.get("updates")),
    }


async def _insert_incident(case: dict) -> int:
    return await execute(
        "INSERT INTO incidents "
        "(title, description, owner_id, severity, status, host, zabbix_event_id, created_by, started_at, resolved_at, rca, source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            case["title"],
            case["description"],
            case["owner_id"],
            case["severity"],
            case["status"],
            case["host"],
            case["zabbix_event_id"],
            case["created_by"],
            case["started_at"],
            case["resolved_at"],
            case["rca"],
            case["source"],
        ),
    )


async def _insert_updates(incident_id: int, updates: list[dict]) -> None:
    for update in updates:
        if update.get("created_at"):
            await execute(
                "INSERT INTO incident_updates (incident_id, employee_id, update_text, update_type, created_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    incident_id,
                    update.get("employee_id"),
                    update["update_text"],
                    update["update_type"],
                    update["created_at"],
                ),
            )
        else:
            await execute(
                "INSERT INTO incident_updates (incident_id, employee_id, update_text, update_type) "
                "VALUES (%s,%s,%s,%s)",
                (
                    incident_id,
                    update.get("employee_id"),
                    update["update_text"],
                    update["update_type"],
                ),
            )


async def _create_runbook_from_case(case: dict, incident_id: int) -> int:
    incident = dict(case)
    incident["id"] = incident_id
    draft = _build_runbook_draft_from_incident(incident, case["updates"])
    rb_id = await execute(
        "INSERT INTO runbooks "
        "(title, author_id, source_incident_id, trigger_desc, trigger_keywords, symptoms, diagnosis, "
        "resolution, prevention, rollback, estimated_mttr, related_hosts, status, validation_status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft','candidate')",
        (
            draft["title"],
            case.get("owner_id"),
            incident_id,
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
    await execute("UPDATE incidents SET runbook_id=%s WHERE id=%s", (rb_id, incident_id))
    return rb_id


async def _import_cases(path: Path, fmt: str, create_runbooks: bool, dry_run: bool) -> tuple[int, int]:
    imported = 0
    runbooks = 0
    rows = _read_rows(path, fmt)
    for raw in rows:
        case = _normalize_incident(raw)
        if not case["title"]:
            continue

        if dry_run:
            imported += 1
            if create_runbooks and case["status"] in {"resolved", "closed"}:
                runbooks += 1
            continue

        incident_id = await _insert_incident(case)
        await _insert_updates(incident_id, case["updates"])
        imported += 1
        print(f"Imported INC-{incident_id:04d}: {case['title']}")

        if create_runbooks and case["status"] in {"resolved", "closed"}:
            rb_id = await _create_runbook_from_case(case, incident_id)
            runbooks += 1
            print(f"  Linked runbook {rb_id} (candidate)")

    await close_pool()
    return imported, runbooks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import historical incidents from CSV/JSON and optionally create candidate runbooks."
    )
    parser.add_argument("--file", required=True, help="Path to the CSV or JSON file to import.")
    parser.add_argument("--format", choices=["auto", "csv", "json"], default="auto", help="Input format.")
    parser.add_argument(
        "--create-runbooks",
        action="store_true",
        help="Automatically create candidate runbook drafts for resolved/closed incidents.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and count rows without writing to the database.")
    args = parser.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")

    imported, runbooks = asyncio.run(
        _import_cases(path, args.format, args.create_runbooks, args.dry_run)
    )
    mode = "Validated" if args.dry_run else "Imported"
    print(f"{mode} {imported} incident(s).")
    if args.create_runbooks:
        print(f"Created {runbooks} candidate runbook(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
