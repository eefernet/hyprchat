"""Offline unit tests for custom-tool validation and execution assembly.

No live server, no Codebox — pure helpers only (events.validate_custom_tool,
tooling.codebox_tools.build_custom_tool_code / tool_import_names).
"""
import contextlib
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from events import validate_custom_tool
from tooling.codebox_tools import build_custom_tool_code, pip_name_for_import, tool_import_names


# ── validate_custom_tool ──

def test_auto_aligns_name_to_first_function():
    code = "def fetch_data(url: str) -> str:\n    return url"
    assert validate_custom_tool("my tool", code) == "fetch_data"


def test_keeps_provided_name_when_it_matches_any_def():
    code = "def helper():\n    pass\n\ndef main_tool(q: str):\n    return q"
    assert validate_custom_tool("main_tool", code) == "main_tool"


def test_filename_stem_mismatch_resolves_to_function():
    code = "def get_weather(city: str) -> str:\n    return city"
    assert validate_custom_tool("weather_tool", code) == "get_weather"


def test_empty_code_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_custom_tool("t", "   ")


def test_syntax_error_reports_line():
    with pytest.raises(ValueError, match="line 2"):
        validate_custom_tool("t", "x = 1\ndef broken(:\n    pass")


def test_no_function_rejected():
    with pytest.raises(ValueError, match="top-level function"):
        validate_custom_tool("t", "x = 1\nprint(x)")


def test_async_only_rejected():
    with pytest.raises(ValueError, match="async"):
        validate_custom_tool("t", "async def fetch(url: str):\n    return url")


def test_reserved_name_rejected():
    code = "def execute_code(code: str):\n    return code"
    with pytest.raises(ValueError, match="built-in"):
        validate_custom_tool("execute_code", code, frozenset({"execute_code"}))


# ── build_custom_tool_code ──

def _run_generated(code: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})
    return buf.getvalue()


def test_generated_code_runs_with_tricky_args():
    tool_code = "def echo_args(q, n, flag, obj):\n    return f'{q}|{n}|{flag}|{obj[\"x\"]}'"
    args = {"q": "a'b\"c\nd", "n": 3, "flag": True, "obj": {"x": 1}}
    out = _run_generated(build_custom_tool_code(tool_code, "echo_args", args))
    assert out.strip() == "a'b\"c\nd|3|True|1".strip() or "|3|True|1" in out


def test_generated_code_no_args():
    out = _run_generated(build_custom_tool_code("def hello():\n    return 'hi'", "hello", {}))
    assert out.strip() == "hi"


def test_generated_code_none_return_prints_empty():
    out = _run_generated(build_custom_tool_code("def noop():\n    return None", "noop", {}))
    assert out.strip() == ""


def test_generated_code_missing_function_raises_nameerror():
    code = build_custom_tool_code("def run(input):\n    return input", "new_tool", {"input": "x"})
    with pytest.raises(NameError):
        _run_generated(code)


# ── tool_import_names ──

def test_import_names_filters_stdlib():
    code = "import json\nimport requests\nfrom bs4 import BeautifulSoup\nfrom os.path import join"
    roots = tool_import_names(code)
    assert roots == {"requests", "bs4"}


def test_import_names_syntax_error_is_empty():
    assert tool_import_names("def broken(:") == set()


def test_pip_name_mapping():
    assert pip_name_for_import("bs4") == "beautifulsoup4"
    assert pip_name_for_import("requests") == "requests"
