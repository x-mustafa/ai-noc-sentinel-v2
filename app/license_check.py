"""
NOC Sentinel — License enforcement module.

How it works:
  1. At startup, this module computes a machine fingerprint (MAC + hostname + platform).
  2. It reads `license.lic` from the project root.
  3. It verifies the HMAC-SHA256 signature inside the file against the fingerprint.
  4. If valid → startup continues.  If invalid / missing → SystemExit.

License file format (one line):
  <fingerprint_hex>:<hmac_hex>

The HMAC is produced with a secret key that never leaves the vendor's machine.
Clients receive only the pre-computed license.lic file.
"""

import hashlib
import hmac
import os
import platform
import uuid

# Path to the license file, relative to the project root (cwd at runtime)
_LICENSE_FILE = os.path.join(os.path.dirname(__file__), "..", "license.lic")

# Environment variable that disables enforcement (for development / CI).
# Set NOC_SKIP_LICENSE=1 in .env or the shell to bypass.
_SKIP_ENV = "NOC_SKIP_LICENSE"


def get_machine_fingerprint() -> str:
    """
    Build a stable, unique fingerprint for the current machine.
    Combines MAC address, hostname, and OS platform string.
    Returns a 32-character hex digest (truncated SHA-256).
    """
    mac  = hex(uuid.getnode())                  # 48-bit MAC as hex string
    host = platform.node()                       # hostname
    sys_info = platform.platform()              # OS + architecture
    raw  = f"{mac}|{host}|{sys_info}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def verify_license(fingerprint: str, token: str, secret_key: bytes) -> bool:
    """
    Verify that `token` is a valid HMAC-SHA256 of `fingerprint`
    signed with `secret_key`.
    Uses constant-time comparison to prevent timing attacks.
    """
    expected = hmac.new(secret_key, fingerprint.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


def check_license() -> None:
    """
    Called at FastAPI startup.  Raises SystemExit if the license is invalid.
    Set NOC_SKIP_LICENSE=1 to bypass (development / CI only).
    """
    if os.environ.get(_SKIP_ENV) == "1":
        return  # developer / CI bypass

    lic_path = os.path.normpath(_LICENSE_FILE)
    if not os.path.isfile(lic_path):
        raise SystemExit(
            "❌  License file not found (license.lic).\n"
            "    Contact support to obtain a license for this machine."
        )

    try:
        content = open(lic_path).read().strip()
        parts   = content.split(":")
        if len(parts) != 2:
            raise ValueError("Malformed license file")
        stored_fp, stored_token = parts
    except Exception as e:
        raise SystemExit(f"❌  License file is corrupt: {e}")

    current_fp = get_machine_fingerprint()

    if stored_fp != current_fp:
        raise SystemExit(
            "❌  License is not valid for this machine.\n"
            f"    Licensed fingerprint : {stored_fp}\n"
            f"    This machine         : {current_fp}\n"
            "    Contact support to obtain a new license."
        )

    # Load the signing secret from env (vendor sets this in their deploy tooling)
    secret_b64 = os.environ.get("NOC_LICENSE_SECRET", "")
    if not secret_b64:
        # Fallback: read from a local secret file (never committed to git)
        secret_file = os.path.join(os.path.dirname(__file__), "..", "tools", ".license_secret")
        if os.path.isfile(secret_file):
            secret_b64 = open(secret_file).read().strip()

    if not secret_b64:
        raise SystemExit(
            "❌  License signing secret not configured.\n"
            "    Set NOC_LICENSE_SECRET env var or place the secret in tools/.license_secret"
        )

    secret_bytes = secret_b64.encode()

    if not verify_license(current_fp, stored_token, secret_bytes):
        raise SystemExit(
            "❌  License signature is invalid.\n"
            "    This license file may have been tampered with or is for a different build.\n"
            "    Contact support."
        )
