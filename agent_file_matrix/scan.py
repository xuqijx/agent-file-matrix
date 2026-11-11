"""Read-only discovery of v0 known agent instruction paths.

Symlinks that resolve outside the repo root are not followed and their
targets are not read. Cycles terminate. Directories named ``skills`` and
files named ``SKILL.md`` are skipped without being opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from agent_file_matrix.imports import ImportToken

MAX_LINK_HOPS = 32
MAX_READ_BYTES = 8 * 1024 * 1024
_INSTRUCTION_KINDS = frozenset({"agents-md", "claude-md", "claude-local-md"})


@dataclass
class FoundFile:
    rel: str
    kind: str
    logical: Path
    text: str | None = None
    truncated: bool = False


@dataclass
class Operational:
    id: str
    path: str
    message: str
    import_target: str | None = None
    line: int | None = None


@dataclass
class Scan:
    root: Path
    files: list[FoundFile] = field(default_factory=list)
    operational: list[Operational] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)


def scan_repository(root: Path) -> Scan:
    root = Path(os.path.abspath(root))
    scan = Scan(root=root)
    seen: set[tuple[int, int]] = set()
    visits = 0

    def walk(logical: Path) -> None:
        nonlocal visits
        visits += 1
        if visits > 100_000:
            scan.operational.append(
                Operational(
                    id="traversal-limit",
                    path=_rel(root, logical),
                    message="stopped after 100000 directories (safety limit)",
                )
            )
            return
        try:
            listed = os.lstat(logical)
        except OSError:
            scan.operational.append(
                Operational(
                    id="unreadable-path",
                    path=_rel(root, logical),
                    message="path could not be stat'ed; skipped",
                )
            )
            return
        if stat.S_ISLNK(listed.st_mode):
            status, final = resolve_inside(logical, root)
            if status != "ok" or final is None:
                _record_link_status(scan, root, logical, status)
                return
            try:
                listed = os.lstat(final)
            except OSError:
                scan.operational.append(
                    Operational(
                        id="unreadable-path",
                        path=_rel(root, logical),
                        message="symlink target inside the repo could not be stat'ed; skipped",
                    )
                )
                return
        if not stat.S_ISDIR(listed.st_mode):
            return
        inode = (listed.st_dev, listed.st_ino)
        if inode in seen:
            scan.operational.append(
                Operational(
                    id="symlink-cycle",
                    path=_rel(root, logical),
                    message="symlink cycle; directory not entered again",
                )
            )
            return
        seen.add(inode)
        try:
            entries = list(os.scandir(logical))
        except OSError:
            scan.operational.append(
                Operational(
                    id="unreadable-path",
                    path=_rel(root, logical),
                    message="directory is not readable; not entered",
                )
            )
            return
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            child = Path(entry.path)
            if name == ".git":
                continue
            if name in {"skills", "SKILL.md"}:
                scan.operational.append(
                    Operational(
                        id="skipped-skill",
                        path=_rel(root, child),
                        message="skill path skipped; contents not read",
                    )
                )
                continue
            try:
                child_stat = os.lstat(child)
            except OSError:
                scan.operational.append(
                    Operational(
                        id="unreadable-path",
                        path=_rel(root, child),
                        message="path could not be stat'ed; skipped",
                    )
                )
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                walk(child)
                continue
            if stat.S_ISLNK(child_stat.st_mode):
                status, final = resolve_inside(child, root)
                if status != "ok" or final is None:
                    _record_link_status(scan, root, child, status)
                    continue
                try:
                    final_stat = os.lstat(final)
                except OSError:
                    scan.operational.append(
                        Operational(
                            id="unreadable-path",
                            path=_rel(root, child),
                            message="symlink target inside the repo could not be stat'ed; skipped",
                        )
                    )
                    continue
                if stat.S_ISDIR(final_stat.st_mode):
                    walk(child)
                    continue
                if not stat.S_ISREG(final_stat.st_mode):
                    continue
            elif not stat.S_ISREG(child_stat.st_mode):
                continue
            _consider_file(scan, root, child)

    walk(root)
    scan.files.sort(key=lambda item: item.rel)
    scan.files_read.sort()
    return scan


def _consider_file(scan: Scan, root: Path, logical: Path) -> None:
    rel = _rel(root, logical)
    if rel in {"", "."}:
        return
    kind = classify_relative(rel)
    if kind is None:
        return
    if _looks_like_skill(rel):
        scan.operational.append(
            Operational(
                id="skipped-skill",
                path=rel,
                message="skill path skipped; contents not read",
            )
        )
        return
    found = FoundFile(rel=rel, kind=kind, logical=logical)
    if kind in _INSTRUCTION_KINDS:
        status, final = resolve_inside(logical, root)
        if status != "ok" or final is None:
            _record_link_status(scan, root, logical, status)
            return
        if _path_is_skill(final, root):
            scan.operational.append(
                Operational(
                    id="skipped-skill",
                    path=rel,
                    message="skill path skipped; contents not read",
                )
            )
            return
        try:
            text, truncated = read_instruction_file(final)
        except OSError:
            scan.operational.append(
                Operational(
                    id="unreadable-path",
                    path=rel,
                    message="instruction file could not be read; imports not parsed",
                )
            )
            return
        found.text = text
        found.truncated = truncated
        scan.files_read.append(rel)
        if truncated:
            scan.operational.append(
                Operational(
                    id="read-truncated",
                    path=rel,
                    message=f"read stopped at {MAX_READ_BYTES} bytes; later @imports were not parsed",
                )
            )
    scan.files.append(found)


def read_instruction_file(path: Path) -> tuple[str, bool]:
    """Read an instruction file that has already been resolved inside the repo."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        remaining = MAX_READ_BYTES + 1
        while remaining > 0:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    truncated = len(raw) > MAX_READ_BYTES
    if truncated:
        raw = raw[:MAX_READ_BYTES]
    return raw.decode("utf-8", errors="replace"), truncated


def classify_relative(rel: str) -> str | None:
    pure = Path(rel)
    name = pure.name
    parts = pure.parts
    if name == "AGENTS.md":
        return "agents-md"
    if name == "CLAUDE.local.md":
        return "claude-local-md"
    if name == "CLAUDE.md":
        return "claude-md"
    if name == ".cursorrules":
        return "cursorrules"
    if name == ".windsurfrules":
        return "windsurfrules"
    if name == "GEMINI.md":
        return "gemini-md"
    if len(parts) >= 2 and parts[-2] == ".github" and name == "copilot-instructions.md":
        return "copilot-instructions"
    if _under_dir(parts, ".cursor", "rules"):
        if name.endswith(".mdc"):
            return "cursor-rule-mdc"
        if name.endswith(".md"):
            return "cursor-rule-md"
    if _direct_child(parts, ".devin", "rules") and name.endswith(".md"):
        return "devin-rule"
    if _direct_child(parts, ".kiro", "steering") and name.endswith(".md"):
        return "kiro-steering"
    return None


def _under_dir(parts: tuple[str, ...], parent: str, child: str) -> bool:
    for index, part in enumerate(parts):
        if part == parent and index + 1 < len(parts) and parts[index + 1] == child:
            return index + 2 < len(parts)
    return False


def _direct_child(parts: tuple[str, ...], grand: str, parent: str) -> bool:
    return len(parts) >= 3 and parts[-3] == grand and parts[-2] == parent


