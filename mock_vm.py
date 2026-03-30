"""Drop-in mock for PcmRuntimeClientSync — operates on a local folder.

Returns objects compatible with agent.py formatters:
  - tree  → MockTreeResult  (.root.name, .root.children recursive)
  - read  → MockReadResult  (.content)
  - list  → MockListResult  (.entries[].name, .entries[].is_dir)
  - search→ MockSearchResult(.matches[].path, .line, .line_text)
  - find/context/write/delete/mkdir/move/answer → dict (via patched MessageToDict)
"""

import json
import os
import re
import shutil
from pathlib import Path

from connectrpc.errors import ConnectError

# ── Try to find Code enum for ConnectError construction ─────────────────────
try:
    from connectrpc import Code
    _CODE_INVALID = Code.INVALID_ARGUMENT
    _CODE_NOT_FOUND = Code.NOT_FOUND
except ImportError:
    try:
        from connectrpc.errors import Code
        _CODE_INVALID = Code.INVALID_ARGUMENT
        _CODE_NOT_FOUND = Code.NOT_FOUND
    except ImportError:
        # Fallback: use raw ints (gRPC codes)
        _CODE_INVALID = 3
        _CODE_NOT_FOUND = 5


def _raise(code, message):
    raise ConnectError(code=code, message=message)


# ── Mock result objects (match protobuf field access patterns) ──────────────

class MockTreeEntry:
    def __init__(self, name, children=None, is_dir=False):
        self.name = name
        self.children = children or []
        self.is_dir = is_dir


class MockTreeResult:
    def __init__(self, root):
        self.root = root


class MockListEntry:
    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir


class MockListResult:
    def __init__(self, entries):
        self.entries = entries


class MockReadResult:
    def __init__(self, content):
        self.content = content


class MockSearchMatch:
    def __init__(self, path, line, line_text):
        self.path = path
        self.line = line
        self.line_text = line_text


class MockSearchResult:
    def __init__(self, matches):
        self.matches = matches


# ── MockVM ──────────────────────────────────────────────────────────────────

