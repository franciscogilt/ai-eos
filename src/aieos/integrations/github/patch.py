"""Validation of agent patches before anything is pushed.

A patch is a `git diff` produced in a sandbox, so it is untrusted. This parser reads only the
diff headers and decides which files it touches; nothing here applies the patch. It rejects,
with a reason the agent can act on:

* binary content (NUL bytes or invalid UTF-8; exported diffs use --text, so binary files would
  otherwise appear as text),
* .gitattributes, .gitmodules, anything under .git/ or .github/workflows/
  (and the App has no Workflows permission, so GitHub would refuse those as well),
* symlinks and submodules (modes 120000 / 160000), and any mode other than 100644 / 100755,
* paths that are absolute, contain `..`, or use characters outside a conservative set
  (git would quote them, and quoted paths are not accepted),
* more than MAX_FILES files or MAX_PATCH_BYTES bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MAX_PATCH_BYTES = 5 * 1024 * 1024
MAX_FILES = 200
ALLOWED_MODES = frozenset({"100644", "100755"})
_PATH = re.compile(r"^[A-Za-z0-9._@+][A-Za-z0-9._@+/-]{0,399}$")  # no leading "-": never an option
_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")


class PatchRejectedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: Literal["add", "modify", "delete"]
    mode: str | None = None  # new mode when the patch sets one (add, or a mode change)


def check_path(path: str) -> str:
    if not _PATH.match(path) or path.endswith("/"):
        raise PatchRejectedError(f"unsupported path {path!r}: use letters, digits and ._@+-/ only")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PatchRejectedError(f"path must be normalised and relative: {path!r}")
    lowered = [p.lower() for p in parts]
    if ".git" in lowered:
        raise PatchRejectedError(f"changes inside .git are not allowed: {path!r}")
    if lowered[-1] in (".gitattributes", ".gitmodules"):
        raise PatchRejectedError(f"changing {parts[-1]} is not allowed")
    if lowered[:2] == [".github", "workflows"]:
        raise PatchRejectedError("changing GitHub Actions workflows is not allowed")
    return path


def _mode(value: str, path: str) -> str:
    if value not in ALLOWED_MODES:
        kind = {"120000": "symlink", "160000": "submodule"}.get(value, f"mode {value}")
        raise PatchRejectedError(f"{path}: {kind} is not allowed")
    return value


@dataclass
class _Section:
    a: str
    b: str
    new_mode: str | None = None
    deleted: bool = False
    added: bool = False
    rename_from: str | None = None
    rename_to: str | None = None
    copy: bool = False


def _changes(s: _Section) -> list[FileChange]:
    if s.copy:
        raise PatchRejectedError(f"{s.b}: copies are not supported")
    if s.rename_from or s.rename_to:
        old, new = check_path(s.rename_from or s.a), check_path(s.rename_to or s.b)
        return [FileChange(old, "delete"), FileChange(new, "add", s.new_mode)]
    if s.a != s.b:
        raise PatchRejectedError(f"inconsistent header for {s.a!r} / {s.b!r}")
    path = check_path(s.b)
    if s.added:
        return [FileChange(path, "add", s.new_mode or "100644")]
    if s.deleted:
        return [FileChange(path, "delete")]
    return [FileChange(path, "modify", s.new_mode)]


def validate_patch(patch: bytes) -> list[FileChange]:
    """Return the files the patch changes, or raise PatchRejectedError."""
    if not patch.strip():
        raise PatchRejectedError("the patch is empty")
    if len(patch) > MAX_PATCH_BYTES:
        raise PatchRejectedError(f"the patch is larger than {MAX_PATCH_BYTES} bytes")
    if b"\x00" in patch:
        raise PatchRejectedError("binary content is not allowed")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchRejectedError("binary content is not allowed (patch is not valid UTF-8)") from None

    sections: list[_Section] = []
    current: _Section | None = None
    in_hunk = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = _DIFF_HEADER.match(line)
            if not m:
                raise PatchRejectedError(f"unsupported diff header: {line[:200]!r}")
            current = _Section(a=m.group(1), b=m.group(2))
            sections.append(current)
            in_hunk = False
            continue
        if current is None:
            if line.strip():
                raise PatchRejectedError("content before the first diff header")
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            if line[:1] in (" ", "+", "-", "\\") or line == "":
                continue
            raise PatchRejectedError(f"unexpected line in a hunk: {line[:200]!r}")
        if line.startswith(("GIT binary patch", "Binary files ")):
            raise PatchRejectedError(f"{current.b}: binary files are not allowed")
        if line.startswith("new file mode "):
            current.added, current.new_mode = True, _mode(line[14:].strip(), current.b)
        elif line.startswith("deleted file mode "):
            current.deleted = True
            _mode(line[18:].strip(), current.a)
        elif line.startswith("new mode "):
            current.new_mode = _mode(line[9:].strip(), current.b)
        elif line.startswith("old mode "):
            _mode(line[9:].strip(), current.a)
        elif line.startswith("rename from "):
            current.rename_from = line[12:]
        elif line.startswith("rename to "):
            current.rename_to = line[10:]
        elif line.startswith(("copy from ", "copy to ")):
            current.copy = True
        elif line.startswith("index "):
            parts = line.split()
            if len(parts) == 3:
                _mode(parts[2], current.b)
        elif line.startswith(("--- ", "+++ ", "similarity index ", "dissimilarity index ")):
            pass
        elif line.strip():
            raise PatchRejectedError(f"unexpected header line: {line[:200]!r}")

    changes = [c for s in sections for c in _changes(s)]
    if not changes:
        raise PatchRejectedError("the patch changes no files")
    if len(changes) > MAX_FILES:
        raise PatchRejectedError(f"the patch changes more than {MAX_FILES} files")
    paths = [c.path for c in changes]
    if len(set(paths)) != len(paths):
        raise PatchRejectedError("the patch changes the same path twice")
    return changes


def net_changes(patches: list[list[FileChange]]) -> list[FileChange]:
    """Combine sequential patches (each made on top of the previous ones) into the net change against
    the first patch's base. A file added then deleted disappears; deleted then re-added is a modify."""
    state: dict[str, FileChange] = {}
    for changes in patches:
        for c in changes:
            prev = state.get(c.path)
            if c.kind == "add":
                if prev is not None and prev.kind != "delete":
                    raise PatchRejectedError(f"{c.path}: added by a later patch but it already exists")
                state[c.path] = FileChange(c.path, "modify" if prev is not None else "add", c.mode or "100644")
            elif c.kind == "modify":
                if prev is not None and prev.kind == "delete":
                    raise PatchRejectedError(f"{c.path}: modified after an earlier patch deleted it")
                mode = c.mode or (prev.mode if prev else None)
                state[c.path] = FileChange(c.path, "add" if prev is not None and prev.kind == "add" else "modify", mode)
            else:
                if prev is not None and prev.kind == "delete":
                    raise PatchRejectedError(f"{c.path}: deleted twice")
                if prev is not None and prev.kind == "add":
                    del state[c.path]
                else:
                    state[c.path] = FileChange(c.path, "delete")
    if len(state) > MAX_FILES:
        raise PatchRejectedError(f"the combined change touches more than {MAX_FILES} files")
    return [state[p] for p in sorted(state)]
