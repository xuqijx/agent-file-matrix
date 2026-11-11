"""Build the load-surface report and the six v0 diagnostics.

Shadow pairing (v0, writer-confirmed): ``shadow-no-import`` compares each
existing CLAUDE-side file with the repository-root ``AGENTS.md`` only.
An ``@`` import suppresses that warning only when it resolves to that root
file. ``@AGENTS.md`` and ``@./AGENTS.md`` are equivalent after resolution
relative to the instruction file's directory. Contract A: an absolute
``@/path`` or a ``~/...`` expansion that normalizes inside the repo root is
a normal in-repo import, not ``out-of-root-skip``.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

from agent_file_matrix.imports import find_imports
from agent_file_matrix.scan import (
    FoundFile,
    ResolvedImport,
    resolve_import,
    resolve_inside,
    scan_repository,
)

MATRIX_SNAPSHOT_DATE = "2026-11-06"
MATRIX_SOURCE = "https://www.ai-standards.dev/docs/handbook/open-agent-standards"
DISCLAIMER = (
    "Load-surface assumption against cited public docs "
    "(AIES handbook matrix snapshot 2026-11-06, Claude Code memory, "
    "and Cursor Rules). This tool does not run agents and does not "
    "empirically verify every runtime."
)

CITATIONS = {
    "aies-2026-11-06": {
        "title": "Open Agent Standards and the Cross-Tool File Matrix",
        "url": MATRIX_SOURCE,
        "updated": MATRIX_SNAPSHOT_DATE,
    },
    "claude-memory": {
        "title": "Claude Code memory (CLAUDE.md, AGENTS.md, @imports)",
        "url": "https://code.claude.com/docs/en/memory",
        "updated": None,
    },
    "cursor-rules": {
        "title": "Cursor Rules",
        "url": "https://cursor.com/docs/rules",
        "updated": None,
    },
    "cursor-help-rules": {
        "title": "Cursor Help: Rules",
        "url": "https://cursor.com/help/customization/rules",
        "updated": None,
    },
    "agents-md": {
        "title": "AGENTS.md",
        "url": "https://agents.md/",
        "updated": None,
    },
}

_TOOL_KINDS = frozenset(
    {
        "claude-md",
        "claude-local-md",
        "cursor-rule-mdc",
        "cursor-rule-md",
        "cursorrules",
        "windsurfrules",
        "copilot-instructions",
        "gemini-md",
        "devin-rule",
        "kiro-steering",
    }
)

_CLAUDE_KINDS = frozenset({"claude-md", "claude-local-md"})
_IMPORT_KINDS = frozenset({"agents-md", "claude-md", "claude-local-md"})


def build_report(root: Path, strict: bool) -> dict:
    scan = scan_repository(root)
    diagnostics = [_operational_dict(item) for item in scan.operational]
    root_agents = next((item for item in scan.files if item.rel == "AGENTS.md"), None)
    root_final = _final_file(root_agents, scan.root) if root_agents else None

    for item in scan.files:
        if item.kind not in _IMPORT_KINDS or item.text is None:
            continue
        resolved = [resolve_import(item.logical, token, scan.root) for token in find_imports(item.text)]
        for result in resolved:
            diagnostics.extend(_import_diagnostics(item, result))
        if item.kind in _CLAUDE_KINDS and root_agents is not None:
            if not any(_points_at_root(result, root_final) for result in resolved):
                diagnostics.append(
                    {
                        "id": "shadow-no-import",
                        "severity": "warning",
                        "base_severity": "warning",
                        "strict_upgraded": False,
                        "path": item.rel,
                        "line": None,
                        "import_target": None,
                        "related_paths": ["AGENTS.md"],
                        "message": (
                            "root AGENTS.md is present, but this CLAUDE-side file has no "
                            "@import that resolves to it (@AGENTS.md and @./AGENTS.md are "
                            "equivalent; paths resolve relative to this file; code spans "
                            "and fences are ignored)"
                        ),
                    }
                )

    for item in scan.files:
        if item.kind in {"cursorrules", "windsurfrules"}:
            diagnostics.append(
                {
                    "id": "legacy-instruction-file",
                    "severity": "warning",
                    "base_severity": "warning",
                    "strict_upgraded": False,
                    "path": item.rel,
                    "line": None,
                    "import_target": None,
                    "related_paths": [],
                    "message": (
                        "legacy instruction file is still present "
                        "(AIES: legacy files may still be read and silently override; "
                        "Cursor Help: .cursorrules will be deprecated)"
                    ),
                }
            )
        elif item.kind == "cursor-rule-md":
            diagnostics.append(
                {
                    "id": "ignored-cursor-md",
                    "severity": "warning",
                    "base_severity": "warning",
                    "strict_upgraded": False,
                    "path": item.rel,
                    "line": None,
                    "import_target": None,
                    "related_paths": [],
                    "message": (
                        "plain .md under .cursor/rules/ is ignored by Cursor's rules system "
                        "(extension is not .mdc; frontmatter is not checked in v0)"
                    ),
                }
            )
        elif item.kind == "agents-md" and item.rel != "AGENTS.md":
            diagnostics.append(
                {
                    "id": "nested-agents",
                    "severity": "info",
                    "base_severity": "info",
                    "strict_upgraded": False,
                    "path": item.rel,
                    "line": None,
                    "import_target": None,
                    "related_paths": [],
                    "message": "nested AGENTS.md is scoped to its directory (info only)",
                }
            )

    tool_paths = sorted(item.rel for item in scan.files if item.kind in _TOOL_KINDS)
    if tool_paths and root_agents is None:
        diagnostics.append(
            {
                "id": "missing-canonical",
                "severity": "warning",
                "base_severity": "warning",
                "strict_upgraded": False,
                "path": "",
                "line": None,
                "import_target": None,
                "related_paths": tool_paths,
                "message": (
                    "tool-specific instruction files exist but root AGENTS.md is missing "
                    "(AIES treats AGENTS.md as canonical)"
                ),
            }
        )

    diagnostics = _dedupe(diagnostics)
    if strict:
        for item in diagnostics:
            if item["id"] == "shadow-no-import" and item["base_severity"] == "warning":
                item["severity"] = "error"
                item["strict_upgraded"] = True
    diagnostics.sort(key=lambda item: (item["path"], item["id"], item["line"] or 0, item["import_target"] or "", item["message"]))

    errors = sum(1 for item in diagnostics if item["severity"] == "error")
    warnings = sum(1 for item in diagnostics if item["severity"] == "warning")
    infos = sum(1 for item in diagnostics if item["severity"] == "info")
    exit_code = 1 if errors else 0
    return {
        "schema_version": 1,
        "matrix_snapshot_date": MATRIX_SNAPSHOT_DATE,
        "matrix_source": MATRIX_SOURCE,
        "disclaimer": DISCLAIMER,
        "repo_root": str(scan.root),
        "strict": bool(strict),
        "citations": CITATIONS,
        "files": [_file_dict(item) for item in scan.files],
        "files_read": list(scan.files_read),
        "diagnostics": diagnostics,
        "summary": {"errors": errors, "warnings": warnings, "infos": infos},
        "exit_code": exit_code,
    }


def _final_file(found: FoundFile, root: Path) -> Path | None:
    status, final = resolve_inside(found.logical, root)
    if status != "ok" or final is None:
        return None
    try:
        listed = os.lstat(final)
    except OSError:
        return None
    if not stat.S_ISREG(listed.st_mode):
        return None
    return final


def _points_at_root(result: ResolvedImport, root_final: Path | None) -> bool:
    if root_final is None or result.status != "ok" or result.final is None:
        return False
    try:
        left = os.lstat(result.final)
        right = os.lstat(root_final)
    except OSError:
        return os.path.abspath(result.final) == os.path.abspath(root_final)
    if not stat.S_ISREG(left.st_mode):
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _import_diagnostics(item: FoundFile, result: ResolvedImport) -> list[dict]:
    target = result.rel if result.rel is not None else result.token
    if result.status == "missing":
        return [
            {
                "id": "broken-at-import",
                "severity": "error",
                "base_severity": "error",
                "strict_upgraded": False,
                "path": item.rel,
                "line": result.line,
                "import_target": target,
                "related_paths": [],
                "message": f"line {result.line}: import target does not exist: {target}",
            }
        ]
    if result.status == "out-of-root":
        return [
            {
                "id": "out-of-root-skip",
                "severity": "info",
                "base_severity": "info",
                "strict_upgraded": False,
                "path": item.rel,
                "line": result.line,
                "import_target": result.token,
                "related_paths": [],
                "message": (
                    f"line {result.line}: @import resolves outside the repo root; "
                    "target not read and not treated as broken-at-import"
                ),
            }
        ]
    if result.status == "cycle":
        return [
            {
                "id": "symlink-cycle",
                "severity": "info",
                "base_severity": "info",
                "strict_upgraded": False,
                "path": item.rel,
                "line": result.line,
                "import_target": result.token,
                "related_paths": [],
                "message": f"line {result.line}: @import symlink cycle; target not read",
            }
        ]
    if result.status == "skipped-skill":
        return [
            {
                "id": "skipped-skill",
                "severity": "info",
                "base_severity": "info",
                "strict_upgraded": False,
                "path": item.rel,
                "line": result.line,
                "import_target": result.token,
                "related_paths": [],
                "message": f"line {result.line}: @import points at a skill path; not read",
            }
        ]
    if result.status == "other":
        return [
            {
                "id": "unreadable-path",
                "severity": "info",
                "base_severity": "info",
                "strict_upgraded": False,
                "path": item.rel,
                "line": result.line,
                "import_target": result.token,
                "related_paths": [],
                "message": f"line {result.line}: @import target could not be resolved",
            }
        ]
    return []


def _operational_dict(item) -> dict:
    return {
        "id": item.id,
        "severity": "info",
        "base_severity": "info",
        "strict_upgraded": False,
        "path": item.path,
        "line": item.line,
        "import_target": item.import_target,
        "related_paths": [],
        "message": item.message,
    }


def _file_dict(item: FoundFile) -> dict:
    rel = item.rel
    kind = item.kind
    runtimes, note = _assumption(kind, rel)
    return {
        "path": rel,
        "kind": kind,
        "exists": True,
        "runtimes": runtimes,
        "note": note,
        "citations": _citation_ids(kind, rel),
    }


def _assumption(kind: str, rel: str) -> tuple[list[str], str]:
    if kind == "agents-md" and rel == "AGENTS.md":
        return (
            [
                "Cursor",
                "GitHub Copilot",
                "Codex",
                "Gemini CLI",
                "Kiro",
                "Devin Desktop",
                "opencode",
                "Claude Code",
            ],
            "Canonical project instructions in the 2026-11-06 AIES matrix. "
            "Claude Code's default loads this file only when no CLAUDE.md, "
            ".claude/CLAUDE.md, or CLAUDE.local.md is on the path; otherwise only via @import. "
            "Direct reads need Claude Code v2.1.277+. Not a live probe.",
        )
    if kind == "agents-md":
        return (
            [
                "Cursor",
                "GitHub Copilot",
                "Codex",
                "Gemini CLI",
                "Kiro",
                "Devin Desktop",
                "opencode",
                "Claude Code",
            ],
            "Nested AGENTS.md is scoped to this directory (AIES; Cursor nested AGENTS.md; "
            "Claude Code loads a subdirectory AGENTS.md on demand when no CLAUDE-side file is in that directory). "
            "Not a live probe.",
        )
    if kind == "claude-md" and rel == "CLAUDE.md":
        return (
            ["Claude Code", "Cursor"],
            "Claude Code project instructions. Cursor Help: root CLAUDE.md is read like AGENTS.md and always applied. "
            "Not a live probe.",
        )
    if kind == "claude-md" and rel.endswith(".claude/CLAUDE.md"):
        return (
            ["Claude Code"],
            "Alternate Claude Code project path (./.claude/CLAUDE.md). "
            "Counted independently for shadow-no-import. Not a live probe.",
        )
    if kind == "claude-md":
        return (
            ["Claude Code"],
            "Claude Code loads subdirectory CLAUDE.md on demand when working in that directory. "
            "v0 still pairs shadow-no-import with root AGENTS.md only. Not a live probe.",
        )
    if kind == "claude-local-md":
        return (
            ["Claude Code"],
            "Claude Code local project instructions. Counts as a CLAUDE-side file in the default "
            "AGENTS.md shadow table. Not a live probe.",
        )
    if kind == "cursor-rule-mdc":
        return (
            ["Cursor"],
            "Cursor project rule (.mdc). v0 does not validate frontmatter or rule schema.",
        )
    if kind == "cursor-rule-md":
        return (
            [],
            "Cursor's rules system ignores plain .md under .cursor/rules/ because the extension is not .mdc. "
            "No runtime is claimed.",
        )
    if kind == "cursorrules":
        return (
            ["Cursor (legacy)"],
            "Cursor Help: root .cursorrules is legacy and will be deprecated. "
            "AIES: legacy files may still be read and silently override. Not a live probe.",
        )
    if kind == "windsurfrules":
        return (
            ["Windsurf (legacy)", "Devin Desktop (legacy)"],
            "AIES pitfall: legacy .windsurfrules may still be read and silently override. "
            "The 2026-11-06 matrix lists Devin Desktop instructions as AGENTS.md plus .devin/rules/*.md. "
            "Not a live probe.",
        )
    if kind == "copilot-instructions":
        return (
            ["GitHub Copilot"],
            "GitHub Copilot repository instructions in the 2026-11-06 AIES matrix, alongside AGENTS.md. "
            "Not a live probe.",
        )
    if kind == "gemini-md":
        return (
            [],
            "In the v0 path table. The 2026-11-06 AIES matrix lists Gemini CLI instructions as "
            "AGENTS.md via context.fileName, not GEMINI.md. No runtime is claimed for this file.",
        )
    if kind == "devin-rule":
        return (
            ["Devin Desktop"],
            "Devin Desktop rules path in the 2026-11-06 AIES matrix (.devin/rules/*.md, one directory level). "
            "Not a live probe.",
        )
    if kind == "kiro-steering":
        return (
            ["Kiro"],
            "Kiro steering path in the 2026-11-06 AIES matrix (.kiro/steering/*.md, one directory level). "
            "Not a live probe.",
        )
    return ([], "No load assumption recorded.")


def _citation_ids(kind: str, rel: str) -> list[str]:
    ids = {"aies-2026-11-06"}
    if kind in {"agents-md", "claude-md", "claude-local-md"}:
        ids.add("claude-memory")
    if kind in {"agents-md", "cursor-rule-mdc", "cursor-rule-md", "cursorrules"}:
        ids.add("cursor-rules")
    if kind == "cursorrules" or (kind == "claude-md" and rel == "CLAUDE.md"):
        ids.add("cursor-help-rules")
    if kind == "agents-md":
        ids.add("agents-md")
    return sorted(ids)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    kept: list[dict] = []
    for item in items:
        key = (
            item["id"],
            item["path"],
            item["line"],
            item["import_target"],
            item["message"],
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept
