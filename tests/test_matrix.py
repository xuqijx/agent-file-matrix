"""Offline acceptance tests for agent-file-matrix v0.

Network libraries are rejected by source inspection. check.py additionally
patches socket.connect before importing the suite. Fixtures are not written.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_file_matrix.imports import find_imports  # noqa: E402
from agent_file_matrix.report import MATRIX_SNAPSHOT_DATE, build_report  # noqa: E402

SENTINELS = (
    "SENTINEL_SKILL_BODY_9f3a7c",
    "SENTINEL_OUTSIDE_BODY_41c0ee",
    "UNRELATED_BODY_SENTINEL_88e1",
    "CURSOR_MD_BODY_SENTINEL_22ab",
    "CURSORRULES_BODY_SENTINEL_23cd",
    "MDC_BODY_SENTINEL_24ef",
    "COPILOT_BODY_SENTINEL_25aa",
    "CURSOR_FM_BODY_SENTINEL_26bb",
)


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_file_matrix", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def load(args: list[str]) -> tuple[int, dict, str]:
    proc = run(args)
    report = json.loads(proc.stdout)
    return proc.returncode, report, proc.stderr


def ids(report: dict, check_id: str | None = None) -> list[dict]:
    if check_id is None:
        return report["diagnostics"]
    return [item for item in report["diagnostics"] if item["id"] == check_id]


@contextmanager
def forbid_opens_outside(root: Path):
    """Fail if instruction parsing opens a path outside the scan root."""
    root_abs = Path(os.path.abspath(root))
    real_open = os.open

    def guarded(path, flags, *args, **kwargs):  # noqa: ANN001
        target = Path(os.path.abspath(path))
        try:
            target.relative_to(root_abs)
        except ValueError as exc:
            raise AssertionError(f"opened outside repo: {target}") from exc
        return real_open(path, flags, *args, **kwargs)

    os.open = guarded  # type: ignore[assignment]
    try:
        yield
    finally:
        os.open = real_open  # type: ignore[assignment]


def cli_json(root: Path, strict: bool = False) -> tuple[int, dict, str]:
    args = ["--json", str(root)]
    if strict:
        args = ["--strict", *args]
    proc = run(args)
    report = json.loads(proc.stdout)
    return proc.returncode, report, proc.stdout + proc.stderr


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


class LexerTests(unittest.TestCase):
    def test_not_first_line_and_equivalents(self) -> None:
        text = "hello\n\nsee @AGENTS.md and @./AGENTS.md and @././AGENTS.md\n"
        tokens = [item.token for item in find_imports(text)]
        self.assertEqual(tokens, ["AGENTS.md", "./AGENTS.md", "././AGENTS.md"])

    def test_span_and_fence_skipped(self) -> None:
        text = "\n".join(
            [
                "The token `@AGENTS.md` is a span.",
                "```",
                "@docs/missing.md",
                "```",
                "~~~",
                "@docs/tilde.md",
                "~~~",
                "   ```",
                "   @docs/indented.md",
                "   ```",
                "after @docs/real-missing.md",
            ]
        )
        tokens = [item.token for item in find_imports(text)]
        self.assertEqual(tokens, ["docs/real-missing.md"])

    def test_unclosed_fence_masks_rest(self) -> None:
        text = "```\n@docs/missing.md\nno close\n"
        self.assertEqual(find_imports(text), [])

    def test_quoted_path_is_not_an_import(self) -> None:
        self.assertEqual(find_imports('See @"AGENTS.md" and @\'AGENTS.md\'.\n'), [])

    def test_email_is_not_an_import(self) -> None:
        self.assertEqual(find_imports("mail dev@example.com today\n"), [])

    def test_escaped_space(self) -> None:
        tokens = find_imports(r"load @Design\ Docs/api.md now")
        self.assertEqual([item.token for item in tokens], ["Design Docs/api.md"])

    def test_four_space_indent_is_not_a_fence(self) -> None:
        tokens = find_imports("    @docs/missing.md\n")
        self.assertEqual([item.token for item in tokens], ["docs/missing.md"])


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hash_before = fixture_hash()

    @classmethod
    def tearDownClass(cls) -> None:
        after = fixture_hash()
        if after != cls.hash_before:
            raise AssertionError("fixture hash changed during tests")

    def assert_no_sentinels(self, blob: str) -> None:
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, blob)

    def test_ok_shared(self) -> None:
        code, report, err = load(["--json", "fixtures/ok-shared"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["matrix_snapshot_date"], MATRIX_SNAPSHOT_DATE)
        self.assertEqual(report["matrix_snapshot_date"], "2026-11-06")
        self.assertIn("does not empirically verify", report["disclaimer"])
        self.assertEqual(ids(report), [])
        self.assertEqual([item["path"] for item in report["files"]], ["AGENTS.md", "CLAUDE.md"])
        self.assertEqual(report["files_read"], ["AGENTS.md", "CLAUDE.md"])
        joined = json.dumps(report)
        self.assertNotIn("Prefer small patches", joined)
        again = run(["--json", "fixtures/ok-shared"])
        self.assertEqual(again.stdout, run(["--json", "fixtures/ok-shared"]).stdout)

    def test_shadow_default_and_strict(self) -> None:
        code, report, _err = load(["--json", "fixtures/shadow-no-import"])
        self.assertEqual(code, 0)
        shadows = ids(report, "shadow-no-import")
        self.assertEqual(len(shadows), 1)
        self.assertEqual(shadows[0]["severity"], "warning")
        self.assertEqual(shadows[0]["path"], "CLAUDE.md")
        self.assertFalse(shadows[0]["strict_upgraded"])
        self.assertEqual(ids(report, "broken-at-import"), [])

        strict_code, strict, _err = load(["--json", "--strict", "fixtures/shadow-no-import"])
        self.assertEqual(strict_code, 1)
        only = ids(strict, "shadow-no-import")
        self.assertEqual(only[0]["severity"], "error")
        self.assertEqual(only[0]["base_severity"], "warning")
        self.assertTrue(only[0]["strict_upgraded"])
        self.assertEqual(strict["summary"]["errors"], 1)

        text = run(["fixtures/shadow-no-import"])
        self.assertEqual(text.returncode, 0)
        self.assertIn("shadow-no-import", text.stdout)
        self.assertIn("does not empirically verify", text.stdout)

    def test_broken_import_exit_1(self) -> None:
        code, report, _err = load(["fixtures/broken-import", "--json"])
        self.assertEqual(code, 1)
        broken = ids(report, "broken-at-import")
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["severity"], "error")
        self.assertEqual(broken[0]["import_target"], "docs/missing.md")
        self.assertEqual(broken[0]["line"], 3)
        self.assertIn("docs/missing.md", broken[0]["message"])
        self.assertEqual(ids(report, "shadow-no-import"), [])
        text = run(["fixtures/broken-import"])
        self.assertEqual(text.returncode, 1)
        self.assertIn("docs/missing.md", text.stdout)
        self.assertIn("broken-at-import", text.stdout)

    def test_ignored_cursor_md(self) -> None:
        code, report, _err = load(["--json", "fixtures/ignored-cursor-md"])
        self.assertEqual(code, 0)
        found = ids(report, "ignored-cursor-md")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "warning")
        self.assertEqual(found[0]["path"], ".cursor/rules/notes.md")
        self.assertNotIn(".cursor/rules/notes.md", report["files_read"])
        self.assert_no_sentinels(json.dumps(report))

    def test_legacy_cursorrules(self) -> None:
        code, report, _err = load(["--json", "fixtures/legacy-cursorrules"])
        self.assertEqual(code, 0)
        found = ids(report, "legacy-instruction-file")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["path"], ".cursorrules")
        self.assertEqual(found[0]["severity"], "warning")
        self.assertNotIn(".cursorrules", report["files_read"])
        self.assert_no_sentinels(json.dumps(report))
        strict_code, strict, _err = load(["--strict", "--json", "fixtures/legacy-cursorrules"])
        self.assertEqual(strict_code, 0)
        self.assertEqual(ids(strict, "legacy-instruction-file")[0]["severity"], "warning")

    def test_legacy_both_are_separate_warnings(self) -> None:
        code, report, _err = load(["--json", "fixtures/legacy-both"])
        self.assertEqual(code, 0)
        paths = sorted(item["path"] for item in ids(report, "legacy-instruction-file"))
        self.assertEqual(paths, [".cursorrules", ".windsurfrules"])

    def test_non_first_line_and_dot_forms_suppress_shadow(self) -> None:
        for fixture in ("fixtures/b1-not-first-line", "fixtures/b1-dot-slash", "fixtures/b1-dot-dot-slash"):
            code, report, _err = load(["--json", fixture])
            self.assertEqual(code, 0, fixture)
            self.assertEqual(ids(report, "shadow-no-import"), [], fixture)
            self.assertEqual(ids(report, "broken-at-import"), [], fixture)

    def test_each_claude_path_is_independent(self) -> None:
        code, dot, _err = load(["--json", "fixtures/b2-dot-claude"])
        self.assertEqual(code, 0)
        self.assertEqual([item["path"] for item in ids(dot, "shadow-no-import")], [".claude/CLAUDE.md"])

        code, local, _err = load(["--json", "fixtures/b2-claude-local"])
        self.assertEqual(code, 0)
        self.assertEqual([item["path"] for item in ids(local, "shadow-no-import")], ["CLAUDE.local.md"])

        code, partial, _err = load(["--json", "fixtures/b2-partial"])
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(item["path"] for item in ids(partial, "shadow-no-import")),
            [".claude/CLAUDE.md", "CLAUDE.local.md"],
        )
        self.assertEqual(ids(partial, "broken-at-import"), [])

        code, imported, _err = load(["--json", "fixtures/b2-dot-import"])
        self.assertEqual(code, 0)
        self.assertEqual(ids(imported, "shadow-no-import"), [])
        self.assertEqual(ids(imported, "broken-at-import"), [])

        code, literal, _err = load(["--json", "fixtures/b2-dot-literal"])
        self.assertEqual(code, 1)
        self.assertEqual([item["path"] for item in ids(literal, "shadow-no-import")], [".claude/CLAUDE.md"])
        broken = ids(literal, "broken-at-import")
        self.assertEqual(broken[0]["import_target"], ".claude/AGENTS.md")

    def test_fence_and_span_false_positive(self) -> None:
        code, report, _err = load(["--json", "fixtures/b3-fence-span"])
        self.assertEqual(code, 0)
        self.assertEqual(len(ids(report, "shadow-no-import")), 1)
        self.assertEqual(ids(report, "broken-at-import"), [])

    def test_broken_import_inside_agents(self) -> None:
        code, report, _err = load(["--json", "fixtures/b4-broken-agents"])
        self.assertEqual(code, 1)
        broken = ids(report, "broken-at-import")
        self.assertEqual(broken[0]["path"], "AGENTS.md")
        self.assertEqual(broken[0]["import_target"], "path/missing.md")

    def test_frontmatter_md_still_warns_and_mdc_has_no_schema_check(self) -> None:
        code, report, _err = load(["--json", "fixtures/b5-frontmatter-md"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [item["path"] for item in ids(report, "ignored-cursor-md")],
            [".cursor/rules/with-fm.md"],
        )
        self.assertTrue(any(item["path"] == ".cursor/rules/ok.mdc" for item in report["files"]))
        schemaish = [item["id"] for item in report["diagnostics"]]
        self.assertNotIn("mdc-schema", schemaish)
        self.assertEqual(report["files_read"], ["AGENTS.md"])
        self.assert_no_sentinels(json.dumps(report))

    def test_strict_does_not_upgrade_other_warnings(self) -> None:
        code, report, _err = load(["--json", "fixtures/b6-strict-other"])
        strict_code, strict, _err = load(["--json", "--strict", "fixtures/b6-strict-other"])
        self.assertEqual(code, 0)
        self.assertEqual(strict_code, 0)
        self.assertEqual(
            [(item["id"], item["severity"]) for item in report["diagnostics"]],
            [(item["id"], item["severity"]) for item in strict["diagnostics"]],
        )
        self.assertIn("legacy-instruction-file", {item["id"] for item in report["diagnostics"]})
        self.assertIn("ignored-cursor-md", {item["id"] for item in report["diagnostics"]})
        self.assertIn("missing-canonical", {item["id"] for item in report["diagnostics"]})
        self.assertTrue(all(item["severity"] == "warning" for item in report["diagnostics"]))

    def test_nested_agents_is_info(self) -> None:
        code, report, _err = load(["--json", "fixtures/b13-nested-agents"])
        self.assertEqual(code, 0)
        found = ids(report, "nested-agents")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "info")
        self.assertEqual(found[0]["path"], "pkg/AGENTS.md")

    def test_missing_canonical(self) -> None:
        code, report, _err = load(["--json", "fixtures/b14-missing-canonical"])
        self.assertEqual(code, 0)
        found = ids(report, "missing-canonical")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "warning")
        self.assertEqual(found[0]["related_paths"], [".github/copilot-instructions.md"])
        self.assertEqual(report["files_read"], [])
        self.assert_no_sentinels(json.dumps(report))

    def test_unrelated_body_not_printed(self) -> None:
        proc = run(["--json", "fixtures/b16-unrelated"])
        self.assertEqual(proc.returncode, 0)
        self.assert_no_sentinels(proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual([item["path"] for item in report["files"]], ["AGENTS.md"])

    def test_skill_sentinel_is_not_opened(self) -> None:
        import agent_file_matrix.scan as scan

        opened: list[str] = []
        real_open = os.open

        def guarded(path, flags, *args, **kwargs):  # noqa: ANN001
            text = os.fspath(path)
            opened.append(text)
            if text.endswith("SKILL.md") or f"{os.sep}skills{os.sep}" in text:
                raise AssertionError(f"opened skill path {text}")
            return real_open(path, flags, *args, **kwargs)

        real_read = scan.read_instruction_file

        def wrapped(path: Path):
            if path.name == "SKILL.md" or "skills" in path.parts:
                raise AssertionError(path)
            return real_read(path)

        proc = run(["--json", "fixtures/skill-sentinel"])
        self.assertEqual(proc.returncode, 0)
        self.assert_no_sentinels(proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(any(item["id"] == "skipped-skill" for item in report["diagnostics"]))
        for rel in report["files_read"]:
            self.assertNotIn("SKILL.md", rel)
            self.assertNotIn("skills/", rel)
        self.assertFalse(any(item["id"] == "broken-at-import" for item in report["diagnostics"]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")
            skill = root / ".claude" / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("SENTINEL_SKILL_BODY_9f3a7c\n", encoding="utf-8")
            (root / "tools").mkdir()
            (root / "tools" / "SKILL.md").write_text("SENTINEL_SKILL_BODY_9f3a7c\n", encoding="utf-8")
            original = scan.read_instruction_file
            scan.read_instruction_file = wrapped
            os.open = guarded  # type: ignore[assignment]
            try:
                report = build_report(root, strict=False)
            finally:
                scan.read_instruction_file = original
                os.open = real_open  # type: ignore[assignment]
            blob = json.dumps(report)
            self.assertNotIn("SENTINEL_SKILL_BODY_9f3a7c", blob)
            self.assertTrue(any(item["id"] == "skipped-skill" for item in report["diagnostics"]))

    def test_symlink_outside_is_not_read(self) -> None:
        outside = Path("/tmp/agent-file-matrix-b7-outside.md")
        outside_dir = Path("/tmp/agent-file-matrix-b7-dir")
        outside.write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
        try:
            proc = run(["--json", "fixtures/b7-symlink-outside"])
        finally:
            outside.unlink(missing_ok=True)
            secret = outside_dir / "secret.txt"
            if secret.exists():
                secret.unlink()
            if outside_dir.exists():
                outside_dir.rmdir()
        self.assertEqual(proc.returncode, 0)
        self.assert_no_sentinels(proc.stdout + proc.stderr)
        self.assertNotIn("/tmp/agent-file-matrix-b7-outside.md", proc.stdout)
        report = json.loads(proc.stdout)
        self.assertTrue(any(item["id"] == "out-of-root-skip" for item in report["diagnostics"]))
        self.assertFalse(any(item["path"] == "AGENTS.md" and item.get("exists") for item in report["files"]))
        self.assertTrue(any(item["id"] == "legacy-instruction-file" for item in report["diagnostics"]))
        self.assertTrue(any(item["id"] == "missing-canonical" for item in report["diagnostics"]))

    def test_symlink_cycle_terminates(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_file_matrix", "--json", "fixtures/b8-symlink-cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertTrue(any(item["id"] == "symlink-cycle" for item in report["diagnostics"]))
        self.assertTrue(any(item["path"] == "AGENTS.md" for item in report["files"]))

    def test_usage_and_io_exit_2(self) -> None:
        self.assertEqual(run([]).returncode, 2)
        self.assertEqual(run(["--nope"]).returncode, 2)
        self.assertEqual(run(["fixtures/does-not-exist"]).returncode, 2)
        self.assertEqual(run(["--help"]).returncode, 0)
        with tempfile.NamedTemporaryFile() as handle:
            self.assertEqual(run([handle.name]).returncode, 2)
        wrapper = subprocess.run(
            [str(ROOT / "agent-file-matrix"), "fixtures/ok-shared", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(wrapper.returncode, 0)
        self.assertIn("AGENTS.md", wrapper.stdout)

    def test_unreadable_root_exit_2(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read mode 000 directories")
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "blocked"
            blocked.mkdir()
            blocked.chmod(0)
            try:
                proc = run([str(blocked)])
            finally:
                blocked.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("not readable", proc.stderr)

    def test_json_schema_and_all_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "AGENTS.md").write_text("# Nested\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
            (root / ".claude").mkdir()
            (root / ".claude" / "CLAUDE.md").write_text("@../AGENTS.md\n", encoding="utf-8")
            (root / "CLAUDE.local.md").write_text("@AGENTS.md\n", encoding="utf-8")
            rules = root / ".cursor" / "rules" / "nested"
            rules.mkdir(parents=True)
            (root / ".cursor" / "rules" / "a.mdc").write_text("mdc\n", encoding="utf-8")
            (rules / "b.mdc").write_text("mdc\n", encoding="utf-8")
            (root / ".cursor" / "rules" / "notes.md").write_text("md\n", encoding="utf-8")
            (root / ".cursorrules").write_text("legacy\n", encoding="utf-8")
            (root / ".windsurfrules").write_text("legacy\n", encoding="utf-8")
            (root / ".github").mkdir()
            (root / ".github" / "copilot-instructions.md").write_text("copilot\n", encoding="utf-8")
            (root / "GEMINI.md").write_text("gemini\n", encoding="utf-8")
            (root / ".devin" / "rules" / "nested").mkdir(parents=True)
            (root / ".devin" / "rules" / "one.md").write_text("devin\n", encoding="utf-8")
            (root / ".devin" / "rules" / "nested" / "skip.md").write_text("nope\n", encoding="utf-8")
            (root / ".kiro" / "steering").mkdir(parents=True)
            (root / ".kiro" / "steering" / "steer.md").write_text("kiro\n", encoding="utf-8")
            (root / ".kiro" / "steering" / "nested").mkdir()
            (root / ".kiro" / "steering" / "nested" / "no.md").write_text("nope\n", encoding="utf-8")
            report = build_report(root, strict=False)
        kinds = {item["path"]: item["kind"] for item in report["files"]}
        self.assertEqual(kinds["AGENTS.md"], "agents-md")
        self.assertEqual(kinds["sub/AGENTS.md"], "agents-md")
        self.assertEqual(kinds["CLAUDE.md"], "claude-md")
        self.assertEqual(kinds[".claude/CLAUDE.md"], "claude-md")
        self.assertEqual(kinds["CLAUDE.local.md"], "claude-local-md")
        self.assertEqual(kinds[".cursor/rules/a.mdc"], "cursor-rule-mdc")
        self.assertEqual(kinds[".cursor/rules/nested/b.mdc"], "cursor-rule-mdc")
        self.assertEqual(kinds[".cursor/rules/notes.md"], "cursor-rule-md")
        self.assertEqual(kinds[".cursorrules"], "cursorrules")
        self.assertEqual(kinds[".windsurfrules"], "windsurfrules")
        self.assertEqual(kinds[".github/copilot-instructions.md"], "copilot-instructions")
        self.assertEqual(kinds["GEMINI.md"], "gemini-md")
        self.assertEqual(kinds[".devin/rules/one.md"], "devin-rule")
        self.assertEqual(kinds[".kiro/steering/steer.md"], "kiro-steering")
        self.assertNotIn(".devin/rules/nested/skip.md", kinds)
        self.assertNotIn(".kiro/steering/nested/no.md", kinds)
        agents = next(item for item in report["files"] if item["path"] == "AGENTS.md")
        self.assertEqual(agents["runtimes"][0], "Cursor")
        self.assertIn("Claude Code", agents["runtimes"])
        gemini = next(item for item in report["files"] if item["path"] == "GEMINI.md")
        self.assertEqual(gemini["runtimes"], [])
        keys = {
            "citations",
            "diagnostics",
            "disclaimer",
            "exit_code",
            "files",
            "files_read",
            "matrix_snapshot_date",
            "matrix_source",
            "repo_root",
            "schema_version",
            "strict",
            "summary",
        }
        self.assertEqual(set(report), keys)
        file_keys = {"citations", "exists", "kind", "note", "path", "runtimes"}
        self.assertEqual(set(report["files"][0]), file_keys)
        diag_keys = {
            "base_severity",
            "id",
            "import_target",
            "line",
            "message",
            "path",
            "related_paths",
            "severity",
            "strict_upgraded",
        }
        self.assertEqual(set(report["diagnostics"][0]), diag_keys)
        self.assertEqual(report["schema_version"], 1)

    def test_strict_upgrades_only_shadow_when_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("no import\n", encoding="utf-8")
            (root / ".cursorrules").write_text("legacy\n", encoding="utf-8")
            plain = build_report(root, strict=False)
            strict = build_report(root, strict=True)
        self.assertEqual(plain["exit_code"], 0)
        self.assertEqual(strict["exit_code"], 1)
        plain_legacy = next(item for item in plain["diagnostics"] if item["id"] == "legacy-instruction-file")
        strict_legacy = next(item for item in strict["diagnostics"] if item["id"] == "legacy-instruction-file")
        self.assertEqual(plain_legacy["severity"], "warning")
        self.assertEqual(strict_legacy["severity"], "warning")
        self.assertEqual(
            next(item["severity"] for item in strict["diagnostics"] if item["id"] == "shadow-no-import"),
            "error",
        )

    def test_existing_import_target_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "exists.md").write_text("UNRELATED_BODY_SENTINEL_88e1\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("@AGENTS.md\n@docs/exists.md\n", encoding="utf-8")
            report = build_report(root, strict=False)
        blob = json.dumps(report)
        self.assertNotIn("UNRELATED_BODY_SENTINEL_88e1", blob)
        self.assertEqual(ids(report, "broken-at-import"), [])
        self.assertEqual(ids(report, "shadow-no-import"), [])
        self.assertEqual(report["files_read"], ["AGENTS.md", "CLAUDE.md"])

    def test_import_escaping_root_is_not_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("@AGENTS.md\n@../outside-missing.md\n", encoding="utf-8")
            report = build_report(root, strict=False)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(ids(report, "broken-at-import"), [])
        skipped = [item for item in report["diagnostics"] if item["id"] == "out-of-root-skip"]
        self.assertTrue(skipped)
        self.assertEqual(skipped[0]["import_target"], "../outside-missing.md")
        self.assertNotIn(str(Path(tmp).resolve().parent / "outside-missing.md"), json.dumps(report))

    def test_source_does_not_import_network_modules(self) -> None:
        banned = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib"}
        for path in (ROOT / "agent_file_matrix").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], banned, path)
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").split(".")[0]
                    self.assertNotIn(module, banned, path)


class ContractATests(unittest.TestCase):
    """Contract A, probed against this tree before the docs were aligned.

    The original suite did not cover an absolute ``@`` import that stays
    inside the repo root, an absolute path that leaves it, or ``~/``.
    Relative escape was only the missing ``../outside-missing.md`` case,
    without a sentinel body and without ``--strict``.
    """

    def _prepare(self, root: Path, claude: str) -> None:
        (root / "AGENTS.md").write_text("# Root canon\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text(claude, encoding="utf-8")

    def _assert_pair(
        self,
        root: Path,
        default_exit: int,
        strict_exit: int,
        shadow: bool,
        skip_tokens: list[str],
        broken_targets: list[str] | None = None,
    ) -> None:
        broken_targets = broken_targets or []
        for strict, expected in ((False, default_exit), (True, strict_exit)):
            with forbid_opens_outside(root):
                report = build_report(root, strict=strict)
            self._assert_report(report, expected, strict, shadow, skip_tokens, broken_targets)
            code, cli, blob = cli_json(root, strict=strict)
            self.assertEqual(code, expected)
            self.assertNotIn("SENTINEL_OUTSIDE_BODY_41c0ee", blob)
            self._assert_report(cli, expected, strict, shadow, skip_tokens, broken_targets)

    def _assert_report(
        self,
        report: dict,
        expected: int,
        strict: bool,
        shadow: bool,
        skip_tokens: list[str],
        broken_targets: list[str],
    ) -> None:
        self.assertEqual(report["exit_code"], expected)
        self.assertNotIn("SENTINEL_OUTSIDE_BODY_41c0ee", json.dumps(report))
        shadows = ids(report, "shadow-no-import")
        if shadow:
            self.assertEqual(len(shadows), 1)
            self.assertEqual(shadows[0]["severity"], "error" if strict else "warning")
            self.assertEqual(shadows[0]["strict_upgraded"], strict)
        else:
            self.assertEqual(shadows, [])
            self.assertTrue(all(not item["strict_upgraded"] for item in report["diagnostics"]))
        self.assertEqual(
            sorted(item["import_target"] for item in ids(report, "out-of-root-skip")),
            sorted(skip_tokens),
        )
        self.assertEqual(
            sorted(item["import_target"] for item in ids(report, "broken-at-import")),
            sorted(broken_targets),
        )
        for rel in report["files_read"]:
            self.assertNotIn("..", Path(rel).parts)

    def test_in_root_absolute_import_clears_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / "AGENTS.md"
            outside = root.parent / f"contract-a-neighbor-{root.name}.md"
            outside.write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
            try:
                self._prepare(root, f"@{agents}\n")
                self._assert_pair(root, 0, 0, shadow=False, skip_tokens=[])
                report = build_report(root, strict=False)
                self.assertEqual(report["files_read"], ["AGENTS.md", "CLAUDE.md"])
            finally:
                outside.unlink(missing_ok=True)

    def test_in_root_absolute_missing_is_broken_not_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "docs" / "missing.md"
            self._prepare(root, f"@{missing}\n")
            self._assert_pair(
                root,
                1,
                1,
                shadow=True,
                skip_tokens=[],
                broken_targets=["docs/missing.md"],
            )

    def test_absolute_escape_is_skip_and_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / f"contract-a-abs-{root.name}.md"
            outside.write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
            try:
                self._prepare(root, f"@{outside}\n")
                self._assert_pair(root, 0, 1, shadow=True, skip_tokens=[str(outside)])
            finally:
                outside.unlink(missing_ok=True)

    def test_tilde_escape_is_skip_and_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = f"contract-a-tilde-{root.name}.md"
            home_file = Path.home() / name
            home_file.write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
            try:
                self._prepare(root, f"@~/{name}\n")
                self._assert_pair(root, 0, 1, shadow=True, skip_tokens=[f"~/{name}"])
            finally:
                home_file.unlink(missing_ok=True)

    def test_relative_escape_is_skip_and_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = f"contract-a-rel-{root.name}.md"
            outside = root.parent / name
            outside.write_text("SENTINEL_OUTSIDE_BODY_41c0ee\n", encoding="utf-8")
            try:
                self._prepare(root, f"@../{name}\n")
                self._assert_pair(root, 0, 1, shadow=True, skip_tokens=[f"../{name}"])
            finally:
                outside.unlink(missing_ok=True)

    def test_tilde_inside_root_is_in_repo_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root, "@~/AGENTS.md\n")
            old = os.environ.get("HOME")
            os.environ["HOME"] = str(root)
            try:
                self._assert_pair(root, 0, 0, shadow=False, skip_tokens=[])
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old


if __name__ == "__main__":
    unittest.main()
