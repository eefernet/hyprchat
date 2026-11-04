"""Shared identities, executed-test counts, and bounded source context."""
import hashlib
import json
import re
from pathlib import Path
from context_policy import estimate_tokens

def normalized(text):
    return " ".join(str(text).split())

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def test_count(command, log):
    """Return None for unknown runners, never fabricate a count for success."""
    patterns = []
    if "unittest" in command:
        patterns = [r"Ran (\d+) tests?\b"]
    elif "pytest" in command:
        counts = re.findall(r"(\d+) (?:passed|failed|error)(?:s)?\b", log)
        if counts:
            return sum(map(int, counts[-3:]))
        if re.search(r"no tests ran|collected 0 items", log):
            return 0
    elif "node" in command and "--test" in command:
        patterns = [r"(?:#|ℹ) tests (\d+)"]
    if "vitest" in command or "jest" in command or "Tests:" in log:
        patterns += [r"Tests\s*:?\s+(\d+) passed", r"Tests:.*?(\d+) total"]
    if "cargo test" in command:
        counts=re.findall(r"test result:.*? (\d+) passed",log)
        if counts: return sum(map(int,counts))
    if "mvn test" in command:
        patterns += [r"Tests run: (\d+)"]
    if "go test" in command and "[no test files]" in log and not re.search(r"^ok\s",log,re.M):
        return 0
    for pattern in patterns:
        matches = re.findall(pattern, log)
        if matches:
            return int(matches[-1])
    return None

def source_context(repository, task, budget, changed_paths=(), *, include_root=True):
    """Prioritize referenced/changed files, keeping real ranges and hashes."""
    paths = list(dict.fromkeys(changed_paths))
    with repository.connect() as db:
        inventory = list(db.execute("SELECT path FROM files ORDER BY path"))
    paths = list(dict.fromkeys([*(row[0] for row in inventory if row[0] in task or Path(row[0]).name in task), *paths,
                               *(row[0] for row in inventory if include_root and "/" not in row[0])]))
    snippets = []
    for path in paths:
        try:
            snippet = repository.read(path, limit=120)
        except (ValueError, OSError):
            continue
        if estimate_tokens([*snippets, snippet]) <= budget:
            snippets.append(snippet)
    return snippets
