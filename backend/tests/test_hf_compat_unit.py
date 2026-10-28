"""Offline unit tests for hf_compat.py — pull tags, file classification, GGUF
header parsing and HF registry manifest parsing. No server, no network.
Run: python -m pytest tests/test_hf_compat_unit.py -v
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hf_compat  # noqa: E402

REPO = [
    "Qwen3.8-27B-Uncensored-IQ2_M.gguf",
    "Qwen3.8-27B-Uncensored-Q4_K_M.gguf",
    "Qwen3.8-27B-Uncensored-Q8_0.gguf",
    "Qwen3.8-27B-Uncensored-draft-Q4_0.gguf",
    "Qwen3.8-27B-Uncensored-draft-Q8_0.gguf",
    "Qwen3.8-27B-Uncensored-imatrix.dat",
    "Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf",
    "Qwen3.8-27B-Uncensored-noMTP-Q8_0.gguf",
    "mmproj-Qwen3.8-27B-Uncensored-F16.gguf",
    "README.md",
]


# ── pull_tag ──

def test_pull_tag_uses_exact_filename_when_ollama_accepts_it():
    assert hf_compat.pull_tag("Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf") == "Qwen3.8-27B-Uncensored-noMTP-Q4_K_M"
    assert hf_compat.pull_tag("Qwen3.8-27B-Uncensored-draft-Q8_0.gguf") == "Qwen3.8-27B-Uncensored-draft-Q8_0"
    assert hf_compat.pull_tag("mmproj-Qwen3.8-27B-Uncensored-F16.gguf") == "mmproj-Qwen3.8-27B-Uncensored-F16"


def test_pull_tag_strips_split_suffix_so_every_part_maps_to_one_tag():
    assert hf_compat.pull_tag("Big-Q8_0-00001-of-00003.gguf") == "Big-Q8_0"
    assert hf_compat.pull_tag("Big-Q8_0-00002-of-00003.gguf") == "Big-Q8_0"


def test_pull_tag_falls_back_to_quant_when_name_is_not_a_valid_tag():
    long_name = "x" * 81 + "-Q4_K_M.gguf"
    assert hf_compat.pull_tag(long_name) == "Q4_K_M"
    assert hf_compat.pull_tag("weird+name-Q5_K_M.gguf") == "Q5_K_M"
    assert hf_compat.pull_tag("-leading-dash-Q4_0.gguf") == "Q4_0"


def test_pull_tag_none_without_quant_or_valid_name():
    assert hf_compat.pull_tag("weird+name.gguf") is None
    assert hf_compat.pull_tag("") is None


# ── classify_file ──

def test_classify_main_mmproj_draft_imatrix():
    assert hf_compat.classify_file("mmproj-Qwen3.8-27B-Uncensored-F16.gguf")["kind"] == "mmproj"
    assert hf_compat.classify_file("Qwen3.8-27B-Uncensored-draft-Q8_0.gguf")["kind"] == "draft"
    assert hf_compat.classify_file("Qwen3.8-27B-Uncensored-imatrix.dat")["kind"] == "imatrix"
    main = hf_compat.classify_file("Qwen3.8-27B-Uncensored-Q4_K_M.gguf")
    assert main["kind"] == "main" and main["quant"] == "Q4_K_M" and main["split"] is None


def test_classify_mtp_variants_need_the_sibling_list():
    stripped = hf_compat.classify_file("Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf", REPO)
    fused = hf_compat.classify_file("Qwen3.8-27B-Uncensored-Q4_K_M.gguf", REPO)
    lonely = hf_compat.classify_file("Qwen3.8-27B-Uncensored-IQ2_M.gguf", REPO)
    assert stripped["mtp"] == "stripped"
    assert fused["mtp"] == "fused"
    assert lonely["mtp"] is None  # no noMTP twin for IQ2_M in this fixture
    assert hf_compat.classify_file("Qwen3.8-27B-Uncensored-Q4_K_M.gguf")["mtp"] is None


def test_classify_split_parts():
    c = hf_compat.classify_file("Big-Q8_0-00002-of-00003.gguf")
    assert c["split"] == {"part": 2, "of": 3} and c["quant"] == "Q8_0"


# ── GGUF header ──

def _gguf(kv, n_tensors=3):
    def s(x):
        b = x.encode(); return struct.pack("<Q", len(b)) + b
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", n_tensors) + struct.pack("<Q", len(kv))
    for key, (vtype, val) in kv:
        out += s(key) + struct.pack("<I", vtype)
        if vtype == 8:
            out += s(val)
        elif vtype == 4:
            out += struct.pack("<I", val)
        elif vtype == 9:  # array of strings
            out += struct.pack("<I", 8) + struct.pack("<Q", len(val)) + b"".join(s(v) for v in val)
        elif vtype == 7:
            out += struct.pack("<B", 1 if val else 0)
    return out


def test_parse_gguf_header_reads_arch_and_stops_before_the_vocab():
    data = _gguf([
        ("general.architecture", (8, "qwen35")),
        ("general.type", (8, "model")),
        ("general.name", (8, "Abl 27b")),
        ("qwen35.context_length", (4, 262144)),
        ("qwen35.block_count", (4, 65)),
        ("qwen35.nextn_predict_layers", (4, 1)),
        ("tokenizer.ggml.tokens", (9, ["a", "b", "c"])),
    ])
    # Truncate inside the vocab: the parser must have stopped before needing it.
    truncated = data[:-10]
    h = hf_compat.parse_gguf_header(truncated)
    assert h["architecture"] == "qwen35" and h["type"] == "model" and h["name"] == "Abl 27b"
    assert h["context_length"] == 262144 and h["block_count"] == 65 and h["mtp_layers"] == 1
    assert h["n_tensors"] == 3


def test_parse_gguf_header_mmproj_and_bad_magic():
    data = _gguf([("general.architecture", (8, "clip")), ("general.type", (8, "mmproj")),
                  ("clip.has_vision_encoder", (7, True))])
    h = hf_compat.parse_gguf_header(data)
    assert h["architecture"] == "clip" and h["type"] == "mmproj" and h["context_length"] is None
    try:
        hf_compat.parse_gguf_header(b"GGML" + b"\0" * 32)
    except ValueError as e:
        assert "not a GGUF" in str(e)
    else:
        raise AssertionError("bad magic must raise")


# ── manifest ──

def test_parse_manifest_sums_model_and_projector_layers():
    doc = {"schemaVersion": 2, "layers": [
        {"mediaType": "application/vnd.ollama.image.model", "size": 16_550_000_000},
        {"mediaType": "application/vnd.ollama.image.projector", "size": 930_000_000},
        {"mediaType": "application/vnd.ollama.image.template", "size": 2000},
    ]}
    m = hf_compat.parse_manifest(doc)
    assert m["model_bytes"] == 16_550_000_000 and m["projector_bytes"] == 930_000_000
    assert [l["type"] for l in m["layers"]] == ["model", "projector", "template"]
    assert hf_compat.parse_manifest({"error": "tag not available"}) is None


def test_tag_candidates_order_and_validity():
    assert hf_compat.tag_candidates("Qwen3.8-27B-Uncensored-draft-Q8_0.gguf") == [
        "Qwen3.8-27B-Uncensored-draft-Q8_0", "Qwen3.8-27B-Uncensored-draft-Q8_0.gguf", "Q8_0"]
    assert hf_compat.tag_candidates("x" * 81 + "-Q4_K_M.gguf") == ["Q4_K_M"]
    assert hf_compat.tag_candidates("Big-Q8_0-00002-of-00003.gguf")[0] == "Big-Q8_0"
