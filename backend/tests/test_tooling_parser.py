import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tooling.parser import parse_text_tool_calls, strip_tool_calls


def test_generate_image_markdown_fence_parses_prompt():
    content = """Okay, making it now.

```generate_image
A focused EU4 player at a desk with two strategy-map monitors and a whiskey glass.
```
"""

    calls = parse_text_tool_calls(content, {"generate_image"})

    assert calls == [{
        "function": {
            "name": "generate_image",
            "arguments": {
                "prompt": "A focused EU4 player at a desk with two strategy-map monitors and a whiskey glass."
            },
        }
    }]
    cleaned = strip_tool_calls(content, {"generate_image"})
    assert "```generate_image" not in cleaned
    assert "focused EU4 player" not in cleaned
    assert "Okay, making it now." in cleaned


def test_generate_image_markdown_fence_requires_available_tool():
    content = """```generate_image
A portrait that should not run when the tool is unavailable.
```"""

    assert parse_text_tool_calls(content, set()) == []
    assert "```generate_image" in strip_tool_calls(content, set())


def test_non_image_markdown_fences_are_not_tool_calls():
    content = """```mermaid
flowchart LR
  A --> B
```

```chart
{"type":"bar","labels":["A"],"data":[1]}
```

```python
print("generate_image should not matter inside normal code")
```"""

    assert parse_text_tool_calls(content, {"generate_image"}) == []
    cleaned = strip_tool_calls(content, {"generate_image"})
    assert "```mermaid" in cleaned
    assert "```chart" in cleaned
    assert "```python" in cleaned


def test_existing_generate_image_tool_call_syntax_still_parses():
    xml_call = """<tool_call>
{"name":"generate_image","arguments":{"prompt":"a castle at sunrise"}}
</tool_call>"""
    py_call = 'generate_image("a castle at sunset")'

    assert parse_text_tool_calls(xml_call, {"generate_image"}) == [{
        "function": {"name": "generate_image", "arguments": {"prompt": "a castle at sunrise"}}
    }]
    assert parse_text_tool_calls(py_call, {"generate_image"}) == [{
        "function": {"name": "generate_image", "arguments": {"prompt": "a castle at sunset"}}
    }]


def test_skill_tools_parse_in_every_text_fallback_shape():
    """A body-less call is the normal text-fallback shape; a body survives only in <parameter=...> form."""
    names = {"skill_view", "skill_manage"}

    def one(text):
        calls = parse_text_tool_calls(text, names)
        assert len(calls) == 1
        return calls[0]["function"]

    assert one('skill_view("resize-images")') == {"name": "skill_view", "arguments": {"name": "resize-images"}}
    assert one('<tool_call>\n{"name": "skill_view", "arguments": {"name": "resize-images"}}\n</tool_call>')["arguments"] == {"name": "resize-images"}
    asked = one('<tool_call>\n{"name": "skill_manage", "arguments": {"action": "create", "name": "resize-images", '
                '"description": "Use when images need resizing"}}\n</tool_call>')
    assert asked["arguments"] == {"action": "create", "name": "resize-images", "description": "Use when images need resizing"}
    full = one("<function=skill_manage>\n<parameter=action>create</parameter>\n<parameter=name>resize-images</parameter>\n"
               "<parameter=body>## Steps\n1. Run it.\n\n```bash\nconvert a b\n```\n</parameter>\n</function>")
    assert full["arguments"]["body"] == "## Steps\n1. Run it.\n\n```bash\nconvert a b\n```"


# ── An answer that shows code is not a tool call (found 2026-11-13) ──

TOOLS = {"read_file", "write_file", "execute_code", "run_shell", "list_files"}


def _names(content, native=False):
    return [call["function"]["name"] for call in parse_text_tool_calls(content, TOOLS, native=native)]


def test_a_sentence_that_mentions_a_call_is_prose():
    assert _names('You can call read_file("data.csv") in your own code.') == []


def test_code_that_defines_or_assigns_is_an_example_not_a_call():
    helper = ('Here is the script:\n\n```python\ndef read_file(path):\n    return open(path).read()\n\n'
              'data = read_file("data.csv")\nwrite_file("out.txt", data)\n```\nDone.')
    assert _names(helper) == []
    assert _names('```python\ndata = read_file("data.csv")\nprint(data)\n```') == []


def test_text_mode_still_takes_calls_that_stand_alone():
    assert _names('I will list the files now.\nlist_files("/root/projects")') == ["list_files"]
    block = '```python\n# set up\nrun_shell("pip install requests")\nwrite_file("/root/app.py", """print(1)\nprint(2)""")\n```'
    calls = parse_text_tool_calls(block, TOOLS)
    assert [call["function"]["name"] for call in calls] == ["run_shell", "write_file"]
    assert calls[1]["function"]["arguments"]["content"] == "print(1)\nprint(2)"


def test_a_model_with_real_tool_definitions_only_calls_through_explicit_markup():
    explained = ('A tool call is a small JSON object. The model writes one when it wants the app to do something, '
                 'and the app runs it and replies with the result. It looks like this:\n```json\n'
                 '{"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}\n```')
    assert _names(explained, native=True) == []
    assert _names('list_files("/root/projects")', native=True) == []
    # Explicit markup and a bare call with a short lead-in still work: native models do emit those.
    tagged = "x" * 300 + '\n<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>'
    assert _names(tagged, native=True) == ["read_file"]
    assert _names('Saving it now:\n{"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}', native=True) == ["write_file"]
    assert _names('{"name": "run_shell", "arguments": {"command": "ls"}}', native=True) == ["run_shell"]
