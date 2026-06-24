"""Text/native fallback tool-call parsing helpers."""
import json
import re

# ── Text-based tool call parsing ──
# When models output tool calls as text instead of using native Ollama protocol,
# these functions extract and clean them.

def _extract_json_objects(text: str) -> list[str]:
    """Extract top-level JSON objects from text using brace-depth tracking.
    More reliable than regex for nested JSON structures."""
    objects = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_str = False
            esc = False
            j = i
            while j < len(text):
                c = text[j]
                if esc:
                    esc = False
                elif c == '\\' and in_str:
                    esc = True
                elif c == '"' and not esc:
                    in_str = not in_str
                elif not in_str:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            objects.append(text[start:j + 1])
                            i = j
                            break
                j += 1
        i += 1
    return objects


def _normalize_tool_args(args):
    """Normalize tool arguments — handles string-encoded JSON from Ollama."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    return args


def _fix_json_newlines(text: str) -> str:
    """Fix JSON with unescaped newlines inside string values.
    Models often output JSON with real newlines in 'code' fields."""
    result = []
    in_str = False
    esc = False
    for i, c in enumerate(text):
        if esc:
            result.append(c)
            esc = False
            continue
        if c == '\\':
            result.append(c)
            if in_str:
                esc = True
            continue
        if c == '"' and not esc:
            in_str = not in_str
            result.append(c)
            continue
        if in_str and c == '\n':
            result.append('\\n')
            continue
        if in_str and c == '\t':
            result.append('\\t')
            continue
        result.append(c)
    return ''.join(result)


def parse_text_tool_calls(content: str, available_names: set) -> list[dict]:
    """Parse tool calls from model text when native Ollama tool protocol fails.
    Handles: raw JSON, <tool_call> tags, JSON in code blocks, bare JSON objects."""
    calls = []

    # Strip markdown code fences and model-specific special tokens for parsing
    stripped = re.sub(r'```(?:json|tool_call|tool)?\s*\n?', '', content).strip().rstrip('`')
    # Strip GPT-OSS / other model special tokens that appear after JSON
    stripped = re.sub(r'<\|(?:call|message|channel|im_end|im_start|eot_id|end)\|>.*', '', stripped, flags=re.DOTALL).strip()

    # 1. Entire response is a single JSON tool call
    for _try_str in (stripped, _fix_json_newlines(stripped)):
        try:
            obj = json.loads(_try_str)
            if isinstance(obj, dict) and obj.get("name") in available_names and "arguments" in obj:
                return [{"function": {"name": obj["name"], "arguments": _normalize_tool_args(obj["arguments"])}}]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # 2. <tool_call>JSON</tool_call> tags (Qwen native format)
    # Also match <|call|>JSON patterns (GPT-OSS format)
    tag_matches = re.findall(
        r'<(?:tool[_\-]?call[s]?|\|call\|)>\s*(.*?)\s*</(?:tool[_\-]?call[s]?|\|call\|)>',
        content, re.DOTALL | re.IGNORECASE
    )
    for raw in tag_matches:
        for json_str in _extract_json_objects(raw):
            try:
                obj = json.loads(json_str)
                name = obj.get("name", "")
                args = _normalize_tool_args(obj.get("arguments", obj.get("parameters", {})))
                if name in available_names:
                    calls.append({"function": {"name": name, "arguments": args}})
            except (json.JSONDecodeError, TypeError):
                pass
    if calls:
        return calls

    # 2b. <function=NAME><parameter=KEY>VALUE</parameter>...</function> (qwen3-coder, Hermes XML-ish)
    # Example:
    #   <function=list_files>
    #   <parameter=path>
    #   /root/projects/proj-abc
    #   </parameter>
    #   </function>
    for fn_match in re.finditer(
        r'<function\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</function\s*>',
        content, re.DOTALL | re.IGNORECASE,
    ):
        fname = fn_match.group(1).strip()
        body = fn_match.group(2)
        if fname not in available_names:
            continue
        args: dict = {}
        for p_match in re.finditer(
            r'<parameter\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</parameter\s*>',
            body, re.DOTALL | re.IGNORECASE,
        ):
            pkey = p_match.group(1).strip()
            pval = p_match.group(2).strip()
            # Coerce obvious literals so numeric/bool params still work
            if pval.lower() in ("true", "false"):
                args[pkey] = (pval.lower() == "true")
            else:
                try:
                    if re.fullmatch(r'-?\d+', pval):
                        args[pkey] = int(pval)
                    elif re.fullmatch(r'-?\d+\.\d+', pval):
                        args[pkey] = float(pval)
                    else:
                        args[pkey] = pval
                except (ValueError, TypeError):
                    args[pkey] = pval
        calls.append({"function": {"name": fname, "arguments": args}})
    if calls:
        return calls

    # 3. JSON objects with name+arguments anywhere in text
    for json_str in _extract_json_objects(stripped):
        try:
            obj = json.loads(json_str)
            if isinstance(obj, dict) and obj.get("name") in available_names:
                args = obj.get("arguments", obj.get("parameters", {}))
                if args is not None:
                    calls.append({"function": {"name": obj["name"], "arguments": _normalize_tool_args(args)}})
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Try extracting from code blocks specifically
    if not calls:
        code_blocks = re.findall(r'```(?:json|tool_call|tool)?\s*\n(.*?)\n\s*```', content, re.DOTALL)
        for block in code_blocks:
            for json_str in _extract_json_objects(block.strip()):
                for _try in (json_str, _fix_json_newlines(json_str)):
                    try:
                        obj = json.loads(_try)
                        if isinstance(obj, dict) and obj.get("name") in available_names:
                            args = obj.get("arguments", obj.get("parameters", {}))
                            if args is not None:
                                calls.append({"function": {"name": obj["name"], "arguments": _normalize_tool_args(args)}})
                                break
                    except (json.JSONDecodeError, TypeError):
                        pass

    # 5. Python function call syntax: run_shell("cmd"), write_file("path", "content"), etc.
    #    Models sometimes write tool calls as Python code instead of using the protocol.
    if not calls:
        calls = _parse_python_tool_calls(content, available_names)

    # 6. Loose roleplay-model syntax: <tools> "generate_image" (prompt: ...)
    if not calls:
        calls = _parse_loose_tool_calls(content, available_names)

    return calls


def _parse_python_tool_calls(content: str, available_names: set) -> list[dict]:
    """Parse Python-style function calls from model output.
    Catches patterns like: run_shell("pip install foo"), write_file("/root/app.py", '''code'''), etc."""
    calls = []

    # Extract all code blocks, or use full content if no code blocks
    code_blocks = re.findall(r'```(?:\w*)\n(.*?)\n\s*```', content, re.DOTALL)
    texts_to_scan = code_blocks if code_blocks else [content]

    for text in texts_to_scan:
        for name in available_names:
            # Match tool_name( ... ) — find the opening paren after the tool name
            pattern = rf'\b{re.escape(name)}\s*\('
            for m in re.finditer(pattern, text):
                start = m.end()  # position after opening (
                args = _extract_balanced_parens(text, start)
                if args is None:
                    continue
                parsed = _parse_python_args(name, args)
                if parsed:
                    calls.append({"function": {"name": name, "arguments": parsed}})
    return calls


def _extract_balanced_parens(text: str, start: int) -> str | None:
    """Extract content between balanced parentheses starting at position after opening paren."""
    depth = 1
    i = start
    in_str = False
    str_char = None
    in_triple = False
    esc = False
    while i < len(text):
        c = text[i]
        if esc:
            esc = False
            i += 1
            continue
        if c == '\\' and in_str and not in_triple:
            esc = True
            i += 1
            continue
        # Triple-quote detection
        if not in_str and i + 2 < len(text) and text[i:i+3] in ("'''", '"""'):
            in_str = True
            in_triple = True
            str_char = text[i:i+3]
            i += 3
            continue
        if in_triple and i + 2 < len(text) and text[i:i+3] == str_char:
            in_str = False
            in_triple = False
            str_char = None
            i += 3
            continue
        if not in_str and c in ('"', "'"):
            in_str = True
            str_char = c
            i += 1
            continue
        if in_str and not in_triple and c == str_char:
            in_str = False
            str_char = None
            i += 1
            continue
        if not in_str:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return text[start:i]
        i += 1
    return None


