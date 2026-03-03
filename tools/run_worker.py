import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("EMBEDDED_SCHEDULER_ENABLED", "false")

from app.database import close_pool, execute, get_pending_migrations
from app.license_check import check_license
from app.services.workflow_engine import start_engine, stop_engine


async def _write_worker_heartbeat(status: str, details: str) -> None:
    await execute(
        "INSERT INTO service_heartbeats (service_name, status, details, updated_at) "
        "VALUES (%s, %s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE status=VALUES(status), details=VALUES(details), updated_at=NOW()",
        ("workflow_worker", status, details),
    )


async def _run_worker(skip_license: bool) -> int:
    if skip_license:
        os.environ["NOC_SKIP_LICENSE"] = "1"

    check_license()
    pending = await get_pending_migrations()
    if pending:
        pending_ids = ", ".join(item["migration_id"] for item in pending)
        print(
            "Pending database migrations detected. "
            f"Run `python tools/migrate.py` before starting the worker. Pending: {pending_ids}"
        )
        await close_pool()
        return 1

    started = await start_engine()
    if not started:
        print("Workflow engine could not start. Verify APScheduler is installed.")
        await close_pool()
        return 1

    worker_details = f"{socket.gethostname()}:{os.getpid()}"
    print("Workflow engine worker started. Press Ctrl+C to stop.")
    try:
        while True:
            await _write_worker_heartbeat("ok", worker_details)
            await asyncio.sleep(60)
    finally:
        try:
            await _write_worker_heartbeat("stopped", worker_details)
        except Exception:
            pass
        await stop_engine()
        await close_pool()
        print("Workflow engine worker stopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the NOC Sentinel workflow worker.")
    parser.add_argument(
        "--skip-license",
        action="store_true",
        help="Set NOC_SKIP_LICENSE=1 for local testing.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run_worker(args.skip_license))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