def resolve_inside(path: Path, root: Path) -> tuple[str, Path | None]:
    """Resolve a path without stat'ing anything outside ``root``.

    Returns ``(status, final)`` where status is one of
    ``ok``, ``missing``, ``out-of-root``, ``cycle``, ``other``.
    ``final`` is set only for ``ok`` and is not a symlink.
    """
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "out-of-root", None
    parts = list(relative.parts)
    current = root
    index = 0
    hops = 0
    seen_links: set[tuple[int, int]] = set()
    while index < len(parts):
        part = parts[index]
        if part in {"", "."}:
            index += 1
            continue
        if part == "..":
            if current == root:
                return "out-of-root", None
            current = current.parent
            index += 1
            continue
        nxt = current / part
        if not _is_within(nxt, root):
            return "out-of-root", None
        try:
            listed = os.lstat(nxt)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "other", None
        if stat.S_ISLNK(listed.st_mode):
            ident = (listed.st_dev, listed.st_ino)
            if ident in seen_links or hops >= MAX_LINK_HOPS:
                return "cycle", None
            seen_links.add(ident)
            hops += 1
            raw = os.readlink(nxt)
            if os.path.isabs(raw):
                target = Path(raw)
            else:
                target = Path(os.path.abspath(nxt.parent / raw))
            if not _is_within(target, root):
                return "out-of-root", None
            try:
                target_rel = Path(os.path.abspath(target)).relative_to(root)
            except ValueError:
                return "out-of-root", None
            parts = list(target_rel.parts) + parts[index + 1 :]
            current = root
            index = 0
            continue
        current = nxt
        index += 1
    return "ok", current


@dataclass(frozen=True)
class ResolvedImport:
    token: str
    line: int
    status: str
    rel: str | None
    final: Path | None


def resolve_import(file_logical: Path, token: ImportToken, root: Path) -> ResolvedImport:
    """Resolve one ``@`` import. Contract A.

    A token is an ordinary in-repo import when its normalized absolute path
    stays inside ``root``. That includes a literal absolute ``@/path`` and a
    ``~/...`` token whose home expansion still lands inside ``root``. Either
    one can clear ``shadow-no-import`` when it resolves to root ``AGENTS.md``.

    ``out-of-root`` is only an actual escape: an absolute path outside
    ``root``, a ``~/...`` expansion outside ``root``, or a relative path that
    climbs past ``root``. Those targets are not stat'd for a read and are not
    opened.
    """
    raw = token.token
    expanded = raw
    if raw.startswith("~"):
        expanded = os.path.expanduser(raw)
    if expanded.startswith("~") or os.path.isabs(expanded):
        target = Path(os.path.abspath(expanded))
    else:
        target = Path(os.path.abspath(file_logical.parent / expanded))
    if not _is_within(target, root):
        return ResolvedImport(raw, token.line, "out-of-root", None, None)
    try:
        rel = Path(os.path.abspath(target)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return ResolvedImport(raw, token.line, "out-of-root", None, None)
    if _looks_like_skill(rel):
        return ResolvedImport(raw, token.line, "skipped-skill", rel, None)
    status, final = resolve_inside(target, root)
    if status == "ok" and final is not None and _path_is_skill(final, root):
        return ResolvedImport(raw, token.line, "skipped-skill", rel, None)
    return ResolvedImport(raw, token.line, status, rel, final if status == "ok" else None)


def _record_link_status(scan: Scan, root: Path, logical: Path, status: str) -> None:
    rel = _rel(root, logical)
    if status == "out-of-root":
        scan.operational.append(
            Operational(
                id="out-of-root-skip",
                path=rel,
                message="symlink points outside the repo root; not followed and not read",
            )
        )
        return
    if status == "cycle":
        scan.operational.append(
            Operational(
                id="symlink-cycle",
                path=rel,
                message="symlink cycle; not followed",
            )
        )
        return
    if status == "missing":
        return
    scan.operational.append(
        Operational(
            id="unreadable-path",
            path=rel,
            message="path could not be resolved inside the repo; skipped",
        )
    )


def _looks_like_skill(rel: str) -> bool:
    parts = Path(rel).parts
    return "skills" in parts or Path(rel).name == "SKILL.md"


def _path_is_skill(path: Path, root: Path) -> bool:
    try:
        rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return False
    return _looks_like_skill(rel)


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _rel(root: Path, path: Path) -> str:
    try:
        rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return Path(path).name
    text = rel.as_posix()
    return "." if text == "" else text