def _parse_python_args(tool_name: str, raw_args: str) -> dict | None:
    """Parse Python function arguments into a tool arguments dict."""
    raw_args = raw_args.strip()
    if not raw_args:
        return {}

    # ── Handle keyword arguments first: tool(key="value", key2="value2") ──
    # Match patterns like: command="...", path="/root/...", task="..."
    kw_pattern = re.findall(r'(\w+)\s*=\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\'|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')', raw_args, re.DOTALL)
    if kw_pattern:
        result = {}
        for kw_match in kw_pattern:
            key = kw_match[0]
            # Pick the first non-empty capture group (triple-double, triple-single, double, single)
            val = kw_match[1] or kw_match[2] or kw_match[3] or kw_match[4]
            val = val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            result[key] = val
        if result:
            return result

    # ── Positional arguments fallback ──
    # Try to evaluate string literals safely
    # We'll extract quoted string arguments
    args_list = []
    i = 0
    while i < len(raw_args):
        c = raw_args[i]
        if c in (' ', ',', '\n', '\t'):
            i += 1
            continue
        # Triple-quoted string
        if i + 2 < len(raw_args) and raw_args[i:i+3] in ("'''", '"""'):
            q = raw_args[i:i+3]
            end = raw_args.find(q, i + 3)
            if end == -1:
                return None
            args_list.append(raw_args[i+3:end])
            i = end + 3
            continue
        # Single/double quoted string
        if c in ('"', "'"):
            j = i + 1
            esc = False
            while j < len(raw_args):
                if esc:
                    esc = False
                elif raw_args[j] == '\\':
                    esc = True
                elif raw_args[j] == c:
                    break
                j += 1
            if j < len(raw_args):
                # Unescape basic escape sequences
                s = raw_args[i+1:j].replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
                args_list.append(s)
                i = j + 1
                continue
            return None
        # f-string — skip
        if c == 'f' and i + 1 < len(raw_args) and raw_args[i+1] in ('"', "'"):
            return None
        # Bare word or number — skip to next comma
        j = i
        depth = 0
        while j < len(raw_args):
            if raw_args[j] == ',' and depth == 0:
                break
            if raw_args[j] in ('(', '[', '{'):
                depth += 1
            elif raw_args[j] in (')', ']', '}'):
                depth -= 1
            j += 1
        token = raw_args[i:j].strip()
        if token:
            args_list.append(token)
        i = j + 1

    if not args_list:
        return None

    param_names = TOOL_PARAMS.get(tool_name)
    if not param_names:
        # Unknown tool — use first arg as "input"
        return {"input": args_list[0]} if args_list else None

    result = {}
    for idx, val in enumerate(args_list):
        if idx < len(param_names):
            result[param_names[idx]] = val
    return result if result else None


