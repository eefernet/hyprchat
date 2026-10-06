"""Bounded Office package inspection shared by RAG and the document worker.

No Office applications or model calls are needed to extract text. Targets are
stable within an immutable source hash, not across subsequent revisions.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
LEGACY_EXTS = {".doc", ".ppt", ".xls"}
MAX_BYTES = 50 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}


def xml(raw: bytes):
    # Reject DTDs before parsing, including UTF-16/32 representations.
    upper = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("Document XML contains a prohibited DTD/entity")
    return ET.fromstring(raw)


def package(path: str | Path) -> dict[str, bytes]:
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Document exceeds the 50 MB limit")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError("Office package exceeds expansion limits")
            names = set()
            for i in infos:
                name = i.filename
                if (name in names or name.startswith("/") or "\\" in name
                        or ".." in name.split("/") or i.flag_bits & 1
                        or (i.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("Unsafe or encrypted Office package")
                names.add(name)
                if i.file_size > 64 * 1024 * 1024 or i.file_size > max(1, i.compress_size) * 1000:
                    raise ValueError("Office package member exceeds expansion limits")
            if "[Content_Types].xml" not in names:
                raise ValueError("Not an Office Open XML package")
            parts = {i.filename: archive.read(i) for i in infos if not i.is_dir()}
    except zipfile.BadZipFile as e:
        raise ValueError("Invalid or encrypted Office file") from e
    types = parts["[Content_Types].xml"].lower()
    if b"macroenabled" in types or any("vbaproject" in n.lower() for n in parts):
        raise ValueError("Macro-enabled documents are not supported")
    ext = path.suffix.lower()
    root = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}.get(ext)
    if not root or root not in parts:
        raise ValueError("File extension does not match its Office package")
    for name, raw in parts.items():
        if name.endswith((".xml", ".rels")):
            xml(raw)
    return parts


def relationships(parts, part):
    relpath = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relpath not in parts:
        return {}
    result = {}
    for rel in xml(parts[relpath]):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(part), target)) if not target.startswith("/") else target[1:]
        if resolved.startswith("../"):
            raise ValueError("Invalid package relationship")
        result[rel.get("Id")] = resolved
    return result


def slide_paths(parts):
    root = xml(parts["ppt/presentation.xml"])
    rels = relationships(parts, "ppt/presentation.xml")
    return [rels[n.get("{" + NS["r"] + "}id")] for n in root.findall("p:sldIdLst/p:sldId", NS)]


def sheet_paths(parts):
    rels = relationships(parts, "xl/workbook.xml")
    root = xml(parts["xl/workbook.xml"])
    return [{"name": n.get("name"), "part": rels[n.get("{" + NS["r"] + "}id")],
             "state": n.get("state", "visible")} for n in root.findall("s:sheets/s:sheet", NS)]


def texts(node, prefix):
    return "".join(n.text or "" for n in node.findall(".//" + prefix + ":t", NS))


def inspect_document(path, *, offset=0, limit=200, sheet="", cell_range="", _extract=False):
    path = Path(path)
    parts = package(path)
    items, sheets, warnings, shapes = [], [], [], []
    ext = path.suffix.lower()
    if any("embeddings/" in n or "activeX/" in n or "diagrams/" in n for n in parts):
        warnings.append("Embedded/advanced objects are preserved but are not editable by this tool.")
    if any(b'TargetMode="External"' in v for k, v in parts.items() if k.endswith(".rels")):
        warnings.append("External links are not fetched or refreshed.")
    if ext == ".docx":
        order = ["word/document.xml"] + sorted(n for n in parts if re.match(r"word/(header|footer|footnotes|endnotes)\d*\.xml$", n))
        for name in order:
            root = xml(parts[name])
            for i, node in enumerate(root.findall(".//w:p", NS)):
                items.append({"target": f"{name}#p:{i}", "text": texts(node, "w")})
    elif ext == ".pptx":
        for idx, name in enumerate(slide_paths(parts)):
            root = xml(parts[name])
            for shape in root.findall("p:cSld/p:spTree/*", NS):
                if shape.tag not in {"{" + NS["p"] + "}" + tag for tag in ("sp", "pic", "graphicFrame", "cxnSp", "grpSp")}:
                    continue
                identity = shape.find(".//p:cNvPr", NS)
                if identity is not None:
                    shapes.append({"target": f'{name}#shape:{identity.get("id")}', "slide": idx + 1, "name": identity.get("name", "")})
            for i, node in enumerate(root.findall(".//a:p", NS)):
                items.append({"target": f"{name}#p:{i}", "slide": idx + 1, "text": texts(node, "a")})
            for note in relationships(parts, name).values():
                if note.startswith("ppt/notesSlides/") and note.endswith(".xml"):
                    for i, node in enumerate(xml(parts[note]).findall(".//a:p", NS)):
                        items.append({"target": f"{note}#p:{i}", "slide": idx + 1, "notes": True, "text": texts(node, "a")})
    else:
        shared = []
        if "xl/sharedStrings.xml" in parts:
            shared = [texts(n, "s") for n in xml(parts["xl/sharedStrings.xml"])]
        sheets = sheet_paths(parts)
        if sheet and sheet not in {s["name"] for s in sheets}:
            raise ValueError("Worksheet not found")
        bounds = range_bounds(cell_range) if cell_range else None
        for sh in sheets:
            if sheet and sh["name"] != sheet:
                continue
            for cell in xml(parts[sh["part"]]).findall(".//s:sheetData/s:row/s:c", NS):
                address = cell.get("r", "")
                if bounds:
                    col, row = cell_coords(address)
                    if not (bounds[0] <= col <= bounds[2] and bounds[1] <= row <= bounds[3]):
                        continue
                val = cell.find("s:v", NS)
                value = val.text if val is not None else None
                if cell.get("t") == "s" and value is not None:
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = texts(cell, "s")
                formula = cell.find("s:f", NS)
                items.append({"target": f'{sh["part"]}#cell:{address}', "sheet": sh["name"], "cell": address,
                              "value": value, "formula": "=" + (formula.text or "") if formula is not None else None,
                              "cached_value": value if formula is not None else None})
        warnings.append("Formula values are source caches and may be stale; PDF previews recalculate a separate copy.")
    images = [{"target": n, "size_bytes": len(v)} for n, v in parts.items() if "/media/" in n]
    charts = [{"target": n, "series": [{"labels": [v.text for v in s.findall(".//c:strCache/c:pt/c:v", NS)],
                                       "values": [v.text for v in s.findall(".//c:val/c:numRef/c:numCache/c:pt/c:v", NS)]}
                                      for s in xml(parts[n]).findall(".//c:ser", NS)]}
              for n in parts if re.match(r"(ppt|xl|word)/charts/chart\d+\.xml$", n)]
    offset, limit = max(0, int(offset)), max(1, min(200, int(limit)))
    if _extract:
        return items
    return {"format": ext[1:], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "items": items[offset:offset + limit], "total_items": len(items),
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "sheets": sheets, "images": images, "shapes": shapes, "charts": charts, "warnings": warnings}


def cell_coords(address):
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", address.upper())
    if not match:
        raise ValueError("Invalid cell address")
    col = 0
    for c in match[1]:
        col = col * 26 + ord(c) - 64
    row = int(match[2])
    if col > 16384 or row > 1048576:
        raise ValueError("Cell exceeds Excel limits")
    return col, row


def range_bounds(value):
    cells = value.upper().split(":")
    if len(cells) > 2:
        raise ValueError("Invalid cell range")
    c1, r1 = cell_coords(cells[0])
    c2, r2 = cell_coords(cells[-1])
    if c2 < c1 or r2 < r1:
        raise ValueError("Invalid cell range")
    return c1, r1, c2, r2


def extract_text(path, max_chars=500000):
    # Parse once, even when extracting a long document for knowledge-base indexing.
    out, size = [], 0
    for item in inspect_document(path, _extract=True):
        value = item.get("formula") if item.get("formula") is not None else item.get("value")
        text = item.get("text") if "text" in item else f'{item["sheet"]}!{item["cell"]}: {value if value is not None else ""}'
        out.append(text)
        size += len(text) + 1
        if size >= max_chars:
            break
    return "\n".join(out)[:max_chars]