class MockVM:
    """Mock PcmRuntimeClientSync operating on a local directory."""

    def __init__(self, task_dir: str | Path):
        task_dir = Path(task_dir)

        # Copy files/ to a temp working dir so agent can write/delete
        src = task_dir / "files"
        self.workdir = task_dir / "_workdir"
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        shutil.copytree(src, self.workdir)

        # Load context
        ctx_file = task_dir / "context.json"
        if ctx_file.exists():
            self._context = json.loads(ctx_file.read_text(encoding="utf-8"))
        else:
            self._context = {"unixTime": "0", "time": "2026-01-01T00:00:00Z"}

        # Tracking for scoring
        self.outcome = None
        self.answer_message = None
        self.answer_refs = []
        self.written_files = set()   # paths written by agent
        self.deleted_files = set()   # paths deleted by agent

    def _resolve(self, path: str) -> Path:
        """Resolve a VM path to local filesystem path."""
        clean = path.strip().lstrip("/")
        return self.workdir / clean if clean else self.workdir

    def _relpath(self, full: Path) -> str:
        """Get VM-relative path from full local path."""
        return str(full.relative_to(self.workdir)).replace("\\", "/")

    # ── tree ────────────────────────────────────────────────────────────────

    def _build_tree(self, path: Path, depth: int, max_depth: int) -> MockTreeEntry:
        name = path.name or "/"
        is_dir = path.is_dir()
        children = []
        if is_dir and (max_depth == 0 or depth < max_depth):
            for child in sorted(path.iterdir()):
                children.append(self._build_tree(child, depth + 1, max_depth))
        return MockTreeEntry(name, children, is_dir)

    def tree(self, request) -> MockTreeResult:
        root_path = self._resolve(getattr(request, "root", "") or "")
        level = getattr(request, "level", 0) or 0
        if not root_path.exists():
            _raise(_CODE_NOT_FOUND, f"path not found: {root_path}")
        root_entry = self._build_tree(root_path, 0, level)
        # Root of repo gets "/" as name (matching real API)
        if root_path == self.workdir:
            root_entry.name = "/"
        return MockTreeResult(root_entry)

    # ── read ────────────────────────────────────────────────────────────────

    def read(self, request) -> MockReadResult:
        path = self._resolve(request.path)
        if not path.exists():
            _raise(_CODE_NOT_FOUND, f"file not found: {request.path}")
        if path.is_dir():
            _raise(_CODE_INVALID, "path must reference a file")
        content = path.read_text(encoding="utf-8")

        start = getattr(request, "start_line", 0) or 0
        end = getattr(request, "end_line", 0) or 0
        if start > 0 or end > 0:
            lines = content.splitlines(keepends=True)
            s = (start - 1) if start > 0 else 0
            e = end if end > 0 else len(lines)
            content = "".join(lines[s:e])

        return MockReadResult(content)

    # ── write ───────────────────────────────────────────────────────────────

    def write(self, request) -> dict:
        path = self._resolve(request.path)
        path.parent.mkdir(parents=True, exist_ok=True)

        start = getattr(request, "start_line", 0) or 0
        end = getattr(request, "end_line", 0) or 0

        if start > 0 and path.exists():
            # Range write
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = request.content.splitlines(keepends=True)
            s = start - 1
            e = end if end > 0 else len(lines)
            lines[s:e] = new_lines
            path.write_text("".join(lines), encoding="utf-8")
        else:
            # Full overwrite
            path.write_text(request.content, encoding="utf-8")

        self.written_files.add(self._relpath(path))
        return {}

    # ── delete ──────────────────────────────────────────────────────────────

    def delete(self, request) -> dict:
        path = self._resolve(request.path)
        if not path.exists():
            _raise(_CODE_NOT_FOUND, f"not found: {request.path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        self.deleted_files.add(self._relpath(path))
        return {}

    # ── list ────────────────────────────────────────────────────────────────

    def list(self, request) -> MockListResult:
        path_str = getattr(request, "name", None) or getattr(request, "path", "/")
        path = self._resolve(path_str)
        if not path.exists():
            _raise(_CODE_NOT_FOUND, f"not found: {path_str}")
        if not path.is_dir():
            _raise(_CODE_INVALID, "path must reference a directory")
        entries = []
        for child in sorted(path.iterdir()):
            entries.append(MockListEntry(child.name, child.is_dir()))
        return MockListResult(entries)

    # ── search (grep) ───────────────────────────────────────────────────────

    def search(self, request) -> MockSearchResult:
        root = self._resolve(getattr(request, "root", "/") or "/")
        pattern = request.pattern
        limit = getattr(request, "limit", 10) or 10
        matches = []

        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        for fpath in sorted(root.rglob("*")):
            if fpath.is_dir():
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel = self._relpath(fpath)
                    matches.append(MockSearchMatch(rel, i, line))
                    if len(matches) >= limit:
                        return MockSearchResult(matches)

        return MockSearchResult(matches)

    # ── find (by filename) ──────────────────────────────────────────────────

    def find(self, request) -> dict:
        root = self._resolve(getattr(request, "root", "/") or "/")
        name = request.name
        kind = getattr(request, "type", 0)  # 0=all, 1=files, 2=dirs
        limit = getattr(request, "limit", 10) or 10
        entries = []

        for fpath in sorted(root.rglob("*")):
            if kind == 1 and fpath.is_dir():
                continue
            if kind == 2 and not fpath.is_dir():
                continue
            if name.lower() in fpath.name.lower():
                entries.append({
                    "path": self._relpath(fpath),
                    "name": fpath.name,
                    "isDir": fpath.is_dir(),
                })
                if len(entries) >= limit:
                    break

        return {"entries": entries} if entries else {}

    # ── mkdir ───────────────────────────────────────────────────────────────

    def mk_dir(self, request) -> dict:
        path = self._resolve(request.path)
        path.mkdir(parents=True, exist_ok=True)
        return {}

    # ── move ────────────────────────────────────────────────────────────────

    def move(self, request) -> dict:
        src = self._resolve(request.from_name)
        dst = self._resolve(request.to_name)
        if not src.exists():
            _raise(_CODE_NOT_FOUND, f"not found: {request.from_name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {}

    # ── context ─────────────────────────────────────────────────────────────

    def context(self, request) -> dict:
        return self._context

    # ── answer ──────────────────────────────────────────────────────────────

    def answer(self, request) -> dict:
        self.outcome = request.outcome
        self.answer_message = getattr(request, "message", "")
        self.answer_refs = list(getattr(request, "refs", []))
        return {}
