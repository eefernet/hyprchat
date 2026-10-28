// Hugging Face → Ollama compatibility report for the Model Manager.
// Pure: no React, no fetch. `compat` is the optional `/api/hf/compat` payload for
// the selected main file ({tag, pull_name, classification, header, manifest, ollama}).
// Levels: bad > warn > info; `flagged` (level !== 'ok') drives "compatibility
// flagged" / "Download anyway". Facts about the selection are `info`, not warnings.

export const isMmproj = (n = "") => /^mmproj/i.test(n) || /\.mmproj/i.test(n);
export const isDraft = (n = "") => /(^|[-_.])draft([-_.]|$)/i.test(n.replace(/\.gguf$/i, ""));
export const isNoMtp = (n = "") => /nomtp/i.test(n);
export const splitPart = (n = "") => { const m = n.match(/-(\d{5})-of-(\d{5})(?=\.gguf$|$)/i); return m ? { part: +m[1], of: +m[2] } : null; };
export const fmtBytes = (b) => { if (!b) return "—"; if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`; if (b >= 1e6) return `${(b / 1e6).toFixed(0)} MB`; return `${(b / 1e3).toFixed(0)} KB`; };

const RUNTIME_RX = /not supported in ollama|ollama[^.\n]{0,80}not supported|requires[^.\n]{0,80}llama\.cpp|llama\.cpp[^.\n]{0,80}only|requires[^.\n]{0,80}pull request|custom architecture/;
const ARCH_RISK_RX = /gated deltanet|linear attention|state space|hybrid architecture|hybrid[^.\n]{0,50}attention|\bssm\b|\bmamba\b|\brwkv\b|\bjamba\b/;
const VISION_RX = /\bmmproj\b|vision encoder|multimodal|vision support|image\/video|image-to-text|image text/;
const GB = 1024 * 1024 * 1024;

export function hfCompatibilityReport({ model, info, readme, selectedFiles = [], compat = null } = {}) {
  const files = info?.gguf_files || [];
  const selected = files.filter((f) => selectedFiles.includes(f.name));
  const text = [model?.id || "", model?.pipeline_tag || "", (model?.tags || []).join(" "), readme || "", files.map((f) => f.name).join(" ")].join(" ").toLowerCase();
  const has = (rx) => rx.test(text);
  const issues = [];
  const add = (level, label, detail) => { if (!issues.some((i) => i.label === label)) issues.push({ level, label, detail }); };

  if (info && files.length === 0) add("bad", "No GGUF files", "This repo does not expose Ollama-loadable GGUF files.");
  if (has(RUNTIME_RX)) add("bad", "Runtime support warning", "The README suggests this may require a specific llama.cpp build or is not supported by Ollama.");

  // Architecture: a real check against what this Ollama already runs; README keywords only as a fallback.
  const arch = compat?.header?.architecture || null;
  const supports = compat?.ollama?.supports_arch;
  const installed = compat?.ollama?.architectures || [];
  if (supports === true) add("info", `Architecture ${arch}`, `This Ollama already runs ${arch} models, so the loader knows this layout.`);
  else if (supports === false) add("warn", `Architecture ${arch}`, `No installed model uses ${arch} on this Ollama${installed.length ? ` (installed: ${installed.join(", ")})` : ""}; the pull may succeed while loading fails.`);
  else if (has(ARCH_RISK_RX)) add("warn", "Architecture risk", "Download may succeed while the local Ollama loader fails to load the blob.");

  const mains = selected.filter((f) => !isMmproj(f.name));
  const repoHasProjector = files.some((f) => isMmproj(f.name));
  const manifestProjector = (compat?.manifest?.projector_bytes || 0) > 0;
  const projectorBytes = compat?.manifest?.projector_bytes || files.find((f) => isMmproj(f.name))?.size || 0;

  if (selected.length) {
    const projectorSelected = selected.some((f) => isMmproj(f.name));
    const anyDraft = mains.some((f) => isDraft(f.name));
    if (selected.every((f) => isMmproj(f.name))) add("bad", "Projector only", "A mmproj file is not a standalone chat model; select a main GGUF too.");
    else if (projectorSelected) add("info", "Includes projector", "Main GGUF and projector are registered together, so images work when the runtime supports them.");
    else if (manifestProjector || repoHasProjector) add(supports === false ? "warn" : "info", "Projector bundled", `Hugging Face attaches this repo's mmproj (${fmtBytes(projectorBytes)}) to every pull; text chat works either way and images work when the architecture supports them.`);
    else if (has(VISION_RX)) add("info", "Text-only build", "The README mentions vision but this repo ships no projector, so the model answers text only.");

    if (anyDraft) add("bad", "Draft model", "This is a speculative-decoding draft head, not a standalone chat model. Pick a main GGUF unless you know you need the draft.");

    const mtp = anyDraft ? null : mains.some((f) => isNoMtp(f.name)) ? "stripped" : (compat?.classification?.mtp === "fused" || (compat?.header?.mtp_layers || 0) > 0) ? "fused" : null;
    if (mtp === "stripped") add("info", "MTP head stripped", "The multi-token-prediction head was removed: a smaller file with identical answers; Ollama does not use the MTP head anyway.");
    else if (mtp === "fused") add("info", "MTP head retained", "The multi-token-prediction tensors are kept (slightly larger file); Ollama loads the model and ignores the extra head.");

    const parts = mains.filter((f) => splitPart(f.name));
    if (parts.length) {
      const of = splitPart(parts[0].name).of;
      if (parts.length < of) add("warn", "Split GGUF", `Only ${parts.length} of ${of} parts selected; select every part before downloading.`);
      else add("info", "Split GGUF", `All ${of} parts selected.`);
    }
    if (mains.some((f) => /\b(BF16|F16|F32)\b/i.test(f.name))) add("warn", "Large full-precision file", "BF16/F16/F32 downloads are large and much more likely to exceed VRAM.");

    const selectedSize = selected.reduce((a, f) => a + (f.size || 0), 0);
    const pullBytes = compat?.manifest ? (compat.manifest.model_bytes || 0) + (compat.manifest.projector_bytes || 0) : selectedSize;
    if (pullBytes > 42 * GB) add("warn", "Very large pull", `${fmtBytes(pullBytes)} will be pulled; this may exceed available VRAM even if it downloads.`);
    else if (pullBytes > 24 * GB) add("info", "Large pull", `${fmtBytes(pullBytes)} will be pulled; load time and VRAM pressure may be high.`);
  } else if (files.some((f) => splitPart(f.name))) {
    add("info", "Split GGUF", "Select every part of a split set before downloading.");
  }

  const level = issues.some((i) => i.level === "bad") ? "bad" : issues.some((i) => i.level === "warn") ? "warn" : "ok";
  const pull = compat ? {
    tag: compat.tag || null,
    pullName: compat.pull_name || null,
    modelBytes: compat.manifest?.model_bytes || 0,
    projectorBytes: compat.manifest?.projector_bytes || 0,
    resolved: !!compat.manifest,
  } : null;
  return { level, issues, flagged: level !== "ok", pull };
}
