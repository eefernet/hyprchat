"""Small deterministic coding signals and excerpts for public search queries.

Only search copies are summarized. The chat model keeps the original message.
"""
from dataclasses import dataclass
import re


_LANGUAGES = {
    "swift": "Swift", "python": "Python", "py": "Python",
    "javascript": "JavaScript", "js": "JavaScript", "jsx": "JavaScript",
    "typescript": "TypeScript", "ts": "TypeScript", "tsx": "TypeScript",
    "rust": "Rust", "go": "Go", "golang": "Go", "ruby": "Ruby", "rb": "Ruby",
    "java": "Java", "kotlin": "Kotlin", "c": "C", "cpp": "C++", "c++": "C++",
    "csharp": "C#", "c#": "C#", "cs": "C#", "php": "PHP",
    "bash": "Bash", "sh": "Bash", "shell": "Bash", "sql": "SQL",
    "html": "HTML", "css": "CSS", "vue": "Vue", "react": "React",
}
_FENCE = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n"
    r"(?P<body>[\s\S]*?)(?:\n[ \t]{0,3}(?P=fence)[ \t]*(?=\n|$)|\Z)", re.M,
)
_PROSE_LANGUAGE = re.compile(
    r"\b(?:Swift|Python|JavaScript|TypeScript|Rust|Go|Golang|Ruby|Java|Kotlin|"
    r"C\+\+|C\#|CSharp|PHP|Bash|SQL|HTML|CSS|Vue|React)(?!\w)", re.I,
)
_API = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_ERROR = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]\w*(?:Error|Exception))\b")
_HINT = re.compile(
    r"\b(?:programming|code|function|method|variable|compile[rs]?|compiler|debug|"
    r"syntax|dependency|package|framework|closure|optional|array|string|integer|"
    r"struct|enum|async|await|unicode|utf8|utf16|nil|null)\b", re.I,
)
_SYNTAX = re.compile(
    r"^\s*(?:func\s+\w+\s*\(|def\s+\w+\s*\(|fn\s+\w+\s*\(|"
    r"(?:const|let|var)\s+\w+\s*(?::[^=\n]+)?=|"
    r"(?:public\s+|private\s+)?(?:class|struct)\s+\w+\s*[:{])", re.M,
)
_TYPES = re.compile(r"\b(?:String|Int|Double|Float|Bool|Character|Array|Dictionary|Optional|List|Map|Set)\b")


@dataclass(frozen=True)
class CodingContext:
    language: str
    query: str
    has_code: bool


def extract_coding_context(text: str) -> CodingContext | None:
    raw = str(text or "")
    # Bound inspection, retaining the tail where compiler diagnostics often sit.
    raw = raw if len(raw) <= 20000 else raw[:16000] + "\n" + raw[-4000:]
    fences = list(_FENCE.finditer(raw))
    bodies = [m["body"] for m in fences]
    prose = _FENCE.sub(" ", raw)
    language = next((_LANGUAGES.get(m["info"].strip().split()[0].lower(), "")
                     for m in fences if m["info"].strip()
                     and m["info"].strip().split()[0].lower() in _LANGUAGES), "")
    syntax = _SYNTAX.search("\n".join(bodies) if fences else raw)
    has_code = bool(language or syntax)
    if syntax and not fences:
        bodies = [raw[syntax.start():]]
        prose = raw[:syntax.start()]
    evidence = re.sub(r"https?://\S+", " ", prose + "\n" + "\n".join(bodies))
    symbols = _ERROR.findall(evidence) + _API.findall(evidence)
    if not language:
        # A name alone is insufficient: Swift/Rust/Go/React also have non-code uses.
        names = _PROSE_LANGUAGE.findall(re.sub(r"\bTaylor\s+Swift\b", "", prose, flags=re.I))
        if names and (symbols or _HINT.search(evidence) or syntax):
            language = _LANGUAGES[names[0].lower()]
    if not language and syntax:
        language = next((name for pattern, name in (
            (r"\bfunc\s+\w+\s*\(", "Swift"), (r"\bdef\s+\w+\s*\(", "Python"),
            (r"\bfn\s+\w+\s*\(", "Rust"),
        ) if re.search(pattern, syntax.group(0))), "")
    if not language and not syntax:
        return None
    if not has_code:
        return CodingContext(language, text, False)
    symbols += _TYPES.findall("\n".join(bodies))
    symbols = list(dict.fromkeys(symbols))[:6]
    operations = [phrase for phrase in ("else if", "guard", "await", "try", "catch")
                  if re.search(r"\b" + phrase + r"\b", "\n".join(bodies))]
    question = re.sub(r"https?://\S+|[`{}<>]", " ", prose)
    question = " ".join(question.split()[:14])
    # Put public API/error identifiers first so query word limits retain them.
    parts = [language, "programming", *symbols, *operations[:2], question]
    query = " ".join(dict.fromkeys(part for part in parts if part)).strip()[:320]
    return CodingContext(language, query, True)
