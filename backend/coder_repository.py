"""Worker-local repository inventory, ranged access, and immutable revisions.

This module is intentionally independent of the HyprChat database and SDK. All
derived state is outside the user's workspace; source is never sampled away.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import threading
import time


MARKERS = {"package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Makefile", "CMakeLists.txt"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def inventory_match(value, query):
    """Literal substring search, or a glob against the path/name or basename."""
    value, query = value.casefold(), query.casefold()
    if any(character in query for character in "*?"):
        return fnmatchcase(value, query) or fnmatchcase(value.rsplit("/", 1)[-1], query)
    return query in value


def repository_lock(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(Path(path).resolve()), threading.RLock())


def safe_relative(root: Path, relative: str) -> Path:
    value = Path(relative)
    if not relative or value.is_absolute() or ".." in value.parts or ".git" in value.parts:
        raise ValueError("Expected a project-relative path")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the project")
    return path


def validate_links(root, excludes=()):
    """Preserve ordinary internal links, never copy or package outside files."""
    root = Path(root).resolve()
    links = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in excludes and name != ".git"]
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):
                    raise ValueError(f"Symlink escapes project: {path.relative_to(root)}")
                links.append({"path": path.relative_to(root).as_posix(), "target": os.readlink(path)})
    return links


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symbols(path: Path):
    """Return symbols/imports, with honest fallback to indexed text access."""
    suffix = path.suffix.lower()
    if suffix not in (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs"):
        return [], [], "text"
    try:
        content = path.read_bytes()
        if suffix == ".py":
            tree = ast.parse(content)
            symbols, imports = [], []
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append((node.name, node.lineno, node.end_lineno, type(node).__name__))
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append("." * node.level + (node.module or ""))
            return symbols, imports, "python-ast"
        from tree_sitter import Language, Parser
        if suffix in (".ts", ".tsx"):
            import tree_sitter_typescript as language
            capsule = language.language_tsx() if suffix == ".tsx" else language.language_typescript()
        else:
            import tree_sitter_javascript as language
            capsule = language.language()
        parser = Parser(Language(capsule))
        tree = parser.parse(content)
        nodes, symbols, imports = [tree.root_node], [], []
        while nodes:
            node = nodes.pop()
            if node.type in ("function_declaration", "class_declaration", "interface_declaration", "type_alias_declaration", "method_definition", "variable_declarator"):
                name = node.child_by_field_name("name")
                if name:
                    symbols.append((name.text.decode("utf-8", "replace"), node.start_point.row + 1, node.end_point.row + 1, node.type))
            if node.type in ("import_statement", "export_statement"):
                source = node.child_by_field_name("source")
                if source:
                    imports.append(source.text.decode("utf-8", "replace").strip("\"'"))
            nodes.extend(reversed(node.children))
        return symbols, imports, "tree-sitter" if not tree.root_node.has_error else "tree-sitter-partial"
    except (SyntaxError, UnicodeError, ImportError, ValueError, RecursionError) as error:
        return [], [], f"text:{type(error).__name__}"


class Repository:
    def __init__(self, root, state_dir, excludes=()):
        self.root = Path(root).resolve()
        self.state = Path(state_dir).resolve()
        if self.state.is_relative_to(self.root):
            raise ValueError("Repository state must live outside the project")
        if not self.root.is_dir():
            raise ValueError("Project directory does not exist")
        self.state.mkdir(parents=True, exist_ok=True)
        self.git_dir = self.state / "revisions.git"
        self.excludes = tuple(dict.fromkeys((".git", *excludes)))
        self.lock = repository_lock(self.state)
        self.index_path = self.state / "inventory.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                    sha256 TEXT, lines INTEGER, language TEXT, package TEXT,
                    parser TEXT, binary INTEGER, seen TEXT);
                CREATE TABLE IF NOT EXISTS symbols (
                    path TEXT, name TEXT, line INTEGER, end_line INTEGER, kind TEXT);
                CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path);
                CREATE TABLE IF NOT EXISTS imports (path TEXT, target TEXT);
                CREATE INDEX IF NOT EXISTS imports_target ON imports(target);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.index_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def git(self, *args, data=None):
        env = {**os.environ, "GIT_AUTHOR_NAME": "Daedalus", "GIT_AUTHOR_EMAIL": "daedalus@localhost",
               "GIT_COMMITTER_NAME": "Daedalus", "GIT_COMMITTER_EMAIL": "daedalus@localhost"}
        result = subprocess.run(["git", f"--git-dir={self.git_dir}", f"--work-tree={self.root}", *args],
                                input=data, capture_output=True, env=env)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def _init_git(self):
        if not self.git_dir.exists():
            result = subprocess.run(["git", "init", "--bare", str(self.git_dir)], capture_output=True)
            if result.returncode:
                raise RuntimeError(result.stderr.decode())
        # The private index is scratch state, not the user's Git index. Rebuild
        # it so newly excluded/generated files cannot leak into checkpoints.
        self.git("read-tree", "--empty")
        (self.git_dir / "info" / "exclude").write_text("\n".join(f"{name}/" for name in self.excludes) + "\n")
        # Packaging must reproduce the checkpoint, including files a project's
        # own release export rules would omit or rewrite.
        (self.git_dir / "info" / "attributes").write_text("* -export-ignore -export-subst\n")

    def paths(self):
        """Git's ignore engine also works for uploaded trees without .git."""
        self._init_git()
        proc = subprocess.Popen(["git", f"--git-dir={self.git_dir}", f"--work-tree={self.root}",
                                 "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pending, seen = b"", set()
        try:
            while chunk := proc.stdout.read(64 * 1024):
                parts = (pending + chunk).split(b"\0")
                pending = parts.pop()
                for item in parts:
                    relative = os.fsdecode(item)
                    if relative in seen or any(p in self.excludes for p in Path(relative).parts):
                        continue
                    seen.add(relative)
                    path = self.root / relative
                    if path.is_symlink() or not path.is_file():
                        continue
                    yield relative
            error = proc.stderr.read()
            if proc.wait():
                raise RuntimeError(error.decode("utf-8", "replace"))
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def refresh(self, *, cancelled=None):
        with self.lock, self.connect() as db:
            links = validate_links(self.root, self.excludes)
            scan_id = str(time.time_ns())
            changed, errors = 0, []
            for relative in self.paths():
                if cancelled and cancelled():
                    raise InterruptedError("Inventory cancelled")
                path = safe_relative(self.root, relative)
                try:
                    stat = path.stat()
                    old = db.execute("SELECT * FROM files WHERE path=?", (relative,)).fetchone()
                    # Hash even equal-size changes: checkout timestamps and rapid
                    # edits are not a reliable revision identifier.
                    digest = file_hash(path)
                    if old and old["sha256"] == digest:
                        db.execute("UPDATE files SET seen=?, mtime_ns=? WHERE path=?", (scan_id, stat.st_mtime_ns, relative))
                        continue
                    binary, lines = False, 0
                    with path.open("rb") as source:
                        for line in source:
                            lines += 1
                            binary = binary or b"\0" in line
                    package = "."
                    for parent in (path.parent, *path.parents):
                        if not parent.is_relative_to(self.root):
                            break
                        if any((parent / marker).is_file() for marker in MARKERS):
                            package = parent.relative_to(self.root).as_posix()
                            break
                    symbols, imports, parser = ([], [], "binary") if binary else _symbols(path)
                    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (relative, stat.st_size, stat.st_mtime_ns, digest, lines, path.suffix, package, parser, int(binary), scan_id))
                    db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                    db.execute("DELETE FROM imports WHERE path=?", (relative,))
                    db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?)", [(relative, *symbol) for symbol in symbols])
                    db.executemany("INSERT INTO imports VALUES(?,?)", [(relative, target) for target in imports])
                    changed += 1
                except (OSError, ValueError) as error:
                    errors.append({"path": relative, "error": str(error)})
            removed = [r[0] for r in db.execute("SELECT path FROM files WHERE seen != ?", (scan_id,))]
            db.executemany("DELETE FROM symbols WHERE path=?", [(p,) for p in removed])
            db.executemany("DELETE FROM imports WHERE path=?", [(p,) for p in removed])
            db.execute("DELETE FROM files WHERE seen != ?", (scan_id,))
            stats = dict(db.execute("SELECT COUNT(*) files, COALESCE(SUM(lines),0) lines, COALESCE(SUM(size),0) bytes FROM files").fetchone())
            stats.update(changed=changed, removed=len(removed), errors=errors, symlinks=links, excluded_directories=list(self.excludes), scan_id=scan_id)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('inventory',?)", (json.dumps(stats),))
            return stats

    def inventory(self, *, cursor="", limit=100, query="", symbols=False):
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        with self.connect() as db:
            db.create_function("inventory_match", 2, inventory_match, deterministic=True)
            if symbols:
                rows = db.execute("SELECT *, path || ':' || line || ':' || name cursor FROM symbols WHERE inventory_match(name, ?) AND path || ':' || line || ':' || name > ? ORDER BY cursor LIMIT ?",
                                  (query, cursor, limit + 1)).fetchall()
            else:
                rows = db.execute("SELECT *, path cursor FROM files WHERE path > ? AND inventory_match(path, ?) ORDER BY path LIMIT ?",
                                  (cursor, query, limit + 1)).fetchall()
            more = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            meta = db.execute("SELECT value FROM metadata WHERE key='inventory'").fetchone()
            return {"items": items, "next_cursor": items[-1]["cursor"] if more else None,
                    "stats": json.loads(meta[0]) if meta else {}, "truncated": more}

    def read(self, relative, *, start=1, limit=200, expected_hash=""):
        if start < 1 or limit < 1:
            raise ValueError("start and limit must be positive")
        path = safe_relative(self.root, relative)
        digest = file_hash(path)
        if expected_hash and expected_hash != digest:
            raise ValueError("Source changed; retrieve the current revision")
        output, following = [], None
        with path.open(encoding="utf-8", errors="replace") as source:
            for number, line in enumerate(source, 1):
                if number < start:
                    continue
                if len(output) == limit:
                    following = number
                    break
                output.append(line)
        return {"path": relative, "sha256": digest, "start": start, "content": "".join(output),
                "next_line": following, "truncated": following is not None}

    def read_bytes(self, relative, *, offset=0, length=65536, expected_hash=""):
        """Byte pagination keeps even generated single-line files navigable."""
        if offset < 0 or length < 1:
            raise ValueError("offset must be nonnegative and length positive")
        path = safe_relative(self.root, relative)
        digest = file_hash(path)
        if expected_hash and digest != expected_hash:
            raise ValueError("Source changed; retrieve the current revision")
        with path.open("rb") as source:
            source.seek(offset)
            content = source.read(length)
        following = offset + len(content)
        return {"path": relative, "sha256": digest, "offset": offset,
                "content": content.decode("utf-8", "replace"),
                "next_offset": following if following < path.stat().st_size else None,
                "truncated": following < path.stat().st_size}

    def dependencies(self, relative="", *, query="", cursor=0, limit=100):
        if cursor < 0 or limit < 1:
            raise ValueError("Invalid dependency page")
        with self.connect() as db:
            rows = db.execute("SELECT rowid cursor,path,target FROM imports WHERE rowid>? AND path LIKE ? AND target LIKE ? ORDER BY rowid LIMIT ?",
                              (cursor, relative or "%", f"%{query}%", limit + 1)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["cursor"] if len(rows)>limit else None,
                    "truncated": len(rows)>limit, "kind": "declared imports; resolution may require package manifests"}

    def search(self, query, *, cursor="", limit=100):
        """Exact text search over all catalogued source; paged by path/line."""
        if not query or limit < 1:
            raise ValueError("query and positive limit are required")
        result = []
        with self.connect() as db:
            for row in db.execute("SELECT path,sha256 FROM files WHERE binary=0 ORDER BY path"):
                try:
                    with safe_relative(self.root, row["path"]).open(encoding="utf-8", errors="replace") as source:
                        for number, line in enumerate(source, 1):
                            key = f"{row['path']}\0{number:012d}"
                            if key <= cursor or query not in line:
                                continue
                            if len(result) == limit:
                                return {"items": result, "next_cursor": result[-1]["cursor"], "truncated": True}
                            result.append({"path": row["path"], "line": number, "text": line.rstrip(), "sha256": row["sha256"], "cursor": key})
                except FileNotFoundError:
                    continue
        return {"items": result, "next_cursor": None, "truncated": False}

    def snapshot(self, message="Checkpoint", *, parent=""):
        with self.lock:
            self._init_git()
            validate_links(self.root, self.excludes)
            self.git("add", "-A", "--", ".")
            tree = self.git("write-tree").decode().strip()
            if parent and re.fullmatch(r"[a-f0-9]{40,64}", parent):
                if self.git("rev-parse", parent + "^{tree}").decode().strip() == tree:
                    return {"revision": parent, "tree": tree, "unchanged": True}
            args = ["commit-tree", tree]
            if parent:
                if not re.fullmatch(r"[a-f0-9]{40,64}", parent):
                    raise ValueError("Invalid parent revision")
                args += ["-p", parent]
            revision = self.git(*args, data=message.encode()).decode().strip()
            self.git("update-ref", "refs/heads/checkpoints", revision)
            return {"revision": revision, "tree": tree, "parent": parent}

    def archive(self, revision, destination):
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ValueError("Invalid revision")
        if not self.git("ls-tree", revision):
            # Git emits a lone PAX global header for an empty commit. Python's
            # tar reader rejects that header without a following member.
            with tarfile.open(destination, "w:gz"):
                pass
        else:
            result = subprocess.run(["git", f"--git-dir={self.git_dir}", "archive", "--format=tar.gz", "-o", str(destination), revision], capture_output=True)
            if result.returncode:
                raise RuntimeError(result.stderr.decode())
        return {"sha256": file_hash(destination), "size": Path(destination).stat().st_size}

    def diff(self, before, after):
        if any(not re.fullmatch(r"[a-f0-9]{40,64}", value) for value in (before, after)):
            raise ValueError("Invalid revision")
        return self.git("diff", "--stat", before, after).decode("utf-8", "replace")
