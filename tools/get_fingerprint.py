#!/usr/bin/env python3
"""
NOC Sentinel — Machine Fingerprint Helper  (ship this to clients)

The client runs this script and sends the output to you.
You then use generate_license.py to produce their license.lic.

Usage:
    python get_fingerprint.py
"""
import hashlib, platform, uuid

mac      = hex(uuid.getnode())
host     = platform.node()
sys_info = platform.platform()
raw      = f"{mac}|{host}|{sys_info}"
fp       = hashlib.sha256(raw.encode()).hexdigest()[:32]

print(f"NOC Sentinel — Machine Fingerprint")
print(f"===================================")
print(f"Fingerprint : {fp}")
print()
print(f"Send this fingerprint to your NOC Sentinel vendor")
print(f"to receive your license.lic file.")
