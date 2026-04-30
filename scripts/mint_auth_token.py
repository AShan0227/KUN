#!/usr/bin/env python3
"""Mint a KUN HMAC bearer token for staging/dev.

Example:
  KUN_AUTH_SECRET=... scripts/mint_auth_token.py --tenant u-sylvan --scopes world:approve,world:dispatch
"""

from __future__ import annotations

import argparse
import os
import time

from kun.security.auth import sign_auth_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument("--scopes", default="")
    parser.add_argument(
        "--audience", default="developer", choices=["novice", "developer", "expert"]
    )
    parser.add_argument("--ttl-sec", type=int, default=86400)
    args = parser.parse_args()
    secret = os.environ.get("KUN_AUTH_SECRET", "")
    if len(secret) < 32:
        raise SystemExit("KUN_AUTH_SECRET must be set to at least 32 characters")
    token = sign_auth_token(
        {
            "tenant_id": args.tenant,
            "user_id": args.user or None,
            "scopes": [item.strip() for item in args.scopes.split(",") if item.strip()],
            "audience": args.audience,
            "exp": int(time.time()) + args.ttl_sec,
        },
        secret,
    )
    print(token)


if __name__ == "__main__":
    main()
