"""A chat's scripts run in /root/work on the sandbox, not loose in /root (2026-11-16)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import chat_workdir
from tooling import codebox_tools


def test_a_script_starts_in_the_work_folder_and_creates_it_first():
    command = codebox_tools._script_command("print(1)", "/root/venv/bin/python3", "py")
    assert command.startswith("mkdir -p /root/work && cd /root/work && ")
    assert "cd /root &&" not in command
    # The script itself still lives in a temporary folder that is removed afterwards.
    assert "/tmp/hc-exec-" in command and "trap" in command


def test_a_shell_command_runs_from_the_work_folder():
    assert chat_workdir.in_work("ls -la") == "mkdir -p /root/work && cd /root/work && ls -la"


@pytest.mark.parametrize("given,expected", [
    ("index.html", "/root/work/index.html"),
    ("site/index.html", "/root/work/site/index.html"),
    ("./out.png", "/root/work/out.png"),
    ("/root/chat_files/c1/u1/data.csv", "/root/chat_files/c1/u1/data.csv"),
    ("/tmp/x", "/tmp/x"),
    ("~/notes.txt", "/root/notes.txt"),
    ("", "/root/work"), (None, "/root/work"),
])
def test_relative_paths_are_relative_to_the_work_folder_and_absolute_ones_are_left_alone(given, expected):
    assert chat_workdir.resolve(given) == expected


def test_an_empty_path_can_stay_empty_for_tools_that_require_one():
    assert chat_workdir.resolve("", default="") == ""


@pytest.mark.parametrize("asked,also", [
    ("/root/output.png", "/root/work/output.png"),
    ("/root/site/index.html", "/root/work/site/index.html"),
    ("/root/work/output.png", None),          # already there
    ("/root/chat_files/c/u/data.csv", None),  # an upload: it has one place
    ("/root/venv/bin/python3", None),
    ("/tmp/out.png", None),
    ("/root/", None),
])
def test_a_file_asked_for_under_root_out_of_habit_is_also_looked_for_in_the_work_folder(asked, also):
    assert chat_workdir.fallback(asked) == also


def test_the_tool_descriptions_name_the_work_folder():
    import tools
    described = {name: d["function"] for name, d in tools.CODEAGENT_TOOLS.items() if isinstance(d, dict) and d.get("function")}
    assert "/root/work/" in described["execute_code"]["description"]
    assert "/root/work/" in described["run_shell"]["description"]
    assert "Always use absolute paths starting with /root/" not in described["write_file"]["description"]
    assert "/root/output.png" not in str(described["download_file"])
