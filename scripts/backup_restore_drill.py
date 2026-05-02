#!/usr/bin/env python3
"""Run a local backup / restore dry-run drill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kun.ops.backup_restore import (
    create_backup_package,
    default_allowed_roots,
    default_backup_sources,
    restore_dry_run,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="KUN local backup/restore drill")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create tar.gz backup package + manifest")
    create.add_argument("--repo-root", type=Path, default=Path.cwd())
    create.add_argument("--output-dir", type=Path, default=Path("backups"))
    create.add_argument("--source", type=Path, action="append", default=[])

    dry_run = sub.add_parser("restore-dry-run", help="validate backup package without writes")
    dry_run.add_argument("manifest", type=Path)
    dry_run.add_argument("--restore-root", type=Path, default=Path(".kun-restore-dry-run"))

    args = parser.parse_args()
    if args.command == "create":
        repo_root = args.repo_root.resolve()
        sources = args.source or default_backup_sources(repo_root)
        manifest = create_backup_package(
            source_paths=sources,
            output_dir=args.output_dir,
            repo_root=repo_root,
            allowed_roots=default_allowed_roots(repo_root),
        )
        print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0

    report = restore_dry_run(manifest_path=args.manifest, restore_root=args.restore_root)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if report.status != "block" else 2


if __name__ == "__main__":
    raise SystemExit(main())
