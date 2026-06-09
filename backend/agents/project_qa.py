"""
ProjectQA agent — Coder Bot v2 Phase 4.

Read-only Q&A agent for existing projects. Replaces the v1 pattern of
"orchestrator hand-rolls read_file + search_files in 5+ chat rounds to answer
a question about the code" with a single stateless ProjectQA run that:

  1. Lists the project tree to orient.
  2. Extracts keywords from the question and greps for candidate files.
  3. Reads snippets from the top matches.
  4. Asks the overseer model to compose a grounded answer with file:line
     citations, plus a structured `looks_like_change_request` flag so the
     chat agent can route a "yes, change it" follow-up to the Builder.

The agent NEVER modifies files. Its outputs are: an answer string, a list of
files actually inspected, and the change-request flag. Persisted as a `runs`
row with role='qa' for the same durability story as builder/reviewer/fixer.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import uuid

import config
import database as db
import cancel_registry

from agents.acceptance import _configured_num_ctx, _ollama_response_text


# Common English "stopwords" that aren't useful as grep targets. Keep small —
# we want to filter out fillers but keep technical terms intact.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "must",
    "this", "that", "these", "those", "i", "you", "he", "she", "it",
    "we", "they", "what", "which", "who", "when", "where", "why", "how",
    "all", "each", "every", "any", "some", "no", "not", "only", "own",
    "same", "so", "than", "too", "very", "can", "just", "of", "in", "on",
    "at", "to", "for", "from", "with", "by", "about", "into", "through",
    "during", "before", "after", "above", "below", "between", "out",
    "again", "further", "then", "once", "here", "there", "if", "as",
    "because", "while", "off", "down", "up", "over", "under",
    "does", "doing", "done", "use", "using", "code", "function", "method",
    "class", "file", "files", "project", "tell", "show", "explain",
}


# Heuristics for detecting that a question is actually a change request.
# We err on the side of NOT flagging (false negative is fine — the user can
# clarify), but flag obvious phrasings.
_CHANGE_VERBS = (
    "add", "addition", "create", "make", "build", "implement",
    "fix", "fixes", "fixed", "patch", "repair", "resolve",
    "change", "changes", "modify", "update", "rewrite", "refactor",
    "remove", "delete", "drop", "strip",
    "rename", "move",
    "support", "enable", "disable",
    "convert", "switch",
)
_CHANGE_PHRASES = (
    "can you", "could you", "please", "i want", "i'd like",
    "make it", "let's", "lets", "would like", "should",
    "do that", "do it", "go ahead",
)


def _is_change_request(question: str) -> bool:
    q = (question or "").lower().strip()
    if not q:
        return False
    # Strong signal: imperative verb at the start.
    first_word = q.split()[0] if q.split() else ""
    if first_word in _CHANGE_VERBS:
        return True
    # Phrasal markers that strongly imply "do this".
    if any(p in q for p in _CHANGE_PHRASES):
        # But filter out cases where the user is asking ABOUT past work,
        # e.g. "can you tell me how X works" — those are still questions.
        if any(s in q for s in ("how does", "how do", "what does", "what do",
                                "where is", "where are", "why does", "why do",
                                "tell me", "explain", "show me how", "show me where")):
            return False
        return True
    return False


def _extract_keywords(question: str, max_kw: int = 6) -> list[str]:
    """Pull likely-useful identifiers from the question for grep.

    Strategy:
      - Tokenize on non-word chars.
      - Keep tokens that look like identifiers (camelCase, snake_case, dotted).
      - Drop stopwords.
      - Prefer longer tokens (more specific).
    """
    if not question:
        return []
    # Capture identifier-ish tokens including dots and underscores.
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", question)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        low = tok.lower()
        if low in _STOPWORDS:
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
    # Sort by length descending — longer tokens are more specific. Cap.
    out.sort(key=len, reverse=True)
    return out[:max_kw]


# Source-file extensions we look for when scanning a question for explicit
# filename mentions. Order matters — check longer ones first so e.g.
# "foo.tar.gz" doesn't get truncated to ".gz".
_FILENAME_EXTS = (
    ".java", ".kt", ".scala",
    ".py", ".pyx",
    ".rs", ".go",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".rb", ".php", ".swift", ".cs", ".m", ".mm",
    ".html", ".css", ".scss", ".vue", ".svelte",
    ".sh", ".bash", ".sql", ".graphql", ".proto",
    ".toml", ".yaml", ".yml", ".json", ".xml",
    ".md", ".rst", ".txt",
)


def _extract_filename_targets(question: str) -> list[str]:
    """Pull filename-like tokens from the question.

    Handles:
      - explicit filenames: "ScoreTracker.java", "main.rs"
      - fuzzy split forms: "score tracker.java" (user typed a space) → also
        emits "scoretracker.java" so the basename matcher can fuzzy-find it
      - capitalized class-style names that imply a file: "ScoreTracker" →
        emits "scoretracker.java" if a Java context is plausible (we just
        emit it generically and let the file-tree matcher decide)
    Returns a list of lowercased basename targets to search for.
    """
    if not question:
        return []
    q = question
    out: list[str] = []
    seen: set[str] = set()

    # 1. Direct ".ext" matches — anything word.ext, possibly with path prefix.
    ext_pattern = "|".join(re.escape(e) for e in _FILENAME_EXTS)
    for m in re.finditer(rf"([A-Za-z][A-Za-z0-9_./-]*({ext_pattern}))",
                          q, flags=re.IGNORECASE):
        target = m.group(1).strip().lower()
        # Strip any leading path
        target = target.split("/")[-1]
        if target and target not in seen:
            seen.add(target)
            out.append(target)

    # 2. Reattach single-space-split filenames: "score tracker.java" → "scoretracker.java"
    # We look for "<word> <word>.<ext>" and emit the squished form, in case the
    # user typed the camelCase class name with spaces. Skip the squish when the
    # first word is a common English filler (show, me, the, in, etc.) — those
    # are obviously NOT part of the filename and would produce junk like
    # "showmescoretracker.java" otherwise.
    for m in re.finditer(rf"([A-Za-z]+)\s+([A-Za-z]+({ext_pattern}))",
                          q, flags=re.IGNORECASE):
        first_word = m.group(1).lower()
        if first_word in _STOPWORDS:
            continue
        squished = (m.group(1) + m.group(2)).lower()
        if squished and squished not in seen:
            seen.add(squished)
            out.append(squished)

    return out


async def _resolve_filename_targets(file_list: list[str],
                                     targets: list[str]) -> list[str]:
    """Match each target against the project's file tree by basename.

    Strategy: case-insensitive `endswith` on the basename. Returns a list of
    project-relative paths (preserving order and uniqueness). One target may
    match multiple files (e.g., 'main.rs' in a multi-crate workspace) — we
    keep them all up to a small cap.
    """
    if not file_list or not targets:
        return []
    matches: list[str] = []
    seen: set[str] = set()
    for target in targets:
        target_low = target.lower()
        for path in file_list:
            base = path.rsplit("/", 1)[-1].lower()
            if base == target_low or base.endswith(target_low):
                if path not in seen:
                    seen.add(path)
                    matches.append(path)
        if len(matches) >= 6:
            break
    return matches[:6]


async def _list_files(http, project_dir: str, max_depth: int = 5) -> list[str]:
    """Return a list of source files in the project, capped to a sensible limit.

    Excludes typical build/cache dirs so the model doesn't see thousands of
    artifacts.
    """
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"find . -maxdepth {max_depth} -type f "
        f"-not -path '*/.git/*' -not -path '*/node_modules/*' "
        f"-not -path '*/target/*' -not -path '*/build/*' -not -path '*/dist/*' "
        f"-not -path '*/out/*' -not -path '*/__pycache__/*' "
        f"-not -path '*/venv/*' -not -path '*/.venv/*' "
        f"| head -200"
    )
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            stdout = (r.json().get("stdout") or "")
            return [line.strip() for line in stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"[QA] list_files failed: {e}")
    return []


async def _grep_for_keywords(http, project_dir: str,
                              keywords: list[str]) -> list[dict]:
    """Run grep across the project for each keyword. Returns a list of
    {file, line, text} hits, deduped per file.

    Caps total hits to keep prompt size sane.
    """
    if not keywords:
        return []
    pattern = "|".join(re.escape(k) for k in keywords)
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        f"grep -rEn -m 3 "
        f"--include='*.java' --include='*.py' --include='*.go' --include='*.rs' "
        f"--include='*.js' --include='*.ts' --include='*.tsx' --include='*.jsx' "
        f"--include='*.c' --include='*.cpp' --include='*.h' --include='*.hpp' "
        f"--include='*.md' --include='*.toml' --include='*.json' --include='*.xml' "
        f"--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=target "
        f"--exclude-dir=build --exclude-dir=dist --exclude-dir=out "
        f"--exclude-dir=__pycache__ --exclude-dir=venv --exclude-dir=.venv "
        f"-- {shlex.quote(pattern)} . 2>/dev/null | head -40"
    )
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 15},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        stdout = (r.json().get("stdout") or "")
    except Exception as e:
        print(f"[QA] grep failed: {e}")
        return []

    hits: list[dict] = []
    for line in stdout.splitlines():
        # grep -n format: "path:line:text" (with -E and --include there can be
        # binary ":" chars in text but we only need first two splits).
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path, lineno, text = parts
        try:
            lineno_i = int(lineno)
        except ValueError:
            continue
        hits.append({"file": path, "line": lineno_i, "text": text.strip()})
    return hits


async def _read_file_full(http, project_dir: str, path: str,
                            max_bytes: int = 30000) -> str:
    """Read a file's full contents (capped at max_bytes). Used for filename-
    targeted reads where the user explicitly asked to see/break down a file."""
    rel = path
    full = (f"{project_dir.rstrip('/')}/{rel.removeprefix('./')}"
            if not rel.startswith("/") else rel)
    cmd = f"head -c {max_bytes} {shlex.quote(full)}"
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            return (r.json().get("stdout") or "")
    except Exception as e:
        print(f"[QA] read_file_full failed for {path}: {e}")
    return ""


async def _read_snippet(http, project_dir: str, path: str, line: int = 0,
                         radius: int = 18) -> str:
    """Read a chunk of a file centered on `line`. If line is 0, read the head."""
    rel = path
    full = f"{project_dir.rstrip('/')}/{rel.removeprefix('./')}" if not rel.startswith("/") else rel
    if line and line > radius:
        start = line - radius
        n = radius * 2 + 1
        cmd = f"sed -n '{start},{start + n - 1}p' {shlex.quote(full)}"
    else:
        cmd = f"head -n {radius * 2 + 1} {shlex.quote(full)}"
    try:
        r = await http.post(
            f"{config.CODEBOX_URL}/command",
            json={"command": cmd, "timeout": 10},
            timeout=15,
        )
        if r.status_code == 200:
            return (r.json().get("stdout") or "")[:3000]
    except Exception as e:
        print(f"[QA] read_snippet failed for {path}: {e}")
    return ""


_QA_PROMPT = """You are ProjectQA — a focused agent that answers questions about an existing codebase. Your answer must be grounded in the file contents you've been shown. Do NOT invent file contents, function signatures, or behaviors that aren't in the snippets below. If the snippets don't contain enough info to answer, say so explicitly.

