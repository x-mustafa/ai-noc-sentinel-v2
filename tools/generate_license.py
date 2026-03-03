#!/usr/bin/env python3
"""
NOC Sentinel — License Generator  (INTERNAL USE ONLY — never ship this file)

Usage:
    python tools/generate_license.py                        # Generate for THIS machine
    python tools/generate_license.py <fingerprint>          # Generate for a remote machine

Requirements:
    - tools/.license_secret must exist (or NOC_LICENSE_SECRET env var must be set)
      Run `python tools/generate_license.py --init-secret` to create it on first use.

Workflow:
    1. Client runs:   python tools/get_fingerprint.py
       → gets their machine fingerprint (32 hex chars)
    2. You run:       python tools/generate_license.py <their_fingerprint>
       → outputs the license.lic content
    3. You send the license.lic file to the client
    4. Client places license.lic in the project root and starts the server
"""

import hashlib
import hmac
import os
import platform
import secrets
import sys
import uuid

_SECRET_FILE = os.path.join(os.path.dirname(__file__), ".license_secret")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_secret() -> bytes:
    """Load signing secret from env or secret file."""
    secret = os.environ.get("NOC_LICENSE_SECRET", "")
    if secret:
        return secret.encode()
    if os.path.isfile(_SECRET_FILE):
        return open(_SECRET_FILE).read().strip().encode()
    print("❌  No signing secret found.")
    print(f"    Run: python {__file__} --init-secret")
    sys.exit(1)


def _get_local_fingerprint() -> str:
    """Compute fingerprint for the current machine."""
    mac      = hex(uuid.getnode())
    host     = platform.node()
    sys_info = platform.platform()
    raw      = f"{mac}|{host}|{sys_info}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _generate_token(fingerprint: str, secret: bytes) -> str:
    """Produce HMAC-SHA256 token for a fingerprint."""
    return hmac.new(secret, fingerprint.encode(), hashlib.sha256).hexdigest()


def _write_license(fingerprint: str, token: str, output_path: str = "license.lic") -> None:
    """Write the license file."""
    content = f"{fingerprint}:{token}\n"
    with open(output_path, "w") as f:
        f.write(content)
    print(f"✅  License written to: {output_path}")
    print(f"    Fingerprint : {fingerprint}")
    print(f"    Token       : {token[:16]}...  (truncated)")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_init_secret():
    """Generate and save a new signing secret."""
    if os.path.isfile(_SECRET_FILE):
        print(f"⚠️   Secret already exists at {_SECRET_FILE}")
        confirm = input("    Overwrite? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("    Aborted.")
            sys.exit(0)

    secret = secrets.token_hex(32)          # 256-bit secret
    with open(_SECRET_FILE, "w") as f:
        f.write(secret)
    os.chmod(_SECRET_FILE, 0o600)           # owner read-only
    print(f"✅  Secret generated and saved to: {_SECRET_FILE}")
    print(f"    Secret (keep safe): {secret}")
    print()
    print("    To use in CI/deploy: export NOC_LICENSE_SECRET=" + secret)


def cmd_generate(fingerprint: str | None = None):
    """Generate a license for a fingerprint (or for this machine)."""
    secret = _load_secret()

    if fingerprint is None:
        fingerprint = _get_local_fingerprint()
        print(f"ℹ️   Using local machine fingerprint: {fingerprint}")
    else:
        fingerprint = fingerprint.strip().lower()
        if len(fingerprint) != 32 or not all(c in "0123456789abcdef" for c in fingerprint):
            print(f"❌  Invalid fingerprint: must be 32 hex characters.")
            sys.exit(1)
        print(f"ℹ️   Generating license for remote fingerprint: {fingerprint}")

    token = _generate_token(fingerprint, secret)
    _write_license(fingerprint, token)


def cmd_verify(lic_path: str = "license.lic"):
    """Verify an existing license file against this machine."""
    secret = _load_secret()
    if not os.path.isfile(lic_path):
        print(f"❌  File not found: {lic_path}")
        sys.exit(1)

    content = open(lic_path).read().strip()
    parts   = content.split(":")
    if len(parts) != 2:
        print("❌  Malformed license file.")
        sys.exit(1)

    stored_fp, stored_token = parts
    current_fp = _get_local_fingerprint()
    expected   = _generate_token(stored_fp, secret)

    print(f"License file      : {lic_path}")
    print(f"Licensed FP       : {stored_fp}")
    print(f"This machine FP   : {current_fp}")
    print(f"FP match          : {'✅' if stored_fp == current_fp else '❌'}")
    sig_ok = hmac.compare_digest(expected, stored_token)
    print(f"Signature valid   : {'✅' if sig_ok else '❌'}")

    if stored_fp == current_fp and sig_ok:
        print("\n✅  License is VALID for this machine.")
    else:
        print("\n❌  License is INVALID.")
        sys.exit(1)


def cmd_fingerprint():
    """Print the fingerprint of this machine (to give to the vendor)."""
    fp = _get_local_fingerprint()
    print(f"Machine fingerprint: {fp}")
    print("Send this to the vendor to receive your license.lic file.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        print("Commands:")
        print("  --init-secret              Create a new signing secret")
        print("  --fingerprint              Print this machine's fingerprint")
        print("  --verify [license.lic]     Verify a license file")
        print("  <fingerprint>              Generate license for a fingerprint")
        print("  (no args)                  Generate license for THIS machine")
        sys.exit(0)

    if args[0] == "--init-secret":
        cmd_init_secret()
    elif args[0] == "--fingerprint":
        cmd_fingerprint()
    elif args[0] == "--verify":
        cmd_verify(args[1] if len(args) > 1 else "license.lic")
    else:
        cmd_generate(args[0])
