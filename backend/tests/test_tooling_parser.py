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