## Project context
- Project root: `{project_dir}`
- Language: {language}

## File tree (top-level summary, may be truncated)
```
{file_tree}
```

## User's question
{question}

## Code snippets the agent retrieved (grep hits + surrounding context)
{snippets_section}

## Output rules
1. Answer directly. Plain English. Markdown is fine.
2. CITE every claim that depends on the code: use the form `path/to/file.ext:line` (or `path:start-end` for ranges) inline as you describe behavior.
3. If the question is genuinely ambiguous or the code doesn't show enough, say what's missing. Do NOT guess.
4. Code excerpts: keep them short (≤6 lines) for *general* questions where you're describing behavior. BUT when the user explicitly asks to see, read, break down, walk through, or explain a file/function (questions like "show me X", "break down X", "walk through Y", "explain the code in Z"), reproduce the relevant code in fenced blocks — that's literally what they asked for. Never refuse to show code that's already in the snippets section above.
5. If you've been given the full contents of a file in the snippets and the user asked for a line-by-line breakdown, walk through it section-by-section, quoting actual lines/blocks from the snippet and explaining each. Do NOT say "I don't have access" when the file IS in the snippets section.
6. If your answer truly is "this isn't shown in the code I read", suggest the most likely 1-2 files the user could check.

Output your answer now:"""


async def run_project_qa(http, events, conv_id: str, *,
                          project_dir: str, question: str,
                          language: str = "",
                          conv_model: str = "",
                          parent_run_id: str = "") -> dict:
    """Execute a ProjectQA run. Returns a result envelope with the answer,
    files examined, and a `looks_like_change_request` flag.

    `conv_model` is the model currently selected by the chat (persona's pinned
    model or the user's chat-dropdown choice). The QA agent uses it directly
    so the answer comes from the same model the user is talking to — not a
    hardcoded fallback.
    """
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        await db.create_run(run_id, conv_id, role="qa",
                            parent_run_id=parent_run_id, status="running")
    except Exception as e:
        print(f"[QA] create_run failed (non-fatal): {e}")
        run_id = ""

    if run_id:
        cancel_registry.register(run_id)

    _step_n = [0]

    async def _step(action: str, detail: str = ""):
        _step_n[0] += 1
        await events.emit(conv_id, "tool_progress", {
            "tool": "ask_project", "icon": "help-circle",
            "status": f"❓ {action}{': ' + detail[:100] if detail else ''}",
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

    try:  # try/finally ensures cancel_registry.cleanup on every exit path

        if not question:
            envelope = {"status": "error", "summary": "ask_project requires a question",
                        "answer": "", "files_examined": [],
                        "looks_like_change_request": False}
            if run_id:
                try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
                except Exception: pass
            return envelope

        if not project_dir:
            envelope = {"status": "error",
                        "summary": "ask_project requires project_dir; pass it explicitly or run after a builder run",
                        "answer": "", "files_examined": [],
                        "looks_like_change_request": False}
            if run_id:
                try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
                except Exception: pass
            return envelope

        await _step("question", question[:140])

        # Phase 1: orient with a file listing.
        await _step("list_files", project_dir)
        file_list = await _list_files(http, project_dir)
        if not file_list:
            envelope = {
                "status": "error",
                "summary": f"Could not list files at {project_dir} (does the project exist?)",
                "answer": f"I couldn't read the project at `{project_dir}`. The directory may not exist or be empty.",
                "files_examined": [],
                "looks_like_change_request": False,
            }
            await events.emit(conv_id, "tool_end", {
                "tool": "ask_project", "icon": "help-circle",
                "status": f"⚠ Project not accessible at {project_dir}",
                "run_id": run_id,
            })
            if run_id:
                try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
                except Exception: pass
            return envelope

        snippet_blocks: list[str] = []
        files_examined: list[str] = []

        # Phase 2a — filename targeting.
        targets = _extract_filename_targets(question)
        target_paths: list[str] = []
        if targets:
            await _step("filename_targets", ", ".join(targets[:5]))
            target_paths = await _resolve_filename_targets(file_list, targets)
            if target_paths:
                await _step("matched_files", f"{len(target_paths)} file(s): " + ", ".join(p.rsplit('/', 1)[-1] for p in target_paths))
                for path in target_paths:
                    full = await _read_file_full(http, project_dir, path)
                    if full:
                        snippet_blocks.append(
                            f"### `{path}` (full contents — user asked for this file)\n"
                            f"```\n{full[:12000]}\n```"
                            + (f"\n[truncated; full file is {len(full)} chars]" if len(full) > 12000 else "")
                        )
                        files_examined.append(path)
            else:
                await _step("no_match", f"no files in tree match: {targets[:3]}")

        # Phase 2b — keyword grep.
        keywords = _extract_keywords(question)
        await _step("keywords", ", ".join(keywords) if keywords else "(none — using head-of-file fallback)")
        hits = await _grep_for_keywords(http, project_dir, keywords) if keywords else []
        await _step("grep", f"{len(hits)} hit(s) across the tree")

        # Phase 3: pick top grep-files by hit-count, read short snippets.
        file_to_hits: dict[str, list[dict]] = {}
        for h in hits:
            if h["file"] in target_paths:
                continue
            file_to_hits.setdefault(h["file"], []).append(h)
        ranked_files = sorted(
            file_to_hits.items(),
            key=lambda kv: (-len(kv[1]), min(h["line"] for h in kv[1])),
        )
        if not ranked_files and not target_paths:
            await _step("fallback", "no grep hits; reading entry-point candidates")
            candidates = []
            for path in file_list:
                base = path.rsplit("/", 1)[-1].lower()
                if base in ("main.rs", "main.py", "main.go", "main.java",
                            "index.js", "index.ts", "index.html",
                            "lib.rs", "mod.rs", "app.py", "server.py"):
                    candidates.append(path)
                elif base.startswith("readme."):
                    candidates.append(path)
                if len(candidates) >= 4:
                    break
            _fb_tasks = [
                _read_snippet(http, project_dir, p, line=0, radius=20)
                for p in candidates[:4]
            ]
            _fb_results = await asyncio.gather(*_fb_tasks, return_exceptions=True)
            for path, snip in zip(candidates[:4], _fb_results):
                if isinstance(snip, str) and snip:
                    snippet_blocks.append(f"### `{path}` (head)\n```\n{snip[:1800]}\n```")
                    files_examined.append(path)
        else:
            _ranked_paths = [(p, fh[0]["line"], len(fh)) for p, fh in ranked_files[:5]]
            _gr_tasks = [
                _read_snippet(http, project_dir, p, line=anchor, radius=15)
                for p, anchor, _ in _ranked_paths
            ]
            _gr_results = await asyncio.gather(*_gr_tasks, return_exceptions=True)
            for (path, anchor, n_hits), snip in zip(_ranked_paths, _gr_results):
                if isinstance(snip, str) and snip:
                    snippet_blocks.append(
                        f"### `{path}` (around line {anchor}; "
                        f"{n_hits} hit{'s' if n_hits != 1 else ''})\n```\n{snip[:1800]}\n```"
                    )
                    files_examined.append(path)

        # Phase 3b: include semantic code-memory chunks for this uploaded
        # project when the indexer has populated ChromaDB. This complements
        # literal grep for questions phrased in domain/user language.
        try:
            import rag as _rag
            project_id = project_dir.rstrip("/").rsplit("/", 1)[-1]
            semantic = await _rag.query_code_memory(question, top_k=8, language=language)
            semantic = [
                s for s in semantic
                if (not s.get("project_id") or s.get("project_id") == project_id)
                and s.get("filename") in file_list
            ][:4]
            if semantic:
                await _step("semantic", f"{len(semantic)} code-memory hit(s)")
                for hit in semantic:
                    path = hit.get("filename") or ""
                    if not path or path in files_examined:
                        continue
                    snip = await _read_snippet(http, project_dir, path, line=0, radius=20)
                    if snip:
                        snippet_blocks.append(
                            f"### `{path}` (semantic code-memory match; score={hit.get('score', 0):.2f})\n"
                            f"```\n{snip[:1800]}\n```"
                        )
                        files_examined.append(path)
        except Exception as e:
            print(f"[QA] semantic retrieval failed (non-fatal): {e}")

        # Phase 3c: add marker/entry files so answers can cite how the project
        # starts, builds, and tests even when the user's keywords don't appear
        # in those files.
        marker_names = {
            "pyproject.toml", "requirements.txt", "setup.py", "package.json",
            "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
            "README.md", "README.rst", "Makefile",
        }
        marker_paths = []
        for path in file_list:
            base = path.rsplit("/", 1)[-1]
            if base in marker_names and path not in files_examined:
                marker_paths.append(path)
            if len(marker_paths) >= 4:
                break
        if marker_paths:
            await _step("markers", f"{len(marker_paths)} build/entry marker file(s)")
            marker_results = await asyncio.gather(
                *[_read_snippet(http, project_dir, p, line=0, radius=18) for p in marker_paths],
                return_exceptions=True,
            )
            for path, snip in zip(marker_paths, marker_results):
                if isinstance(snip, str) and snip:
                    snippet_blocks.append(f"### `{path}` (build/entry marker)\n```\n{snip[:1600]}\n```")
                    files_examined.append(path)
        snippets_section = "\n\n".join(snippet_blocks) if snippet_blocks else "(no snippets retrieved)"
        await _step("snippets", f"{len(files_examined)} file(s) inspected")

        # Phase 4: ask LLM to compose grounded answer.
        qa_model = (config.QA_MODEL or conv_model or config.DEFAULT_MODEL or config.PLANNING_MODEL or "").strip()
        if not qa_model:
            envelope = {"status": "error",
                        "summary": "No model configured for ProjectQA",
                        "answer": "", "files_examined": files_examined,
                        "looks_like_change_request": _is_change_request(question)}
            if run_id:
                try: await db.update_run(run_id, status="failed", result_envelope=envelope, ended=True)
                except Exception: pass
            return envelope

        tree_text = "\n".join(file_list[:80])
        if len(file_list) > 80:
            tree_text += f"\n... ({len(file_list) - 80} more files)"

        prompt = _QA_PROMPT.format(
            project_dir=project_dir,
            language=language or "(unknown)",
            file_tree=tree_text,
            question=question,
            snippets_section=snippets_section,
        )

        await _step("compose", f"calling {qa_model}")
        answer = ""
        try:
            coro = http.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={
                    "model": qa_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.3, "num_ctx": _configured_num_ctx()},
                },
                timeout=600,
            )
            r = await cancel_registry.await_cancellable(coro, run_id)
            if r.status_code == 200:
                # Reasoning models can route the answer to the thinking field
                # even with think disabled — read both.
                answer = _ollama_response_text(r.json())
            else:
                answer = f"(LLM call failed: HTTP {r.status_code})"
        except cancel_registry.RunCancelled:
            cancelled_env = {
                "status": "cancelled",
                "summary": "ProjectQA cancelled by user (Stop pressed) during LLM call.",
                "answer": "",
                "files_examined": files_examined,
                "looks_like_change_request": False,
                "qa_model": qa_model,
                "project_dir": project_dir,
                "language": language,
            }
            try:
                await events.emit(conv_id, "tool_end", {
                    "tool": "ask_project", "icon": "help-circle",
                    "status": "⛔ ProjectQA cancelled by user",
                    "run_id": run_id,
                })
            except Exception:
                pass
            if run_id:
                try:
                    await db.update_run(run_id, status="cancelled",
                                        result_envelope=cancelled_env, ended=True)
                except Exception:
                    pass
            return cancelled_env
        except Exception as e:
            answer = f"(LLM call failed: {type(e).__name__}: {e})"

        looks_like_change = _is_change_request(question)
        envelope = {
            "status": "ok" if answer and not answer.startswith("(LLM call failed") else "error",
            "summary": (f"Answered question by examining {len(files_examined)} file(s)"
                        if answer else "Failed to compose answer"),
            "answer": answer,
            "files_examined": files_examined,
            "keywords": keywords,
            "grep_hits": len(hits),
            "looks_like_change_request": looks_like_change,
            "qa_model": qa_model,
            "project_dir": project_dir,
            "language": language,
        }

        await events.emit(conv_id, "tool_end", {
            "tool": "ask_project", "icon": "help-circle",
            "status": (f"❓ Answered using {len(files_examined)} file(s)"
                       + (" — looks like a change request" if looks_like_change else "")),
            "run_id": run_id,
        })
        if run_id:
            try:
                await db.update_run(
                    run_id,
                    status="succeeded" if envelope["status"] == "ok" else "failed",
                    result_envelope=envelope, ended=True,
                )
            except Exception as e:
                print(f"[QA] update_run failed (non-fatal): {e}")

        return envelope

    finally:
        if run_id:
            cancel_registry.cleanup(run_id)
