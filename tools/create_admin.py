import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.config import settings
from app.database import close_pool, execute, fetch_one
from app.utils.password import hash_password


async def _run(username: str, password: str, display_name: str, email: str) -> int:
    try:
        existing = await fetch_one("SELECT id FROM users WHERE username=%s LIMIT 1", (username,))
        password_hash = hash_password(password)

        if existing:
            await execute(
                "UPDATE users SET password_hash=%s, role='admin', display_name=%s, email=%s WHERE id=%s",
                (password_hash, display_name or None, email or None, existing["id"]),
            )
            print(f"Updated existing admin user: {username}")
        else:
            user_id = await execute(
                "INSERT INTO users (username, password_hash, role, display_name, email) VALUES (%s,%s,'admin',%s,%s)",
                (username, password_hash, display_name or None, email or None),
            )
            print(f"Created admin user {username} (id={user_id})")
        return 0
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset a local admin user.")
    parser.add_argument("--username", required=True, help="Local admin username")
    parser.add_argument("--password", required=True, help="Local admin password")
    parser.add_argument("--display-name", default="", help="Optional display name")
    parser.add_argument("--email", default="", help="Optional email")
    args = parser.parse_args()

    if len(args.password) < settings.password_min_length:
        parser.error(f"--password must be at least {settings.password_min_length} characters long")

    return asyncio.run(_run(args.username, args.password, args.display_name, args.email))


if __name__ == "__main__":
    raise SystemExit(main())
