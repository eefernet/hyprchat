"""Where a delivered file is stored on this host.

    data/sandbox/outputs/<YYYY-MM>/<chat id>/<file name>

Every tool used to write straight into data/sandbox/outputs. The folder was flat, mixed every chat and
user, and `download_file` wrote the bare file name: `index.html` from one chat overwrote `index.html`
from another, and both chats' download links then served the newer file (three links on the live
server did, 2026-11-16).

`place()` gives each chat a folder per month, never overwrites inside it, and gives each delivery its
own download address. The name people see and download stays the plain one (the artifact row's
`filename`); the address and the name on disk are details.

Files written before this stay where they are and keep working: saved rows point at their durable copy
(durable_storage.py), and the by-name fallbacks call `find()` below.
"""
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config

IMAGE_STUDIO = "image-studio"   # images made in Image Studio, outside any chat
LIBRARY = "library"             # revisions and bundles made in Artifact Studio from files with no chat
UNSORTED = "unsorted"           # no chat known

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def folder_name(conversation_id) -> str:
    """A chat id (or one of the bucket names above) as one safe path segment."""
    name = _UNSAFE.sub("-", str(conversation_id or "")).strip(".-")[:80]
    return name or UNSORTED


def clean_name(filename) -> str:
    """The last path segment, without control characters; never empty, `.` or `..`."""
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable())
    if name in ("", ".", ".."):
        name = "file"
    stem, ext = os.path.splitext(name)
    return stem[:150] + ext[:20]


@dataclass(frozen=True)
class Placement:
    path: str        # absolute path to write
    url_name: str    # last segment of /api/downloads/<url_name>: unique per delivery
    filename: str    # the name to show and to download as

    @property
    def url(self) -> str:
        return f"/api/downloads/{self.url_name}"


def place(conversation_id, filename, *, keep_name: bool = False, when: datetime | None = None) -> Placement:
    """Reserve a path for a file a chat delivers.

    keep_name=True is for callers whose names are already unique and are looked up by that name later
    (Daedalus archives, generated images, managed documents): the name and the address stay exactly as
    given, only the folder changes. Otherwise a repeated name gets " (2)", " (3)", … and the address
    gets a short random prefix.
    """
    root = Path(config.SANDBOX_OUTPUTS_DIR).resolve()
    month = (when or datetime.now(timezone.utc)).strftime("%Y-%m")
    folder = root / month / folder_name(conversation_id)
    name = clean_name(filename)
    target = folder / name
    if not target.resolve().is_relative_to(root):
        raise ValueError("Output path escapes the outputs folder")
    folder.mkdir(parents=True, exist_ok=True)
    if keep_name:
        return Placement(str(target), name, name)
    stem, ext = os.path.splitext(name)
    count = 2
    while target.exists():
        target = folder / f"{stem} ({count}){ext}"
        count += 1
    address = f"{uuid.uuid4().hex[:8]}-{_UNSAFE.sub('-', name).strip('-') or 'file'}"
    return Placement(str(target), address, name)


def find(name) -> str | None:
    """A stored file by its exact name: the flat folder first (files from before this layout), then one
    chat folder deep. For rows that carry no storage path; new rows always carry one."""
    safe = os.path.basename(str(name or ""))
    if not safe or safe != name:
        return None
    root = Path(config.SANDBOX_OUTPUTS_DIR)
    flat = root / safe
    if flat.is_file() and not flat.is_symlink():
        return str(flat)
    if not root.is_dir():
        return None
    for month in sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")), reverse=True):
        for chat in month.iterdir():
            candidate = chat / safe
            if chat.is_dir() and not chat.is_symlink() and candidate.is_file() and not candidate.is_symlink():
                return str(candidate)
    return None


def usage() -> tuple[int, int]:
    """(files, bytes) under the outputs folder, subfolders included. Leased temporary files are not counted."""
    root = Path(config.SANDBOX_OUTPUTS_DIR)
    files = size = 0
    if not root.is_dir():
        return files, size
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            try:
                size += os.path.getsize(path)
                files += 1
            except OSError:
                pass
    return files, size


_BUCKET_KIND = {IMAGE_STUDIO: "image_studio", LIBRARY: "library", UNSORTED: "unsorted"}


def summary() -> dict:
    """What the Storage settings page shows: how many delivered files there are, how they are spread
    over months and chats, and how many still sit loose at the top (none, once sorted). The page used
    to count the top level only, and read 0 the moment the files were organized into folders."""
    root = Path(config.SANDBOX_OUTPUTS_DIR)
    blank = lambda: {"files": 0, "bytes": 0}
    result = {"files": 0, "bytes": 0, "chat_folders": 0, "loose": blank(),
              "kinds": {"chats": blank(), "image_studio": blank(), "library": blank(), "unsorted": blank()}, "months": []}
    if not root.is_dir() or root.is_symlink():
        return result

    def add(target, size):
        target["files"] += 1
        target["bytes"] += size

    def size_of(path):
        try:
            return path.stat().st_size if path.is_file() and not path.is_symlink() else None
        except OSError:
            return None

    months = {}
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue                                  # leased temporary files are reported by the cleanup preview
        size = size_of(entry)
        if size is not None:
            add(result["loose"], size); add(result, size)
            continue
        if not entry.is_dir() or entry.is_symlink():
            continue
        month = months.setdefault(entry.name, {"month": entry.name, "files": 0, "bytes": 0, "chats": 0})
        for folder in entry.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                size = size_of(folder)
                if size is not None:
                    add(month, size); add(result, size); add(result["kinds"]["unsorted"], size)
                continue
            kind = _BUCKET_KIND.get(folder.name, "chats")
            held = 0
            for base, _dirs, names in os.walk(folder):
                for name in names:
                    size = size_of(Path(base) / name)
                    if size is None:
                        continue
                    held += 1
                    add(month, size); add(result, size); add(result["kinds"][kind], size)
            if kind == "chats" and held:
                month["chats"] += 1
                result["chat_folders"] += 1
    result["months"] = sorted((m for m in months.values() if m["files"]), key=lambda m: m["month"], reverse=True)
    return result
