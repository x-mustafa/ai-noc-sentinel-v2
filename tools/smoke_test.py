import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("EMBEDDED_SCHEDULER_ENABLED", "false")

from fastapi.testclient import TestClient

from app.database import close_pool, fetch_one, get_pending_migrations
from app.main import app
from app.services.zabbix_client import call_zabbix


def _print_check(name: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")


def run_http_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    checks = [
        ("GET /", 200),
        ("GET /api/health", 200),
        ("GET /api/auth/me", 401),
        ("GET /api/zabbix/status", 401),
    ]
    with TestClient(app) as client:
        for route, expected in checks:
            method, path = route.split(" ", 1)
            resp = client.request(method, path)
            ok = resp.status_code == expected
            results.append((route, ok, f"expected {expected}, got {resp.status_code}"))
    return results


async def run_runtime_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    try:
        users = await fetch_one("SELECT COUNT(*) AS c FROM users")
        results.append(("DB users table", True, f"{users['c']} user(s)"))

        pending = await get_pending_migrations()
        if pending:
            pending_ids = ", ".join(item["migration_id"] for item in pending)
            results.append(("Pending migrations", False, pending_ids))
        else:
            results.append(("Pending migrations", True, "none"))

        zabbix = await call_zabbix("apiinfo.version")
        if isinstance(zabbix, dict) and zabbix.get("_zabbix_error"):
            results.append(("Zabbix connectivity", False, zabbix["_zabbix_error"]))
        else:
            results.append(("Zabbix connectivity", True, f"version {zabbix}"))
    except Exception as exc:
        results.append(("Runtime checks", False, f"{type(exc).__name__}: {exc}"))
    finally:
        await close_pool()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run NOC Sentinel smoke checks.")
    parser.add_argument(
        "--skip-license",
        action="store_true",
        help="Set NOC_SKIP_LICENSE=1 for local testing.",
    )
    args = parser.parse_args()

    if args.skip_license:
        os.environ["NOC_SKIP_LICENSE"] = "1"

    failures = 0

    for name, ok, detail in run_http_checks():
        _print_check(name, ok, detail)
        if not ok:
            failures += 1

    for name, ok, detail in asyncio.run(run_runtime_checks()):
        _print_check(name, ok, detail)
        if not ok:
            failures += 1

    if failures:
        print(f"\nSmoke test failed with {failures} issue(s).")
        return 1

    print("\nSmoke test completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
