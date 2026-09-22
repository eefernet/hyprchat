"""Opaque, job-scoped references to immutable browser evidence."""
import hashlib
import json
from pathlib import Path


def register(store, job_id, path, *, revision="", kind=""):
    path = Path(path).resolve()
    roots = [(store.root/"checks"/job_id).resolve(),(store.root/"diagnostics"/job_id).resolve()]
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise ValueError("Evidence does not belong to this job")
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source,"sha256").hexdigest()
    identity = hashlib.sha256((str(path)+checksum).encode()).hexdigest()
    directory = store.root/"evidence"/job_id; directory.mkdir(parents=True,exist_ok=True)
    record = {"id":identity,"path":str(path),"sha256":checksum,"revision_id":revision,"kind":kind,"size":path.stat().st_size}
    (directory/(identity+".json")).write_text(json.dumps(record))
    return {k:v for k,v in record.items() if k!="path"}


def resolve(store,job_id,identity):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]+",job_id) or not re.fullmatch(r"[a-f0-9]{64}",identity):
        raise ValueError("Invalid evidence identity")
    record = json.loads((store.root/"evidence"/job_id/(identity+".json")).read_text())
    path=Path(record["path"]).resolve()
    if not any(path.is_relative_to((store.root/category/job_id).resolve()) for category in ("checks","diagnostics")):
        raise ValueError("Evidence does not belong to this job")
    with path.open("rb") as source:
        if hashlib.file_digest(source,"sha256").hexdigest()!=record["sha256"]:
            raise ValueError("Evidence changed since verification")
    return record


def attach(store,job_id,result,revision=""):
    for key,kind in (("screenshots","screenshot"),("traces","trace")):
        for item in result.get(key,[]):
            item["evidence"] = register(store,job_id,item["path"],revision=revision,kind=kind)
    return result
