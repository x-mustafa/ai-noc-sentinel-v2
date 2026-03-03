import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.database import close_pool, get_pending_migrations, run_migration


async def _run(list_only: bool) -> int:
    try:
        pending = await get_pending_migrations()
        if list_only:
            if not pending:
                print("No pending migrations.")
                return 0
            print("Pending migrations:")
            for item in pending:
                print(f"- {item['migration_id']}: {item['name']}")
            return 0

        applied = await run_migration()
        if applied:
            print("Applied migrations:")
            for migration_id in applied:
                print(f"- {migration_id}")
        else:
            print("No pending migrations.")
        return 0
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply tracked NOC Sentinel database migrations.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List pending migrations without applying them.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(list_only=args.list))


if __name__ == "__main__":
    raise SystemExit(main())
