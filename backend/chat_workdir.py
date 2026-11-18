"""The folder a chat's scripts and shell commands run in on the sandbox VM.

Each chat already has its own private /root there (hephaestus_scope.py maps it per conversation). Inside
it everything a model made landed loose in /root itself, next to venv/, the uploaded files and the
tool caches. Scripts and commands now start in /root/work, so a file written with a relative name
(`open("index.html", "w")`) goes there, and a multi-file result goes in its own subfolder of it.

Absolute paths are left alone: uploads stay under /root/chat_files/, the shared interpreter under
/root/venv/, and a model that writes /root/x on purpose still can.
"""
import posixpath
import shlex

WORK = "/root/work"

# Shell prefix: create the folder on first use and start there.
ENTER = f"mkdir -p {WORK} && cd {WORK}"

# One sentence for tool descriptions and hints, so every tool says the same thing.
HINT = (f"The working directory is {WORK}/ (already the current directory): use relative paths, and put a "
        f"result made of several files in its own subfolder ({WORK}/<short-name>/). "
        "Files the user attached are under /root/chat_files/.")


def resolve(path, default: str = WORK) -> str:
    """A path a tool was given, as an absolute sandbox path. Relative paths are relative to WORK."""
    text = str(path or "").strip()
    if not text:
        return default
    if text.startswith("~/"):
        text = "/root/" + text[2:]
    if text.startswith("/"):
        return text
    return posixpath.normpath(posixpath.join(WORK, text))


def in_work(command: str) -> str:
    """A shell command, run from the work folder."""
    return f"{ENTER} && {command}"


def fallback(path) -> str | None:
    """Where else to look when /root/<name> does not exist: models were told for a long time that the
    working directory is /root, and still ask for /root/output.png after writing output.png."""
    text = str(path or "")
    if not text.startswith("/root/") or text.startswith(WORK + "/"):
        return None
    rest = text[len("/root/"):]
    if not rest or rest.split("/", 1)[0] in ("chat_files", "venv", "projects", "skills", ".cache", ".local", ".config"):
        return None
    return posixpath.join(WORK, rest)


def exists_command(path) -> str:
    return f"test -e {shlex.quote(str(path))}"
