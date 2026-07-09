"""AI selection edits for the Artifact Canvas editor.

Takes the editor's current document, a selection range, and a user
instruction; returns replacement text for exactly that range. Used by
`POST /api/artifacts/{id}/ai-edit` (routes/artifacts.py). The document
never leaves the request/response cycle — saving is a separate, explicit
`/revise` call from the frontend, so a bad AI edit can always be rejected
in the diff preview without touching the artifact.
"""

import re

import config
import model_providers

# Context shown around the selection so edits stay stylistically coherent
# without shipping a 500KB document into the prompt.
_CTX_CHARS = 3000
_MAX_SELECTION_CHARS = 48000
_MAX_INSTRUCTION_CHARS = 2000


class CanvasEditError(Exception):
    pass


def _strip_fences(text: str) -> str:
    """Models love wrapping output in a markdown fence despite instructions."""
    stripped = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_+-]*\n(.*)\n```$", stripped, flags=re.S)
    return m.group(1) if m else text


def build_prompt(content: str, sel_start: int, sel_end: int, instruction: str,
                 filename: str = "") -> str:
    before = content[max(0, sel_start - _CTX_CHARS):sel_start]
    selection = content[sel_start:sel_end]
    after = content[sel_end:sel_end + _CTX_CHARS]
    fname = f" from the file `{filename}`" if filename else ""
    return f"""You are an expert editor. Below is a snippet{fname} with a SELECTED region to rewrite.

TEXT BEFORE SELECTION:
<<<BEFORE
{before}
BEFORE>>>

SELECTED TEXT (rewrite this, and ONLY this):
<<<SELECTED
{selection}
SELECTED>>>

TEXT AFTER SELECTION:
<<<AFTER
{after}
AFTER>>>

INSTRUCTION: {instruction}

Rules:
- Output ONLY the replacement for the SELECTED text — it will be spliced verbatim between the BEFORE and AFTER text.
- Do NOT repeat the BEFORE or AFTER text.
- Do NOT wrap the output in markdown code fences and do NOT add any commentary.
- Preserve the original indentation, formatting style, and trailing newline behavior unless the instruction says otherwise."""


async def ai_edit_selection(http, content: str, sel_start: int, sel_end: int,
                            instruction: str, *, model: str = "",
                            filename: str = "") -> str:
    """Returns the replacement text for content[sel_start:sel_end]."""
    instruction = (instruction or "").strip()[:_MAX_INSTRUCTION_CHARS]
    if not instruction:
        raise CanvasEditError("An edit instruction is required.")
    if content is None:
        raise CanvasEditError("Document content is required.")
    n = len(content)
    sel_start = max(0, min(int(sel_start), n))
    sel_end = max(sel_start, min(int(sel_end), n))
    if sel_end == sel_start:
        # No selection = edit the whole document (bounded).
        sel_start, sel_end = 0, n
    if sel_end - sel_start > _MAX_SELECTION_CHARS:
        raise CanvasEditError(
            f"Selection too large ({sel_end - sel_start} chars, max {_MAX_SELECTION_CHARS}). "
            "Select a smaller region.")

    model = (model or "").strip() or config.DEFAULT_MODEL
    prompt = build_prompt(content, sel_start, sel_end, instruction, filename)
    # Output can be as long as the selection plus growth headroom.
    est_out_tokens = max(1024, min(16384, (sel_end - sel_start) // 2))
    raw = await model_providers.complete_chat(
        http, model, prompt,
        temperature=0.2,
        num_ctx=max(8192, min(32768, (len(prompt) // 4) + est_out_tokens + 1024)),
        num_predict=est_out_tokens,
        timeout=300,
        ollama_url=config.OLLAMA_URL,
    )
    if not (raw or "").strip():
        raise CanvasEditError(f"The model ({model}) returned no edit. Try again or rephrase the instruction.")
    return _strip_fences(raw)
