"""
Project Indexer agent — Coder Bot v2 Phase 4.2.

When a user uploads an existing project (.zip / .tar.gz / etc.), the upload
endpoint extracts it onto Codebox at /root/projects/{project_id}. After
that, this agent runs ONCE to:

  1. Detect the project's language + build system from on-disk markers
     (reusing the Reviewer's `_detect_project` helper for consistency).
  2. Walk the source tree and pull a capped sample of file contents
     (excluding build artifacts, node_modules, .git, etc.).
  3. Push those file contents into ChromaDB's code_memory collection so
     subsequent ask_project / generate_code calls have semantic retrieval
     over the uploaded code.
  4. Persist a `runs` row with role='indexer' so the frontend renders a
     card showing what was indexed.

Stateless. Single-shot. Does NOT modify the project. Does NOT call any LLM
— uses ChromaDB embeddings only.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid

import config
import database as db


# Same exclusion list used by the Reviewer's tree walk. Keeps build artifacts,
# vendored deps, and VCS metadata out of the index.
_EXCLUDE_DIRS = (
    "node_modules", ".git", "target", "build", "dist", "out",
    "__pycache__", "venv", ".venv", ".cache", ".npm", ".gradle",
    ".idea", ".vscode", ".mvn", ".pytest_cache", ".tox",
)
# Only index source-like files. Binary blobs and generated assets get skipped.
_SOURCE_EXTS = (
    ".py", ".java", ".kt", ".scala", ".groovy",
    ".rs", ".go", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    ".rb", ".php", ".swift", ".m", ".mm",
    ".cs", ".vb", ".fs",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".sql", ".graphql", ".proto",
    ".toml", ".yaml", ".yml", ".json", ".xml", ".ini", ".env.example",
    ".md", ".rst", ".txt",
    ".dockerfile", ".gitignore",
)
# Build markers map → (build_system_label, build_cmd, test_cmd, language).
# Same shape as reviewer's _PROJECT_MARKERS but reused here for first-pass
# language detection. The Reviewer is the authoritative source if/when the
# project actually goes through review later.
_BUILD_MARKERS = [
    ("pom.xml",          "maven",     "mvn -q -DskipTests compile",       "mvn -q test", "java"),
    ("build.gradle",     "gradle",    "./gradlew build -q -x test",        "./gradlew test -q", "java"),
    ("build.gradle.kts", "gradle",    "./gradlew build -q -x test",        "./gradlew test -q", "kotlin"),
    ("Cargo.toml",       "cargo",     "cargo build --quiet",               "cargo test --quiet", "rust"),
    ("go.mod",           "go",        "go build ./...",                     "go test ./...", "go"),
    ("package.json",     "npm",       "npm install --silent && npm run build --if-present",
                                                                            "npm test --if-present", "javascript"),
    ("pyproject.toml",   "pyproject", "python3 -m pip install -q -e . 2>/dev/null || true",
                                                                            "python3 -m pytest -q --no-header 2>/dev/null || true", "python"),
    ("requirements.txt", "pip",       "python3 -m pip install -q -r requirements.txt 2>/dev/null || true",
                                                                            "python3 -m pytest -q --no-header 2>/dev/null || true", "python"),
    ("Makefile",         "make",      "make -s 2>&1 | head -200",          "make -s test 2>&1 | head -200", ""),
    ("CMakeLists.txt",   "cmake",     "cmake -B build -S . && cmake --build build --quiet",
                                                                            "cd build && ctest --output-on-failure", "cpp"),
]


async def _detect_build_marker(http, project_dir: str) -> dict:
    """Look at top-level files to pick a build system. Returns dict with
    {build_system, build_cmd, test_cmd, language, marker} or empty defaults
    when no marker matches."""
    listing = ""
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"ls -1 {shlex.quote(project_dir)}", "timeout": 5},
            timeout=10,
        )
        if r.status_code == 200:
            listing = (r.json().get("stdout") or "")
    except Exception as e:
        print(f"[INDEXER] ls failed for {project_dir}: {e}")

    files = set(line.strip() for line in listing.splitlines() if line.strip())
    for marker, bs, build, test, lang in _BUILD_MARKERS:
        if marker in files:
            return {"marker": marker, "build_system": bs,
                    "build_cmd": build, "test_cmd": test, "language": lang}
    # Fallback: count source files by extension to guess language.
    return {"marker": "(none)", "build_system": "none",
            "build_cmd": "", "test_cmd": "", "language": ""}


async def _list_source_files(http, project_dir: str, cap: int = 100) -> list[str]:
    """Walk the project tree, return up to `cap` source files. Excludes
    build artifacts and vendored deps."""
    excludes = " ".join(f"-not -path '*/{d}/*'" for d in _EXCLUDE_DIRS)
    name_filter = " -o ".join(f"-name '*{ext}'" for ext in _SOURCE_EXTS)
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"find . -type f \\( {name_filter} \\) {excludes} "
        f"-printf '%s %p\\n' 2>/dev/null "
        f"| sort -n | head -{cap}"
    )
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 30},
            timeout=35,
        )
        if r.status_code != 200:
            return []
        stdout = (r.json().get("stdout") or "")
    except Exception as e:
        print(f"[INDEXER] file walk failed: {e}")
        return []

    paths: list[str] = []
    for line in stdout.splitlines():
        # `find -printf '%s %p\n'` → "<size> <path>"
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            size = int(parts[0])
        except ValueError:
            continue
        path = parts[1]
        # Skip empty files and very large ones (likely generated/binary).
        if size < 8 or size > 200_000:
            continue
        paths.append(path)
    return paths


async def _read_file(http, project_dir: str, rel_path: str) -> str:
    """Read a file from Codebox. Returns empty string on failure."""
    full = f"{project_dir.rstrip('/')}/{rel_path.lstrip('./')}" if not rel_path.startswith("/") else rel_path
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": f"cat {shlex.quote(full)}", "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            return (r.json().get("stdout") or "")[:60000]
    except Exception as e:
        print(f"[INDEXER] read failed for {rel_path}: {e}")
    return ""


async def run_project_indexer(http, events, conv_id: str, *,
                                project_id: str, project_dir: str,
                                project_name: str = "",
                                file_cap: int = 100) -> dict:
    """Index an uploaded project into ChromaDB code_memory. Persists a
    `runs` row with role='indexer' for the run-card timeline.

    Returns an envelope with detected language/build_system, file count
    indexed, and chunk count.
    """
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        await db.create_run(run_id, conv_id, role="indexer",
                            project_id=project_id, status="running")
    except Exception as e:
        print(f"[INDEXER] create_run failed (non-fatal): {e}")
        run_id = ""

    _step_n = [0]

    async def _step(action: str, detail: str = ""):
        _step_n[0] += 1
        await events.emit(conv_id, "tool_progress", {
            "tool": "project_indexer", "icon": "database",
            "status": f"📚 {action}{': ' + detail[:100] if detail else ''}",
            "run_id": run_id, "step": _step_n[0],
        })
        if run_id:
            try:
                await db.append_run_event(run_id, {
                    "type": "step", "step": _step_n[0],
                    "action": action, "detail": detail[:300],
                })
            except Exception:
                pass

    await _step("start", project_dir)

    # 1. Detect language / build system.
    detect = await _detect_build_marker(http, project_dir)
    marker = detect.get("marker", "(none)")
    build_system = detect.get("build_system", "none")
    language = detect.get("language", "")
    await _step("detected", f"build_system={build_system} marker={marker} lang={language or 'unknown'}")

    # 2. Walk source tree.
    paths = await _list_source_files(http, project_dir, cap=file_cap)
    if not paths:
        envelope = {
            "status": "error",
            "summary": f"No source files found at {project_dir} (or directory not accessible)",
            "project_dir": project_dir, "project_id": project_id,
            "language": language, "build_system": build_system, "marker": marker,
            "files_indexed": [], "indexed_chunks": 0,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "project_indexer", "icon": "database",
            "status": f"⚠ No source files at {project_dir}",
            "run_id": run_id,
        })
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
        return envelope

    await _step("found_files", f"{len(paths)} source file(s) (capped at {file_cap})")

    # 3. Read files in parallel batches (10 concurrent to avoid flooding Codebox).
    _READ_CONCURRENCY = 10
    _sema = asyncio.Semaphore(_READ_CONCURRENCY)
    file_contents: dict[str, str] = {}

    async def _read_one(rel: str) -> tuple[str, str]:
        async with _sema:
            body = await _read_file(http, project_dir, rel)
            return rel, body

    for batch_start in range(0, len(paths), _READ_CONCURRENCY):
        batch = paths[batch_start:batch_start + _READ_CONCURRENCY]
        if batch_start > 0:
            await _step("reading", f"{batch_start}/{len(paths)}")
        results = await asyncio.gather(
            *[_read_one(rel) for rel in batch],
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, tuple):
                rel, body = res
                if body and len(body.strip()) >= 20:
                    file_contents[rel] = body
    await _step("read_done", f"{len(file_contents)} file(s) with substantive content")

    if not file_contents:
        envelope = {
            "status": "error",
            "summary": "Files were found but none had substantive content to index",
            "project_dir": project_dir, "project_id": project_id,
            "language": language, "build_system": build_system, "marker": marker,
            "files_indexed": [], "indexed_chunks": 0,
        }
        await events.emit(conv_id, "tool_end", {
            "tool": "project_indexer", "icon": "database",
            "status": "⚠ Files empty — nothing to index",
            "run_id": run_id,
        })
        if run_id:
            try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
            except Exception: pass
        return envelope

    # 4. Call existing rag.index_generated_code — same pipeline as built
    # projects, just sourced from an upload.
    await _step("indexing", f"chunking + embedding {len(file_contents)} file(s)")
    try:
        import rag
        index_result = await rag.index_generated_code(
            task=f"Uploaded project: {project_name or project_id}",
            language=language or "unknown",
            file_contents=file_contents,
            conv_id=conv_id,
            project_id=project_id,
        )
    except Exception as e:
        print(f"[INDEXER] index_generated_code failed: {e}")
        index_result = {"indexed": False, "reason": f"{type(e).__name__}: {e}"}

    indexed_ok = bool(index_result.get("indexed"))
    chunks = int(index_result.get("chunks", 0) or 0)
    await _step("done", f"{chunks} chunk(s) indexed" if indexed_ok else f"indexing failed: {index_result.get('reason','?')}")

    envelope = {
        "status": "succeeded" if indexed_ok else "failed",
        "summary": (f"Indexed {len(file_contents)} file(s) → {chunks} chunk(s) "
                    f"({build_system} / {language or 'unknown'})"
                    if indexed_ok else
                    f"Indexing failed: {index_result.get('reason','?')}"),
        "run_id": run_id,
        "project_dir": project_dir,
        "project_id": project_id,
        "project_name": project_name,
        "language": language,
        "build_system": build_system,
        "build_cmd": detect.get("build_cmd", ""),
        "test_cmd": detect.get("test_cmd", ""),
        "marker": marker,
        "files_indexed": sorted(file_contents.keys())[:50],
        "files_total": len(file_contents),
        "indexed_chunks": chunks,
    }

    await events.emit(conv_id, "tool_end", {
        "tool": "project_indexer", "icon": "database",
        "status": (f"📚 Indexed {len(file_contents)} file(s), {chunks} chunk(s) "
                   f"({build_system}, {language or 'unknown'})"
                   if indexed_ok else
                   f"⚠ Indexing failed: {index_result.get('reason','?')[:60]}"),
        "run_id": run_id,
    })
    if run_id:
        try:
            await db.update_run(
                run_id,
                status="succeeded" if indexed_ok else "failed",
                result_envelope=envelope, ended=True,
            )
        except Exception as e:
            print(f"[INDEXER] update_run failed (non-fatal): {e}")

    return envelope
