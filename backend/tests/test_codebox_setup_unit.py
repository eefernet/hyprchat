"""Sandbox setup and per-invocation script isolation, without a live Codebox."""
import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tooling import codebox_tools as cb


class Response:
    def __init__(self, stdout="", exit_code=0, status_code=200):
        self.status_code = status_code
        self.payload = {"stdout": stdout, "stderr": "", "exit_code": exit_code}

    def json(self):
        return self.payload


class Sandbox:
    def __init__(self):
        self.commands = []
        self.venv = False
        self.fail_install = False
        self.started = None
        self.finish = None

    async def post(self, url, *, json, **kwargs):
        command = json["command"]
        self.commands.append(command)
        if "DATA_STACK_OK" in command:
            assert self.venv, "install attempted before the venv existed"
            if self.started:
                self.started.set()
                await self.finish.wait()
            if self.fail_install:
                return Response(exit_code=1)
            return Response("DATA_STACK_OK\n")
        if "VENV_OK" in command:
            await asyncio.sleep(0)
            self.venv = True
            return Response("VENV_OK\n")
        return Response("script output\n")


@pytest.fixture(autouse=True)
def reset_setup(monkeypatch):
    for name, value in {
        "_sandbox_venv_ready": False, "_sandbox_venv_lock": None,
        "_sandbox_venv_retry_at": 0.0, "_data_stack_ready": False,
        "_data_stack_lock": None, "_data_stack_retry_at": 0.0,
    }.items():
        monkeypatch.setattr(cb, name, value)


def test_staging_warmup_bootstraps_venv_before_packages():
    http = Sandbox()
    assert asyncio.run(cb.ensure_data_stack(http)) is True
    assert "VENV_OK" in http.commands[0]
    assert "DATA_STACK_OK" in http.commands[1]
    assert asyncio.run(cb.ensure_data_stack(http)) is True
    assert len(http.commands) == 2


def test_concurrent_callers_wait_for_the_same_install():
    async def run():
        http = Sandbox()
        http.started, http.finish = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(cb.ensure_data_stack(http))
        await http.started.wait()
        second = asyncio.create_task(cb.ensure_data_stack(http))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert not cb._data_stack_ready
        http.finish.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert len(http.commands) == 2
    asyncio.run(run())


def test_concurrent_venv_creation_runs_once():
    async def run():
        http = Sandbox()
        assert await asyncio.gather(cb._ensure_venv(http), cb._ensure_venv(http)) == [True, True]
        assert len(http.commands) == 1
    asyncio.run(run())


def test_failed_package_install_retries_after_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cb.time, "monotonic", lambda: clock[0])
    http = Sandbox()
    http.fail_install = True
    assert asyncio.run(cb.ensure_data_stack(http)) is False
    http.fail_install = False
    assert asyncio.run(cb.ensure_data_stack(http)) is False
    assert len(http.commands) == 2
    clock[0] += 60
    assert asyncio.run(cb.ensure_data_stack(http)) is True
    assert len(http.commands) == 3


@pytest.mark.parametrize("exit_code,status", [(1, 200), (0, 503)])
def test_failure_with_success_marker_does_not_claim_ready(exit_code, status):
    class Broken:
        async def post(self, *args, **kwargs):
            return Response("VENV_OK\n", exit_code, status)
    assert asyncio.run(cb._ensure_venv(Broken())) is False
    assert cb._sandbox_venv_ready is False


def test_venv_failure_can_recover(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cb.time, "monotonic", lambda: clock[0])

    class Recovering(Sandbox):
        broken = True
        async def post(self, *args, **kwargs):
            if self.broken:
                raise OSError("temporarily offline")
            return await super().post(*args, **kwargs)

    http = Recovering()
    assert asyncio.run(cb.ensure_data_stack(http)) is False
    http.broken = False
    clock[0] += 60
    assert asyncio.run(cb.ensure_data_stack(http)) is True


def test_failed_venv_stops_custom_tool_execution():
    class Broken:
        async def post(self, *args, **kwargs):
            return Response(exit_code=1)
    with pytest.raises(RuntimeError, match="Python environment"):
        asyncio.run(cb.run_custom_tool_code(Broken(), "print('do not run')"))


def test_optional_package_failure_does_not_block_standard_library_code():
    class Events:
        async def emit(self, *args):
            pass
    http = Sandbox()
    http.fail_install = True
    output = asyncio.run(cb.run_codebox_tool(
        "execute_code", {"code": "print(1 + 1)"}, http=http, events=Events(), conv_id="test"))
    assert "SUCCESS" in output
    assert "script output" in output
    assert len(http.commands) == 3


@pytest.mark.parametrize("language", ["python", "javascript"])
def test_concurrent_scripts_keep_distinct_content_and_clean_up(tmp_path, language):
    interpreter = sys.executable if language == "python" else shutil.which("node")
    if not interpreter:
        pytest.skip("node is not installed")

    async def invoke(label):
        if language == "python":
            code = f"import time\nprint(__file__, flush=True)\ntime.sleep(0.05)\nprint({label!r})\nraise SystemExit(7)"
        else:
            code = f"console.log(__filename); setTimeout(() => {{console.log({json.dumps(label)}); process.exit(7);}}, 50);"
        command = cb._script_command(code, interpreter, "py" if language == "python" else "js")
        command = command.replace("cd /root", f"cd {shlex.quote(str(tmp_path))}", 1)
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 7, stderr.decode()
        path, result = stdout.decode().splitlines()
        assert result == label
        assert not Path(path).exists()
        return path

    async def run():
        return await asyncio.gather(invoke("first's $value"), invoke('second "value"'))

    paths = asyncio.run(run())
    assert paths[0] != paths[1]