# Map positional args to known tool parameter names (used by the python-call
# and loose-call text parsers)
TOOL_PARAMS = {
    "execute_code": ["code", "language"],
    "run_shell": ["command"],
    "write_file": ["path", "content"],
    "read_file": ["path"],
    "list_files": ["path"],
    "download_file": ["filename"],
    "generate_image": ["prompt", "negative_prompt"],
    "download_project": ["filenames", "project_name"],
    "delete_file": ["path"],
    "plan_project": ["task", "language", "constraints"],
    "run_review": ["project_dir", "project_id"],
    "run_acceptance_review": ["project_dir", "reviewer_run_id", "project_id"],
    "search_files": ["pattern", "path", "file_pattern"],
    "diff_files": ["path_a", "path_b"],
    "git_init": ["path", "language"],
    "git_diff": ["path"],
    "git_commit": ["message", "path"],
    "run_tests": ["path", "framework"],
    "lint_code": ["path", "language"],
    "resume_project": ["project_id"],
    "research": ["query"],
    "fetch_url": ["url"],
    "generate_code": ["task", "language", "context"],
    "start_coder_workflow": ["mode", "task", "project_id"],
    "run_aider_fix": ["project_dir", "task", "issue_run_id"],
    "get_coder_workflow": ["workflow_id"],
    "cancel_coder_workflow": ["workflow_id"],
}


def _parse_loose_args(name: str, raw: str) -> dict | None:
    """Parse the loose `key: value` / `key=value` arg style some roleplay
    models invent (unquoted values, prose commas). Commas split a new arg only
    when followed by a `key:`/`key=` token, so commas inside a prompt survive."""
    raw = raw.strip()
    if not raw:
        return {}
    parts = re.split(r',\s*(?=[A-Za-z_]\w*\s*[:=])', raw)
    args: dict = {}
    for part in parts:
        pm = re.match(r'\s*([A-Za-z_]\w*)\s*[:=]\s*(.+?)\s*$', part, re.DOTALL)
        if not pm:
            args = {}
            break
        args[pm.group(1)] = pm.group(2).strip().strip('"\'').strip()
    if args:
        return args
    # Bare single positional: the whole body is the first known param
    params = TOOL_PARAMS.get(name)
    if params:
        val = raw.strip().strip('"\'').strip()
        if val:
            return {params[0]: val}
    return None


