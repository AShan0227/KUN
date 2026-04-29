#!/usr/bin/env python3
"""V3 ops dogfood smoke report.

This is intentionally offline and cheap: it imports the delivery-status catalog,
derives dogfood scenarios, validates they are honest, and prints a JSON report.
Use it as the first release-candidate gate before running any expensive or
external-world dogfood.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kun.engineering.ops_dogfood import dogfood_scenario_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate V3 ops dogfood smoke report.")
    parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="Exit non-zero if any scenario is blocked.",
    )
    parser.add_argument(
        "--report-path",
        default="",
        help="Optional path to write the JSON report.",
    )
    args = parser.parse_args(argv)

    report = dogfood_scenario_report()
    data = report.model_dump(mode="json")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.report_path:
        Path(args.report_path).write_text(text + "\n", encoding="utf-8")
    print(text)

    if report.validation_issues:
        return 2
    if args.fail_on_blocked and report.summary.get("blocked", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
