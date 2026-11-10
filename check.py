#!/usr/bin/env python3
"""Run the offline fixture matrix.

Blocks socket egress inside this process, runs the shipped fixtures, then
the unittest suite. Prints a sha256 of fixture file bytes before and after
(find -type f | sort | sha256sum pipeline) and exits non-zero if anything
fails or the hash changes. Does not modify fixtures.
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

MATRIX = [
    (["fixtures/ok-shared"], 0),
    (["fixtures/shadow-no-import"], 0),
    (["fixtures/shadow-no-import", "--strict"], 1),
    (["fixtures/broken-import"], 1),
    (["fixtures/ignored-cursor-md"], 0),
    (["fixtures/legacy-cursorrules"], 0),
    (["fixtures/b3-fence-span"], 0),
    (["fixtures/b6-strict-other", "--strict"], 0),
    (["fixtures/skill-sentinel"], 0),
    (["fixtures/b8-symlink-cycle"], 0),
]


def block_network() -> None:
    class Guard(socket.socket):
        def connect(self, address):  # noqa: ANN001
            raise RuntimeError(f"egress blocked: {address}")

        def connect_ex(self, address):  # noqa: ANN001
            raise RuntimeError(f"egress blocked: {address}")

    socket.socket = Guard  # type: ignore[misc]

    def blocked(*_args, **_kwargs):
        raise RuntimeError("egress blocked")

    socket.create_connection = blocked  # type: ignore[assignment]
    if hasattr(socket, "create_server"):
        socket.create_server = blocked  # type: ignore[assignment]


def fixture_hash() -> str:
    command = "find fixtures -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum"
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def run_matrix() -> int:
    from agent_file_matrix.cli import main

    failed = 0
    for args, expected in MATRIX:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        status = "ok" if code == expected else "FAIL"
        if code != expected:
            failed += 1
        print(f"matrix {status} exit={code} expected={expected} args={' '.join(args)}")
        if code != expected:
            print(stdout.getvalue())
            print(stderr.getvalue(), file=sys.stderr)
    return failed


def main() -> int:
    os.chdir(ROOT)
    sys.stdout.reconfigure(line_buffering=True)
    block_network()
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.2)
    except RuntimeError as exc:
        print(f"egress_guard {exc}")
    else:
        print("egress guard did not block create_connection", file=sys.stderr)
        return 1

    before = fixture_hash()
    print(f"fixtures_sha256_before {before}")
    failed = run_matrix()
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    after = fixture_hash()
    print(f"fixtures_sha256_after {after}")
    if before != after:
        print("fixture hash changed; scanned trees must stay unmodified", file=sys.stderr)
        return 1
    if failed or not result.wasSuccessful():
        return 1
    print("check.py OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
