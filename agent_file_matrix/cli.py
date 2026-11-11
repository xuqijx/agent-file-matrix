"""CLI entry for agent-file-matrix."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from agent_file_matrix import __version__
from agent_file_matrix.report import DISCLAIMER, MATRIX_SNAPSHOT_DATE, MATRIX_SOURCE, build_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-file-matrix",
        description=(
            "Offline inventory of known agent instruction files and mechanical load traps. "
            + DISCLAIMER
        ),
    )
    parser.add_argument("repo_root", help="repository root to scan (read-only)")
    parser.add_argument("--json", action="store_true", help="print a stable JSON document")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="upgrade only shadow-no-import from warning to error",
    )
    parser.add_argument("--version", action="version", version=f"agent-file-matrix {__version__}")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)

    root = Path(os.path.abspath(args.repo_root))
    if not root.exists():
        print(f"error: repo root does not exist: {args.repo_root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"error: repo root is not a directory: {args.repo_root}", file=sys.stderr)
        return 2
    if not os.access(root, os.R_OK | os.X_OK):
        print(f"error: repo root is not readable: {args.repo_root}", file=sys.stderr)
        return 2

    try:
        report = build_report(root, strict=args.strict)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _write_text(report, sys.stdout)
    return int(report["exit_code"])


def _write_text(report: dict, out) -> None:
    print("agent-file-matrix", file=out)
    print(f"disclaimer: {report['disclaimer']}", file=out)
    print(f"matrix_snapshot_date: {MATRIX_SNAPSHOT_DATE}", file=out)
    print(f"matrix_source: {MATRIX_SOURCE}", file=out)
    print(f"repo: {report['repo_root']}", file=out)
    print(f"strict: {str(report['strict']).lower()}", file=out)
    print("", file=out)
    print("FILES", file=out)
    if not report["files"]:
        print("(no known agent instruction files)", file=out)
    for item in report["files"]:
        runtimes = ", ".join(item["runtimes"]) if item["runtimes"] else "(none claimed)"
        print(f"- {item['path']}", file=out)
        print(f"    kind: {item['kind']}", file=out)
        print(f"    exists: true", file=out)
        print(f"    runtimes: {runtimes}", file=out)
        print(f"    note: {item['note']}", file=out)
    print("", file=out)
    print("DIAGNOSTICS", file=out)
    if not report["diagnostics"]:
        print("(none)", file=out)
    for item in report["diagnostics"]:
        where = item["path"] or "-"
        print(f"- [{item['severity']}] {item['id']}  {where}", file=out)
        print(f"    {item['message']}", file=out)
    summary = report["summary"]
    print("", file=out)
    print(
        "SUMMARY "
        f"errors={summary['errors']} "
        f"warnings={summary['warnings']} "
        f"infos={summary['infos']} "
        f"exit={report['exit_code']}",
        file=out,
    )


if __name__ == "__main__":
    sys.exit(main())