# Loose parsing is restricted to content-safe tools: a shredded key:value
# body fed to execute_code/run_shell would EXECUTE garbage. These tools take
# a single descriptive string, so a slightly-mangled arg is harmless.
_LOOSE_PARSE_SAFE_TOOLS = {"generate_image", "research", "fetch_url"}


def _parse_loose_tool_calls(content: str, available_names: set) -> list[dict]:
    """Last-resort parser for mangled tool-call text — e.g. the roleplay-model
    shape `<tools> "generate_image" (prompt: selfie of ...)`: quoted name,
    space before the paren, unquoted key: value args. Only fires when the text
    shows tool-call INTENT (quoted/backticked name, or a <tool...> tag in the
    content) so plain prose mentioning a tool name can't trigger it."""
    calls = []
    has_tag = bool(re.search(r'<tools?\b|<tool[_\-]?call', content, re.IGNORECASE))
    for name in available_names & _LOOSE_PARSE_SAFE_TOOLS:
        for m in re.finditer(rf'(?<![\w.])(["\'`]?){re.escape(name)}(["\'`]?)\s*\(', content):
            quoted = bool(m.group(1) or m.group(2))
            if not (quoted or has_tag):
                continue
            raw = _extract_balanced_parens(content, m.end())
            if raw is None:
                continue
            # Loose key:value style first — _parse_python_args mangles it
            # (treats "prompt: text, more text" as comma-split positionals).
            args = _parse_loose_args(name, raw)
            if not args:
                args = _parse_python_args(name, raw)
            if args:
                calls.append({"function": {"name": name, "arguments": args}})
    return calls


def strip_tool_calls(content: str, available_names: set | None = None) -> str:
    """Remove tool call artifacts from content so the user sees clean text."""
    # Remove <tool_call>...</tool_call>
    content = re.sub(
        r'<tool[_\-]?call[s]?\b[^>]*>\s*.*?\s*</tool[_\-]?call[s]?>',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove <tools>...</tools> wrappers used by some text-tool-call models.
    content = re.sub(
        r'<tools?\b[^>]*>\s*.*?\s*</tools?>',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # If a model opens a tool tag and truncates before closing it, treat the
    # rest of the response as tool junk. Keeping it visible leaks raw tool JSON.
    content = re.sub(
        r'<tool[_\-]?call[s]?\b[^>]*>.*$',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    content = re.sub(
        r'<tools?\b[^>]*>.*$',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove qwen3-coder / Hermes-style <function=name>...<parameter=k>v</parameter>...</function>
    content = re.sub(
        r'<function\s*=\s*[A-Za-z_][A-Za-z0-9_]*\s*>.*?</function\s*>',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove ```json blocks containing tool calls
    content = re.sub(
        r'```(?:json|tool_call|tool)?\s*\n?\s*\{[^`]*"name"[^`]*\}\s*\n?\s*```',
        '', content, flags=re.DOTALL
    )
    # Remove bare JSON tool call objects (name + arguments pattern)
    content = re.sub(
        r'\{\s*"name"\s*:\s*"[^"]*"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
        '', content, flags=re.DOTALL
    )
    # Remove loose roleplay-style calls: "tool_name" (args...) — only when the
    # name is quote-wrapped (NOT backtick: `tool` (...) is normal prose) or a
    # <tool...> tag is present, so 'you can use read_file (with a path)' and
    # backtick-quoted explanations survive.
    has_tag = bool(re.search(r'<tools?\b|<tool[_\-]?call', content, re.IGNORECASE))
    if available_names is None:
        from tools import CODEAGENT_TOOLS

        available_names = set(CODEAGENT_TOOLS)
    spans = []
    for name in available_names:
        for m in re.finditer(rf'(?<![\w.])(["\']?){re.escape(name)}(["\']?)\s*\(', content):
            if not (m.group(1) or m.group(2) or has_tag):
                continue
            inner = _extract_balanced_parens(content, m.end())
            if inner is None:
                # Unterminated call (model truncated mid-args) — junk runs to
                # the end of the line; keep any following lines.
                nl = content.find("\n", m.end())
                spans.append((m.start(), nl if nl != -1 else len(content)))
            else:
                spans.append((m.start(), m.end() + len(inner) + 1))
    # Merge overlapping/nested spans BEFORE splicing — removing an inner span
    # first would leave the outer span's indices pointing at shifted text.
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in reversed(merged):
        content = content[:s] + content[e:]
    # Stray wrapper tags (often unpaired)
    content = re.sub(r'</?tools?\b[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'</?tool[_\-]?call[s]?\b[^>]*>', '', content, flags=re.IGNORECASE)
    return content.strip()
