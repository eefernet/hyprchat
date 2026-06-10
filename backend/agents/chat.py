"""
Chat streaming agent — the core multi-round tool-calling loop.
Extracted from main.py to keep the agent logic isolated.
"""
import asyncio
import json
import os
import re
import traceback
import urllib.parse
from datetime import datetime

import config
import database as db
import model_providers
import rag
from tools import (CODEAGENT_TOOLS, exec_tool, parse_text_tool_calls,
                   strip_tool_calls, _v2_name_match)
from connectors import tool_def_from_connector_tool
from events import inject_text_tool_prompt, parse_tool_params
from quick_search import run_quick_search_for_chat


# Background tasks (best-effort memory suggestion) — held in a set so the
# event loop doesn't garbage-collect them mid-run; discarded on completion.
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (shouldn't happen inside the stream) — drop the coro.
        coro.close()
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


_PERSONA_RATING_GUIDANCE = {
    "G": "G: All-ages only. Avoid profanity, sexual content, graphic violence, drugs, and mature themes.",
    "PG": "PG: Mild jokes, tiny scares, and gentle conflict are allowed. Avoid explicit adult content.",
    "PG-13": "PG-13: Teen-level edge is allowed: mild profanity, flirting, suggestive jokes, and non-graphic mature themes.",
    "R": "R: Adult language and mature themes are allowed, but keep sexual content non-explicit.",
    "NC-17": "NC-17: Adult-only explicit themes may be used for consenting adults. Avoid minors, coercion, illegal content, and non-consensual sexual content.",
    "Unrated": "Unrated (max XXX): Broadest adult setting. Explicit consenting-adult NSFW is allowed within safety boundaries. Avoid minors, coercion, illegal content, and non-consensual sexual content.",
}

_PERSONA_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(user|char)\s*\}\}|\{\s*(user|char)\s*\}",
    re.IGNORECASE,
)


class _ProviderStreamComplete(Exception):
    pass


def _replace_persona_placeholders(text: str, *, user_name: str, char_name: str) -> str:
    if not text:
        return text or ""
    names = {
        "user": (user_name or "User").strip() or "User",
        "char": (char_name or "the character").strip() or "the character",
    }

    def repl(match: re.Match) -> str:
        key = (match.group(1) or match.group(2) or "").lower()
        return names.get(key, match.group(0))

    return _PERSONA_PLACEHOLDER_RE.sub(repl, str(text))


def _normalize_persona_rating(value) -> str:
    s = re.sub(r"[\s_]+", "", str(value or "PG-13").strip().lower())
    if s in {"g", "general", "allages"}:
        return "G"
    if s == "pg":
        return "PG"
    if s in {"pg13", "pg-13", "teen"}:
        return "PG-13"
    if s in {"r", "mature"}:
        return "R"
    if s in {"nc17", "nc-17"}:
        return "NC-17"
    if s in {"unrated", "xxx", "maxxxx"}:
        return "Unrated"
    return "PG-13"


def _normalize_persona_thinking_mode(value) -> str:
    s = str(value or "auto").strip().lower()
    if s in {"on", "true", "enable", "enabled", "1"}:
        return "on"
    if s in {"off", "false", "disable", "disabled", "0"}:
        return "off"
    return "auto"


def _model_config_profile_type(mc: dict, params: dict) -> str:
    raw = params.get("profile_type")
    if raw in {"agent", "persona"}:
        return raw
    name = (mc.get("name") or "").lower()
    persona = params.get("persona") if isinstance(params.get("persona"), dict) else {}
    persona_keys = ("personality", "scenario", "first_message", "example_dialogue", "lore", "rating")
    if (
        "based bot" in name
        or "gamer bro" in name
        or "tyler" in name
        or any(str(persona.get(k) or params.get(k) or "").strip() for k in persona_keys)
    ):
        return "persona"
    return "agent"


def _persona_rating_guidance(params: dict) -> str:
    persona = params.get("persona") if isinstance(params.get("persona"), dict) else {}
    rating = _normalize_persona_rating(persona.get("rating", params.get("rating")))
    return _PERSONA_RATING_GUIDANCE.get(rating, _PERSONA_RATING_GUIDANCE["PG-13"])


def _extract_json_array(text: str) -> list:
    raw = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _suggest_workspace_memories(
    req,
    http,
    events,
    conv_id: str,
    workspace: dict | None,
    user_text: str,
    assistant_text: str,
    assistant_msg_id: int | None,
):
    if not workspace or not int(workspace.get("memory_enabled") or 0):
        return
    ws_id = workspace.get("id")
    if not ws_id or len((user_text or "") + (assistant_text or "")) < 120:
        return
    # reject_cloud: never POST a cloud-prefixed chat model to Ollama /api/generate
    # (it 404s every turn). Cloud models are honored only via explicit WORKSPACE_MODEL.
    model = (getattr(config, "WORKSPACE_MODEL", "")
             or model_providers.reject_cloud(getattr(req, "model", ""))
             or config.DEFAULT_MODEL)
    prompt = (
        "You extract useful long-term workspace memories from one chat turn.\n"
        "Return ONLY a JSON array with 0-5 objects. Do not include markdown.\n"
        "Each object shape:\n"
        "{\"type\":\"semantic|episodic|procedural\",\"content\":\"one durable memory\","
        "\"importance\":1-5,\"confidence\":0-1,\"entities\":[\"short names\"],"
        "\"reason\":\"why it should be remembered\"}\n\n"
        "Rules:\n"
        "- Suggest only durable, reusable information for this workspace.\n"
        "- semantic = stable facts/preferences/constraints.\n"
        "- episodic = dated decisions, outcomes, blockers, or events.\n"
        "- procedural = repeatable workflows, commands, deployment steps, lessons learned.\n"
        "- Do not save secrets, credentials, private keys, passwords, or raw tokens.\n"
        "- Do not save generic facts that are obvious from the chat app.\n"
        "- Prefer fewer high-value memories over many weak ones.\n\n"
        f"Workspace: {workspace.get('name') or ws_id}\n"
        f"User turn:\n{(user_text or '')[:5000]}\n\n"
        f"Assistant answer:\n{(assistant_text or '')[:7000]}"
    )
    try:
        await events.emit(conv_id, "tool_start", {
            "tool": "memory", "icon": "brain",
            "status": "Reviewing turn for workspace memory suggestions...",
        })
        r = await http.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 700},
            },
            timeout=45,
        )
        if r.status_code != 200:
            await events.emit(conv_id, "tool_done", {
                "tool": "memory", "icon": "brain",
                "status": "Workspace memory suggestion skipped (helper model unavailable).",
            })
            return
        suggestions = _extract_json_array(r.json().get("response", ""))
        if not suggestions:
            await events.emit(conv_id, "tool_done", {
                "tool": "memory", "icon": "brain",
                "status": "No new workspace memories suggested.",
            })
            return
        existing = await db.list_workspace_memories(ws_id, status="all")
        existing_norm = {re.sub(r"\s+", " ", (m.get("content") or "").strip().lower()) for m in existing}
        created = 0
        for item in suggestions[:5]:
            if not isinstance(item, dict):
                continue
            content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
            if len(content) < 12 or len(content) > 800:
                continue
            norm = content.lower()
            if norm in existing_norm:
                continue
            if re.search(r"(password|api[_ -]?key|secret|private key|token)\s*[:=]", content, re.I):
                continue
            try:
                await db.create_workspace_memory(
                    ws_id,
                    content=content,
                    memory_type=item.get("type", "semantic"),
                    status="suggested",
                    importance=item.get("importance", 3),
                    source_conv_id=conv_id,
                    source_conversation_id=conv_id,
                    source_message_id=assistant_msg_id,
                    confidence=item.get("confidence", 0),
                    entities=item.get("entities") if isinstance(item.get("entities"), list) else [],
                    metadata={"reason": item.get("reason", ""), "suggested_by": "workspace_helper"},
                )
                existing_norm.add(norm)
                created += 1
            except Exception as _ce:
                print(f"[CHAT] memory suggestion insert failed: {_ce}")
        await events.emit(conv_id, "tool_done", {
            "tool": "memory", "icon": "brain",
            "status": f"Queued {created} workspace memory suggestion{'s' if created != 1 else ''} for review." if created else "No new workspace memories suggested.",
        })
    except Exception as e:
        print(f"[CHAT] workspace memory suggestion failed (non-fatal): {e}")


async def _suggest_global_memories(
    req,
    http,
    events,
    conv_id: str,
    user_text: str,
    assistant_text: str,
    assistant_msg_id: int | None,
):
    if len((user_text or "") + (assistant_text or "")) < 120:
        return
    # reject_cloud: never POST a cloud-prefixed chat model to Ollama /api/generate
    # (it 404s every turn). Cloud models are honored only via explicit WORKSPACE_MODEL.
    model = (getattr(config, "WORKSPACE_MODEL", "")
             or model_providers.reject_cloud(getattr(req, "model", ""))
             or config.DEFAULT_MODEL)
    prompt = (
        "You extract useful long-term personal memories for a cross-chat AI assistant.\n"
        "Return ONLY a JSON array with 0-5 objects. Do not include markdown.\n"
        "Each object shape:\n"
        "{\"type\":\"semantic|episodic|procedural\",\"content\":\"one durable memory\","
        "\"importance\":1-5,\"confidence\":0-1,\"entities\":[\"short names\"],"
        "\"reason\":\"why it should be remembered\"}\n\n"
        "Rules:\n"
        "- Suggest only durable information useful across many future chats.\n"
        "- Good semantic memories include user preferences, personal background, important people, birthdays, interests, names, and stable constraints.\n"
        "- Good episodic memories include dated user-confirmed events, decisions, or outcomes.\n"
        "- Good procedural memories include reusable workflows or recurring user instructions.\n"
        "- Do not save secrets, credentials, private keys, passwords, raw tokens, or credential-bearing URLs.\n"
        "- Do not infer sensitive facts; only save what the user clearly states or confirms.\n"
        "- Do not save generic assistant claims or one-off trivia.\n"
        "- Prefer fewer high-value memories over many weak ones.\n\n"
        f"User turn:\n{(user_text or '')[:5000]}\n\n"
        f"Assistant answer:\n{(assistant_text or '')[:7000]}"
    )
    try:
        await events.emit(conv_id, "tool_start", {
            "tool": "memory", "icon": "brain",
            "status": "Reviewing turn for HyprChat memory suggestions...",
        })
        r = await http.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 700},
            },
            timeout=45,
        )
        if r.status_code != 200:
            await events.emit(conv_id, "tool_done", {
                "tool": "memory", "icon": "brain",
                "status": "HyprChat memory suggestion skipped (helper model unavailable).",
            })
            return
        suggestions = _extract_json_array(r.json().get("response", ""))
        if not suggestions:
            await events.emit(conv_id, "tool_done", {
                "tool": "memory", "icon": "brain",
                "status": "No new HyprChat memories suggested.",
            })
            return
        existing = await db.list_global_memories(status="all")
        existing_norm = {re.sub(r"\s+", " ", (m.get("content") or "").strip().lower()) for m in existing}
        created = 0
        for item in suggestions[:5]:
            if not isinstance(item, dict):
                continue
            content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
            if len(content) < 12 or len(content) > 800:
                continue
            norm = content.lower()
            if norm in existing_norm:
                continue
            if re.search(r"(password|api[_ -]?key|secret|private key|token)\s*[:=]", content, re.I):
                continue
            try:
                await db.create_global_memory(
                    content=content,
                    memory_type=item.get("type", "semantic"),
                    status="suggested",
                    importance=item.get("importance", 3),
                    source_conv_id=conv_id,
                    source_conversation_id=conv_id,
                    source_message_id=assistant_msg_id,
                    confidence=item.get("confidence", 0),
                    entities=item.get("entities") if isinstance(item.get("entities"), list) else [],
                    metadata={"reason": item.get("reason", ""), "suggested_by": "global_helper"},
                )
                existing_norm.add(norm)
                created += 1
            except Exception as _ce:
                print(f"[CHAT] global memory suggestion insert failed: {_ce}")
        await events.emit(conv_id, "tool_done", {
            "tool": "memory", "icon": "brain",
            "status": f"Queued {created} HyprChat memory suggestion{'s' if created != 1 else ''} for review." if created else "No new HyprChat memories suggested.",
        })
    except Exception as e:
        print(f"[CHAT] global memory suggestion failed (non-fatal): {e}")


# ── Tool-calling templates keyed by model family ──
TOOL_TEMPLATES = {
    "chatml": {
        "label": "ChatML (Qwen2.5 / Qwen3 / most instruct models)",
        "template": (
            '{{- if .System }}<|im_start|>system\n{{- .System }}<|im_end|>\n{{ end }}'
            '{{- range $i, $_ := .Messages }}'
            '{{- $last := eq (len (slice $.Messages $i)) 1 }}'
            '{{- if eq .Role "user" }}<|im_start|>user\n{{- .Content }}<|im_end|>\n'
            '{{- if $last }}<|im_start|>assistant\n{{ end }}'
            '{{- else if eq .Role "assistant" }}<|im_start|>assistant\n'
            '{{- if .Content }}{{ .Content }}'
            '{{- else if .ToolCalls }}<tool_call>\n'
            '{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}\n{{ end }}'
            '</tool_call>{{ end }}'
            '{{- if not $last }}<|im_end|>\n{{ end }}'
            '{{- else if eq .Role "tool" }}<|im_start|>tool\n{{- .Content }}<|im_end|>\n'
            '{{- if $last }}<|im_start|>assistant\n{{ end }}{{ end }}{{- end }}'
        ),
        "stops": ["<|im_start|>", "<|im_end|>"],
    },
    "llama3": {
        "label": "Llama 3 / 3.1 / 3.2 / 3.3",
        "template": (
            '{{- if .System }}<|start_header_id|>system<|end_header_id|>\n\n{{- .System }}<|eot_id|>{{ end }}'
            '{{- range $i, $_ := .Messages }}'
            '{{- $last := eq (len (slice $.Messages $i)) 1 }}'
            '{{- if eq .Role "user" }}<|start_header_id|>user<|end_header_id|>\n\n{{- .Content }}<|eot_id|>'
            '{{- if $last }}<|start_header_id|>assistant<|end_header_id|>\n\n{{ end }}'
            '{{- else if eq .Role "assistant" }}<|start_header_id|>assistant<|end_header_id|>\n\n'
            '{{- if .Content }}{{ .Content }}'
            '{{- else if .ToolCalls }}{"name": "{{ (index .ToolCalls 0).Function.Name }}", "parameters": {{ (index .ToolCalls 0).Function.Arguments }}}{{ end }}'
            '{{- if not $last }}<|eot_id|>{{ end }}'
            '{{- else if eq .Role "tool" }}<|start_header_id|>ipython<|end_header_id|>\n\n{{- .Content }}<|eot_id|>'
            '{{- if $last }}<|start_header_id|>assistant<|end_header_id|>\n\n{{ end }}{{ end }}{{- end }}'
        ),
        "stops": ["<|start_header_id|>", "<|end_header_id|>", "<|eot_id|>"],
    },
    "mistral": {
        "label": "Mistral / Mixtral",
        "template": (
            '{{- if .System }}[INST] {{ .System }} [/INST]\n{{ end }}'
            '{{- range $i, $_ := .Messages }}'
            '{{- $last := eq (len (slice $.Messages $i)) 1 }}'
            '{{- if eq .Role "user" }}[INST] {{ .Content }} [/INST]{{ if $last }} {{ end }}'
            '{{- else if eq .Role "assistant" }} {{ .Content }}'
            '{{- if .ToolCalls }} [TOOL_CALLS] [{"name": "{{ (index .ToolCalls 0).Function.Name }}", "arguments": {{ (index .ToolCalls 0).Function.Arguments }}}]{{ end }}'
            '{{- if not $last }}</s>{{ end }}'
            '{{- else if eq .Role "tool" }} [TOOL_RESULTS] {"content": {{ .Content }}} [/TOOL_RESULTS]{{ end }}{{- end }}'
        ),
        "stops": ["[INST]", "[/INST]", "</s>"],
    },
    "gemma": {
        "label": "Gemma 2 / 3",
        "template": (
            '{{- if .System }}<start_of_turn>user\n{{- .System }}<end_of_turn>\n{{ end }}'
            '{{- range $i, $_ := .Messages }}'
            '{{- $last := eq (len (slice $.Messages $i)) 1 }}'
            '{{- if eq .Role "user" }}<start_of_turn>user\n{{- .Content }}<end_of_turn>\n'
            '{{- if $last }}<start_of_turn>model\n{{ end }}'
            '{{- else if eq .Role "assistant" }}<start_of_turn>model\n{{- .Content }}'
            '{{- if not $last }}<end_of_turn>\n{{ end }}{{ end }}{{- end }}'
        ),
        "stops": ["<start_of_turn>", "<end_of_turn>"],
    },
}


def detect_template_family(model_name: str) -> str:
    b = model_name.lower()
    if any(x in b for x in ("qwen", "chatml")):
        return "chatml"
    if any(x in b for x in ("llama", "hermes", "dolphin", "openhermes", "nous")):
        return "llama3"
    if any(x in b for x in ("mistral", "mixtral", "codestral")):
        return "mistral"
    if "gemma" in b:
        return "gemma"
    return "chatml"


CODEAGENT_TOOLS_SET = {"execute_code", "run_shell", "write_file", "read_file",
                       "list_files", "download_file", "download_project", "delete_file",
                       "search_files", "diff_files", "git_init", "git_diff", "git_commit",
                       "run_tests", "lint_code", "resume_project", "run_review",
                       "run_acceptance_review", "run_fixer", "run_aider_fix",
                       "start_coder_workflow", "get_coder_workflow",
                       "cancel_coder_workflow", "ask_project"}

# Tools safe to run in parallel (read-only or independent-target writes)
_PARALLEL_SAFE_TOOLS = {"read_file", "list_files", "search_files", "diff_files",
                        "git_diff", "research", "fetch_url"}

# Tool icons and human-readable labels for progress pills
_TOOL_ICONS = {
    "execute_code": ("code", "⚡ Executing code"),
    "run_shell": ("terminal", "🖥️ Running command"),
    "write_file": ("file-plus", "📝 Writing file"),
    "read_file": ("file-text", "📖 Reading file"),
    "list_files": ("folder", "📂 Listing files"),
    "download_file": ("download", "📦 Preparing download"),
    "download_project": ("package", "📦 Packaging project"),
    "delete_file": ("trash-2", "🗑️ Deleting file"),
    "generate_code": ("wand", "🤖 OpenHands building project"),
    "research": ("search", "🔍 Searching the web"),
    "fetch_url": ("globe", "🌐 Fetching URL"),
    "deep_research": ("microscope", "🔬 Agent research in progress"),
    "conspiracy_research": ("eye", "🕵️ Investigating"),
    "plan_project": ("compass", "📐 Architect designing plan"),
    "run_review": ("search-check", "🔍 Reviewing project"),
    "run_acceptance_review": ("clipboard-check", "✅ Acceptance reviewing project"),
    "run_fixer": ("wrench", "🛠 Fixer applying scoped edits"),
    "run_aider_fix": ("wrench", "🛠 Aider fixing uploaded project"),
    "start_coder_workflow": ("activity", "🧭 Starting Coder workflow"),
    "get_coder_workflow": ("activity", "📊 Checking Coder workflow"),
    "cancel_coder_workflow": ("x-circle", "🛑 Cancelling Coder workflow"),
    "ask_project": ("help-circle", "❓ Investigating project"),
    "project_indexer": ("database", "📚 Indexing project"),
    "run_tests": ("code", "🧪 Running tests"),
    "lint_code": ("code", "🧹 Linting code"),
    "git_init": ("terminal", "📁 Initializing git"),
    "git_diff": ("terminal", "📊 Checking changes"),
    "git_commit": ("terminal", "💾 Committing"),
    "resume_project": ("activity", "📂 Resuming project"),
    "search_files": ("search", "🔍 Searching files"),
    "diff_files": ("terminal", "📊 Diffing files"),
}


_BLOCKED_RUN_RE = re.compile(r"\b(run-[0-9a-fA-F]{8,16})\b")
_UPLOAD_MANUAL_TOOLS = {
    "read_file", "write_file", "run_shell", "execute_code", "list_files",
    "search_files", "diff_files", "download_project", "download_file",
}
_TERMINAL_WORKFLOW_STATES = {"accepted", "completed", "cancelled", "blocked"}


def _blocked_tool_signature(tool_name: str, tool_result: str) -> str:
    # Keyed on the blocking trigger only (run-id or first line), NOT the tool
    # name — a model alternating read_file → list_files → run_shell against
    # the same blocking run must still trip the duplicate-BLOCKED stop.
    if not (tool_result or "").lstrip().startswith("BLOCKED"):
        return ""
    first_line = next((line.strip() for line in tool_result.splitlines() if line.strip()), "")
    trigger = _BLOCKED_RUN_RE.search(tool_result or "")
    return trigger.group(1) if trigger else first_line[:140]


def _record_blocked_tool_result(state: dict, tool_name: str, tool_result: str) -> bool:
    sig = _blocked_tool_signature(tool_name, tool_result)
    if not sig:
        # A non-blocked sibling result (e.g. one OK tool in the same batch)
        # must not reset the counter mid-detection.
        return False
    if state.get("key") == sig:
        state["count"] = int(state.get("count") or 0) + 1
    else:
        state["key"] = sig
        state["count"] = 1
    return state["count"] >= 2


def _qa_run_qualifies(run: dict, stream_started_at: str) -> bool:
    """True when a qa run can be streamed verbatim as this turn's answer:
    succeeded, not a change request, has an answer, and was created by THIS
    stream — a stale qa run from a previous turn must never be replayed."""
    if (run.get("started_at") or "") < (stream_started_at or ""):
        return False
    env = run.get("result_envelope") or {}
    return (run.get("status") == "succeeded"
            and not env.get("looks_like_change_request")
            and bool((env.get("answer") or "").strip()))


def _next_action_from_blocked_result(tool_result: str) -> str:
    text = tool_result or ""
    m = re.search(r"(?:VERY NEXT tool call MUST be|Your VERY NEXT tool call MUST be):\s*\n\s*([^\n]+)", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"REQUIRED NEXT TOOL CALL:\s*([^\n]+)", text, re.I)
    if m:
        return m.group(1).strip()
    if "run_aider_fix" in text:
        return "run_aider_fix(...)"
    if "run_review" in text:
        return "run_review(...)"
    if "run_fixer" in text:
        return "run_fixer(...)"
    return "respond to the user with the blocked state and ask for guidance"


async def _mark_latest_uploaded_workflow_blocked(conv_id: str) -> None:
    if not conv_id:
        return
    try:
        wf = await db.get_latest_coder_workflow(conv_id)
        if wf and wf.get("mode") == "fix_uploaded_project" and wf.get("state") != "blocked":
            await db.update_coder_workflow(
                wf.get("id", ""),
                state="blocked",
                active_run_id=wf.get("active_run_id") or "",
                artifact_status="not_ready",
            )
    except Exception as e:
        print(f"[CHAT] blocked-loop workflow update failed (non-fatal): {e}")


async def _is_active_uploaded_manual_loop(conv_id: str, tool_names: list[str]) -> bool:
    if not conv_id or not tool_names:
        return False
    if not all((name or "") in _UPLOAD_MANUAL_TOOLS for name in tool_names):
        return False
    try:
        wf = await db.get_latest_coder_workflow(conv_id)
    except Exception as e:
        print(f"[CHAT] duplicate-loop workflow lookup failed (non-fatal): {e}")
        return False
    if not wf or wf.get("mode") != "fix_uploaded_project":
        return False
    return (wf.get("state") or "").lower() not in _TERMINAL_WORKFLOW_STATES


async def _next_uploaded_workflow_action(conv_id: str) -> str:
    project_id = ""
    try:
        wf = await db.get_latest_coder_workflow(conv_id) if conv_id else None
        project_id = (wf or {}).get("project_id") or ""
        runs = await db.get_runs_by_conversation(conv_id, limit=10) if conv_id else []
    except Exception as e:
        print(f"[CHAT] duplicate-loop next-action lookup failed (non-fatal): {e}")
        runs = []
    project_arg = f'project_id="{project_id}"' if project_id else "project_id=<active_project_id>"
    latest = runs[0] if runs else {}
    role = latest.get("role") or ""
    run_id = latest.get("id") or ""
    env = latest.get("result_envelope") or {}
    env_status = (env.get("status") or "").lower()

    if role == "aider.fix":
        return f"run_review({project_arg})"
    if role == "reviewer" and env_status == "clean":
        return f"run_acceptance_review({project_arg})"
    if role in {"reviewer", "acceptance"} and run_id:
        return f'run_aider_fix(project_id="{project_id}", issue_run_id="{run_id}")' if project_id else f'run_aider_fix(issue_run_id="{run_id}")'
    return f"run_review({project_arg})"


async def _duplicate_manual_tool_summary(conv_id: str, tool_names: list[str]) -> str:
    tool_label = ", ".join(n for n in tool_names if n) or "manual tool"
    next_action = await _next_uploaded_workflow_action(conv_id)
    synthetic = f"BLOCKED — duplicate manual tool loop.\nREQUIRED NEXT TOOL CALL: {next_action}"
    summary = await _blocked_tool_summary(conv_id, tool_label, synthetic)
    return summary.replace(
        "after the workflow gate rejected that same action",
        "after the duplicate detector rejected that same manual action",
    )


async def _blocked_tool_summary(conv_id: str, tool_name: str, tool_result: str) -> str:
    project_dir = ""
    issue_lines: list[str] = []
    reviewer_summary = ""
    try:
        wf = await db.get_latest_coder_workflow(conv_id) if conv_id else None
        if wf and wf.get("project_id"):
            project_dir = f"/root/projects/{wf.get('project_id')}"
        runs = await db.get_runs_by_conversation(conv_id, limit=20) if conv_id else []
        latest_issue = next(
            (r for r in runs
             if r.get("role") in {"reviewer", "acceptance"}
             and ((r.get("result_envelope") or {}).get("status") or "").lower() in {"issues", "error"}),
            None,
        )
        if latest_issue:
            env = latest_issue.get("result_envelope") or {}
            project_dir = project_dir or (env.get("project_dir") or "")
            reviewer_summary = env.get("summary") or ""
            for i, iss in enumerate((env.get("issues") or [])[:3], 1):
                issue_lines.append(
                    f"{i}. [{iss.get('severity', '?')}] {iss.get('file', '?')}"
                    + (f":{','.join(str(x) for x in iss.get('lines') or [])}" if iss.get("lines") else "")
                    + f" - {(iss.get('summary') or '')[:220]}"
                )
    except Exception as e:
        print(f"[CHAT] blocked-loop summary lookup failed (non-fatal): {e}")

    next_action = _next_action_from_blocked_result(tool_result)
    lines = [
        f"**Blocked**: I stopped because the agent repeatedly tried `{tool_name}` after the workflow gate rejected that same action.",
    ]
    if project_dir:
        lines.append(f"\nActive project path: `{project_dir}`")
    if reviewer_summary:
        lines.append(f"\nLatest reviewer summary: {reviewer_summary}")
    if issue_lines:
        lines.append("\nLatest reviewer issue(s):")
        lines.extend(issue_lines)
    lines.append(f"\nNext valid action: `{next_action}`")
    lines.append("\nI marked the uploaded-project workflow blocked so it does not keep burning rounds on the same rejected action.")
    return "\n".join(lines)


async def chat_stream_generate(req, http, events, custom_tool_map, custom_tool_id_map, connector_tool_id_map=None, connector_tool_name_map=None):
    """Async generator that yields SSE events for a streaming chat with tool-calling.

    Args:
        req: ChatRequest pydantic model
        http: httpx.AsyncClient
        events: EventBus instance
        custom_tool_map: dict of custom tools keyed by name
        custom_tool_id_map: dict of custom tools keyed by id
    """
    conv_id = req.conversation_id
    ephemeral = bool(getattr(req, "ephemeral", False))

    last_user_msg = ""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    # ── Defensive persist of the incoming user message ──
    # The frontend POSTs the user message fire-and-forget; if that fails
    # (flaky network, etc.) the message would be lost while the assistant
    # reply still saves below. Upsert if the most recent user message in
    # the DB doesn't match the incoming one.
    if conv_id and last_user_msg and not ephemeral:
        # Prefer the explicit display_content sent by the frontend (user's typed text,
        # without the `[Attached N image: ...]` hint that goes to the model). Fall back
        # to last_user_msg for older clients.
        _display = getattr(req, "display_content", None)
        _user_meta = getattr(req, "user_metadata", None)
        _content_to_save = _display if _display is not None else last_user_msg
        try:
            existing = await db.get_conversation(conv_id)
            if existing:
                msgs = existing.get("messages") or []
                recent_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
                # Match by either the clean display content or the raw model-facing content
                # so we don't double-save when the previous turn already persisted one.
                _recent_content = (recent_user.get("content") or "") if recent_user else ""
                if not recent_user or (_recent_content != _content_to_save and _recent_content != last_user_msg):
                    await db.add_message(conv_id, "user", _content_to_save, metadata=_user_meta)
        except Exception:
            pass

    await events.emit(conv_id, "tool_start", {"tool": "processing", "status": "🔮 Connecting to neural oracle...", "icon": "activity"})

    print(f"[CHAT] conv={conv_id} model={req.model} tool_ids={req.tool_ids} msgs={len(req.messages)} persona={req.persona_id} ephemeral={ephemeral}")
    _model_provider, _provider_model_name = model_providers.split_model_id(req.model)
    _is_cloud_provider = _model_provider in model_providers.CLOUD_PROVIDERS

    # ── Validate model exists in Ollama before streaming ──
    if not _is_cloud_provider:
        try:
            _tags_r = await http.get(f"{config.OLLAMA_URL}/api/tags", timeout=10)
            if _tags_r.status_code == 200:
                _available = [m["name"] for m in _tags_r.json().get("models", [])]
                _ollama_model_name = _provider_model_name if _model_provider == "ollama" else req.model
                if _ollama_model_name and _ollama_model_name not in _available:
                    _err_msg = f"Model '{req.model}' is not available. It may have been deleted. Available models: {', '.join(_available[:5])}"
                    print(f"[CHAT] Model not found: {req.model}")
                    await events.emit(conv_id, "error", {"status": f"Model not found: {req.model}"})
                    yield f"data: {json.dumps({'type': 'error', 'error': _err_msg})}\n\n"
                    return
        except Exception as _e:
            print(f"[CHAT] Could not validate model (continuing anyway): {_e}")

    # Resolve persona (model config) if provided — apply parameters and KB
    model_options = {}
    kb_context = ""
    persona_system_prompt = None
    persona_kb_ids = []
    persona_think_budget = None
    persona_placeholder_ctx = None
    _is_v2_persona = False
    if req.persona_id:
        all_configs = await db.get_model_configs()
        mc = next((c for c in all_configs if c["id"] == req.persona_id), None)
        if mc:
            persona_system_prompt = mc.get("system_prompt") or None
            # Same matching rules as the tools.py gate (_is_v2_persona); the
            # two still differ on source — req.persona_id here vs the
            # conversation's model_config_id there.
            if _v2_name_match(mc.get("name") or ""):
                _is_v2_persona = True
            params = mc.get("parameters", {})
            profile_type = _model_config_profile_type(mc, params)
            if profile_type == "persona":
                try:
                    current_user = await db.get_user()
                    user_name = (current_user or {}).get("name") or "User"
                except Exception:
                    user_name = "User"
                persona_placeholder_ctx = {
                    "user_name": user_name,
                    "char_name": mc.get("name") or "the character",
                }
                persona_fields = params.get("persona") if isinstance(params.get("persona"), dict) else {}
                thinking_mode = _normalize_persona_thinking_mode(persona_fields.get("thinking_mode", params.get("thinking_mode")))
                if thinking_mode != "auto":
                    persona_think_budget = 0 if thinking_mode == "off" else 1
                rating_guidance = _persona_rating_guidance(params)
                rating_block = f"\n\n=== PERSONA CONTENT RATING ===\n{rating_guidance}"
                if persona_system_prompt:
                    if "=== PERSONA CONTENT RATING ===" not in persona_system_prompt:
                        persona_system_prompt += rating_block
                else:
                    persona_system_prompt = rating_block.strip()
                persona_system_prompt = _replace_persona_placeholders(
                    persona_system_prompt or "",
                    **persona_placeholder_ctx,
                )
            for key in ("temperature", "num_ctx", "top_p", "top_k"):
                if params.get(key) is not None:
                    if key == "num_ctx":
                        _ctx = config.coerce_num_ctx(params[key], fallback=None)
                        if _ctx:
                            model_options[key] = _ctx
                    else:
                        model_options[key] = params[key]

            # Extract latest user message for RAG queries
            user_query = ""
            for m in reversed(req.messages):
                if m.get("role") == "user" and m.get("content"):
                    user_query = m["content"]
                    break

            kb_ids = mc.get("kb_ids", [])
            persona_kb_ids = [] if ephemeral else kb_ids

            # ── RAG config defaults (used by both KB and research memory queries) ──
            _rag_research_top_k = 4
            _rag_research_max_chars = 3000

            # ── RAG: Query attached knowledge bases ──
            if kb_ids and user_query and not ephemeral:
                await events.emit(conv_id, "tool_start", {
                    "tool": "kb", "icon": "database",
                    "status": f"Searching {len(kb_ids)} knowledge base(s)...",
                })
                try:
                    _rag_cfg = config.DEFAULT_SETTINGS.get("rag", {})
                    try:
                        import json as _json
                        with open(config.SETTINGS_PATH, "r") as _sf:
                            _rag_cfg = {**_rag_cfg, **_json.load(_sf).get("rag", {})}
                    except Exception:
                        pass
                    _rag_top_k = int(_rag_cfg.get("top_k", 6))
                    _rag_max_chars = int(_rag_cfg.get("max_context_chars", 6000))
                    _rag_research_top_k = int(_rag_cfg.get("research_top_k", 4))
                    _rag_research_max_chars = int(_rag_cfg.get("research_max_chars", 3000))

                    chunks = await rag.query(kb_ids, user_query, top_k=_rag_top_k)
                    if chunks:
                        kb_context = rag.format_context(chunks, max_chars=_rag_max_chars)
                        filenames = list(set(c["filename"] for c in chunks))
                        avg_score = sum(c["score"] for c in chunks) / len(chunks)
                        await events.emit(conv_id, "tool_done", {
                            "tool": "kb", "icon": "database",
                            "status": f"Found {len(chunks)} relevant chunks from {', '.join(filenames[:3])} ({avg_score:.0%} avg relevance)",
                        })
                        print(f"[RAG] KB retrieved {len(chunks)} chunks (avg {avg_score:.2f}) for: {user_query[:80]!r}")
                    else:
                        print(f"[RAG] No KB chunks found for: {user_query[:80]!r}")
                except Exception as e:
                    print(f"[RAG] KB query failed, falling back to raw injection: {e}")
                    kb_files = await db.get_kb_files_for_kbs(kb_ids)
                    parts = []
                    total_kb_chars = 0
                    for kf in kb_files:
                        if total_kb_chars >= 8000:
                            break
                        fp = kf.get("filepath", "")
                        if os.path.exists(fp):
                            try:
                                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                                    txt = fh.read(4000)
                                parts.append(f"--- {kf.get('filename', '')} ---\n{txt}")
                                total_kb_chars += len(txt)
                            except Exception:
                                pass
                    if parts:
                        kb_context = "\n\n".join(parts)

            # ── RAG: Query persona's past research memory ──
            if user_query and not ephemeral:
                try:
                    research_chunks = await rag.query_research(req.persona_id, user_query, top_k=_rag_research_top_k)
                    if research_chunks:
                        research_context = rag.format_context(research_chunks, max_chars=_rag_research_max_chars)
                        if kb_context:
                            kb_context += "\n\n=== PAST RESEARCH FINDINGS ===\n" + research_context
                        else:
                            kb_context = research_context
                        avg_rs = sum(c["score"] for c in research_chunks) / len(research_chunks)
                        await events.emit(conv_id, "tool_done", {
                            "tool": "memory", "icon": "brain",
                            "status": f"Recalled {len(research_chunks)} past research findings ({avg_rs:.0%} relevance)",
                        })
                        print(f"[RAG] Research memory: {len(research_chunks)} chunks (avg {avg_rs:.2f}) for: {user_query[:80]!r}")
                except Exception as e:
                    print(f"[RAG] Research memory query error: {e}")

    # Apply global overrides from request (when no persona overrides them)
    if req.num_ctx is not None and "num_ctx" not in model_options:
        _ctx = config.coerce_num_ctx(req.num_ctx, fallback=None)
        if _ctx:
            model_options["num_ctx"] = _ctx
    if req.temperature is not None and "temperature" not in model_options:
        model_options["temperature"] = req.temperature
    if req.top_p is not None and "top_p" not in model_options:
        model_options["top_p"] = req.top_p
    if req.top_k is not None and "top_k" not in model_options:
        model_options["top_k"] = req.top_k
    if req.repeat_penalty is not None and "repeat_penalty" not in model_options:
        model_options["repeat_penalty"] = req.repeat_penalty

    # Ensure num_ctx always has a sane positive default. Passing 0 through to
    # Ollama collapses some models to their tiny native default context.
    if "num_ctx" in model_options:
        _ctx = config.coerce_num_ctx(model_options.get("num_ctx"), fallback=None)
        if _ctx:
            model_options["num_ctx"] = _ctx
        else:
            model_options.pop("num_ctx", None)
    if "num_ctx" not in model_options:
        model_options["num_ctx"] = config.coerce_num_ctx(config.DEFAULT_NUM_CTX)

    global_memory_enabled = False
    if not ephemeral:
        raw_use_memories = getattr(req, "use_memories", None)
        if raw_use_memories is None:
            try:
                global_memory_enabled = await db.get_conversation_use_memories(conv_id)
            except Exception as _gme:
                print(f"[CHAT] Conversation memory flag lookup failed (non-fatal): {_gme}")
                global_memory_enabled = False
        else:
            global_memory_enabled = str(raw_use_memories).lower() in {"1", "true", "yes", "on"}

    global_memory_info = {"profile": None, "context": "", "memory_ids": []}
    if global_memory_enabled and not ephemeral:
        try:
            global_memory_info = await db.build_global_memory_context(
                query=last_user_msg,
                max_chars=4200,
            )
            if global_memory_info.get("context"):
                mem_count = len(global_memory_info.get("memory_ids") or [])
                await events.emit(conv_id, "tool_done", {
                    "tool": "memory", "icon": "brain",
                    "status": (
                        f"Recalled {mem_count} HyprChat memor{'y' if mem_count == 1 else 'ies'}"
                        if mem_count else "Recalled HyprChat profile"
                    ),
                })
        except Exception as _gme:
            print(f"[CHAT] HyprChat memory context failed (non-fatal): {_gme}")
            global_memory_info = {"profile": None, "context": "", "memory_ids": []}

    workspace_memory_info = {"workspace": None, "context": "", "memory_ids": [], "block_ids": []}
    if not ephemeral:
        try:
            workspace_memory_info = await db.build_workspace_memory_context(
                workspace_id=getattr(req, "workspace_id", None),
                conversation_id=conv_id,
                query=last_user_msg,
                max_chars=5200,
            )
            if workspace_memory_info.get("context"):
                await events.emit(conv_id, "tool_done", {
                    "tool": "memory", "icon": "brain",
                    "status": (
                        f"Recalled {len(workspace_memory_info.get('memory_ids') or [])} workspace "
                        f"memories from {workspace_memory_info.get('workspace', {}).get('name', 'workspace')}"
                    ),
                })
        except Exception as _wme:
            print(f"[CHAT] Workspace memory context failed (non-fatal): {_wme}")
            workspace_memory_info = {"workspace": None, "context": "", "memory_ids": [], "block_ids": []}

    messages = []
    effective_system = persona_system_prompt if persona_system_prompt is not None else req.system_prompt
    if kb_context:
        effective_system += (
            "\n\n=== RELEVANT KNOWLEDGE BASE CONTEXT ===\n"
            "The following excerpts were retrieved from your knowledge base based on "
            "the user's query. Use them to accurately answer questions. "
            "Each excerpt shows its source file and relevance score.\n\n"
            + kb_context
        )
    if global_memory_info.get("context"):
        effective_system += (
            "\n\n=== HYPRCHAT MEMORY ===\n"
            "The following user profile and cross-chat memories were explicitly entered "
            "or accepted by the user. Use them as persistent context when relevant. "
            "If this context conflicts with the latest user message, ask or follow the "
            "latest explicit user instruction.\n\n"
            + global_memory_info["context"]
        )
    if workspace_memory_info.get("context"):
        effective_system += (
            "\n\n=== WORKSPACE MEMORY ===\n"
            "The following workspace memory was explicitly accepted or configured by the user. "
            "Use it as persistent context for this workspace. If it conflicts with the latest "
            "user message, ask or follow the latest explicit user instruction.\n\n"
            + workspace_memory_info["context"]
        )
    if effective_system:
        messages.append({"role": "system", "content": effective_system})

    # ── Rendering hint — tell the model diagrams/math render INLINE, not as files ──
    # Without this, models with write_file / generate_code available will often save
    # flowcharts to a file and return a download link instead of emitting inline.
    messages.append({
        "role": "system",
        "content": (
            "## RENDERING\n"
            "\n"
            "### Visualisation — emit inline fences, never save image files\n"
            "The chat UI renders these formats directly in your response text:\n"
            "- **Diagrams**: wrap Mermaid source in a ```mermaid code fence. Flowcharts, sequence diagrams, "
            "class/state/ER diagrams, gantt, mindmap, and pie all render as live SVG. Example: "
            "```mermaid\\nflowchart LR\\nA --> B\\n```\n"
            "- **Math**: use `$...$` for inline math and `$$...$$` for display equations. KaTeX renders both.\n"
            "- **Charts**: wrap Chart.js JSON in a ```chart code fence for inline data viz. "
            "You may also use ```pygraph as an alias for the same safe chart schema. "
            "Supported types: `bar`, `line`, `pie`, `doughnut`, `scatter`, `radar`, `polarArea`. "
            "Simple form: `{\"type\":\"bar\",\"labels\":[\"A\",\"B\",\"C\"],\"data\":[4,7,2],\"title\":\"optional\"}`. "
            "Multi-series: use `\"datasets\":[{\"label\":\"X\",\"data\":[...]},{\"label\":\"Y\",\"data\":[...]}]` instead of `\"data\"`. "
            "Colors auto-pick from the active theme palette. Use charts for quantitative comparisons or trends "
            "instead of dumping raw numbers or ASCII art.\n"
            "- **Callouts**: start a blockquote with `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, "
            "`> [!WARNING]`, or `> [!CAUTION]` to render a coloured admonition box. Subsequent `> ` lines are body. "
            "Use these for important caveats, tips, or warnings — don't over-use.\n"
            "- **Keyboard keys**: wrap key names in `<kbd>` tags (e.g. `<kbd>Ctrl</kbd>+<kbd>K</kbd>`) for "
            "styled key caps when describing shortcuts.\n"
            "- **Colors**: hex codes (`#ff0088`), `rgb(...)`, and `hsl(...)` values are auto-detected and "
            "render with a small color-swatch chip inline. No special syntax required.\n"
            "\n"
            "**For visualisation, never save image files.** Do NOT call `write_file` to produce a PNG/SVG. "
            "Do NOT write Python/R/JS with matplotlib, seaborn, plotly, pandas.plot, ggplot, or any charting "
            "library that calls `savefig` / `write_image` / similar. The ```chart fence renders interactively, "
            "theme-matched, with zero latency — a saved image cannot match that. If your first instinct is "
            "`import matplotlib`, stop and emit a ```chart fence instead.\n"
            "\n"
            "### Computation — `execute_code` IS the right tool\n"
            "Use `execute_code` freely for actual arithmetic, aggregation, statistics, parsing, scraping, "
            "growth-rate/CAGR/variance/weighted-average calculations — anything you'd get wrong by doing it "
            "in your head. LLM mental math is unreliable past trivial cases; trust the sandbox. Code tools "
            "are explicitly encouraged for computation, transformation, and fetching data you don't have. "
            "The prohibition above is purely about saving image files, not about running code.\n"
            "\n"
            "### The compute-then-chart pattern\n"
            "When a task needs both math AND a visual:\n"
            "  1. Call `execute_code` to COMPUTE the numbers. Print the final values to stdout so you can "
            "read them back (e.g. `print(json.dumps({\"labels\": [...], \"data\": [...]}))`).\n"
            "  2. Emit a ```chart fence using those computed values.\n"
            "Never present a chart of numbers you estimated when `execute_code` could have computed them "
            "exactly.\n"
            "\n"
            "### Combining diagrams and math\n"
            "Mermaid does NOT render LaTeX inside node labels. Keep node labels as plain text "
            "(short descriptions, variable names like `E_n` or `psi`, plain ASCII). If a node "
            "needs an equation, reference it by name in the node and write the actual equation "
            "as a separate `$$...$$` block ABOVE or BELOW the diagram. Never put `$...$` or "
            "`\\frac{}{}` or other LaTeX syntax inside a mermaid node label — it will show as "
            "raw dollar-sign text instead of rendered math."
        )
    })

    # ── Active project context: if this conversation has a coding project, inject it ──
    # Lets the bot answer "fix this bug in the project you built me" without the LLM
    # having to remember to call resume_project on its own. One DB query, fails closed.
    try:
        _active_project = await db.get_coding_project_by_conv(conv_id)
    except Exception as _ape:
        _active_project = None
        print(f"[CHAT] Active project lookup failed (non-fatal): {_ape}")

    if _active_project:
        _ap_files = _active_project.get("file_manifest") or []
        # On-disk dir is openhands_project_id when present (worker may have
        # dedupe-renamed); every tools.py consumer resolves the same way.
        _ap_id = (_active_project.get("openhands_project_id")
                  or _active_project.get("id", "") or "")
        _ap_path = f"/root/projects/{_ap_id}" if _ap_id else ""
        _ap_lines = [
            "## ACTIVE PROJECT (this conversation already has a built project)",
            f"- display name: {_active_project.get('name', '?')}  (NOT a directory — do NOT use this as a path)",
            f"- project_id: {_ap_id}",
            f"- **ON-DISK PATH (use this for ALL tool calls): `{_ap_path}`**",
            f"- language: {_active_project.get('language', '?')}",
            f"- description: {(_active_project.get('description') or '')[:200]}",
        ]
        if _ap_files:
            _ap_lines.append(f"- files ({len(_ap_files)}):")
            for _f in _ap_files[:25]:
                _ap_lines.append(f"  - {_f}")
            if len(_ap_files) > 25:
                _ap_lines.append(f"  - ... and {len(_ap_files) - 25} more")
        if _is_v2_persona:
            _ap_lines.append(
                "\n## How to handle this project\n"
                f"**The project lives at `{_ap_path}` — already extracted on disk. "
                f"NEVER mkdir a new directory. NEVER use the display name "
                f"('{_active_project.get('name', '?')}') as a path. ALWAYS pass "
                f"`{_ap_path}` to any tool that takes a project_dir or path.**\n\n"
                "1. **For QUESTIONS** about the code (\"how does X work?\", \"walk me "
                "through Y\", \"show me Z\", \"break down W line by line\", \"what does "
                "this do?\"): your VERY FIRST tool call MUST be `ask_project(question='...')`. "
                "Do NOT call list_files, read_file, search_files, run_review, run_shell, "
                "or download_project. The ProjectQA agent reads the code, finds what's "
                "relevant, and produces a grounded answer with file:line citations in "
                "ONE round. Hand-rolling read_file across many files is the v1 "
                "antipattern that wastes rounds.\n"
                "2. **For CHANGE REQUESTS — DEFAULT TO write_file, NOT generate_code.** "
                "Only call generate_code when the user is genuinely asking you to add "
                "3+ NEW files or do a major refactor. For ALL of these, use read_file + "
                "write_file:\n"
                "   - User pastes 1–5 lines of code (\"the fix is `x.foo()`\", \"replace "
                "Y with Z\", \"use this API call\") → read_file the relevant file(s) → "
                "write_file with the change.\n"
                "   - 1–3 file tweaks, bug fixes, refactors of an existing function, "
                "additions of small features.\n"
                "   - Anything where you'd touch fewer than ~3 files.\n"
                f"   When you DO need generate_code, pass `project_id='{_ap_id}'` so the "
                f"Builder amends in place instead of scaffolding fresh. NEVER pass a "
                f"task description that says \"create a complete X\" or \"build a Y\" "
                f"for a project that already exists — that triggers a scaffold rebuild "
                f"and clobbers the existing code.\n"
                "3. **For BUG REPORTS** (\"the tests are failing\", \"it crashes when "
                f"...\"): call `run_review(project_dir='{_ap_path}')` first — the "
                f"Reviewer runs the build/tests and tells you what's broken. If the "
                f"reviewer comes back clean but the user still reports a runtime bug "
                f"(crashes, UI not updating, error that compiles fine): read the "
                f"relevant 1–3 files and write_file the targeted fix. Do NOT call "
                f"generate_code for runtime bugs.\n"
                "4. **Do NOT auto-package or download** the project unless the user "
                "explicitly asks for the tarball/zip. They already uploaded it."
            )
        else:
            # v1 path — keep the existing read_file → write_file/generate_code flow.
            _ap_lines.append(
                "\nIf the user reports a bug, error, or asks for changes to this project: "
                "use read_file on the relevant files first to see the current code, then fix "
                "with write_file (small changes) or generate_code with project_id="
                f"'{_ap_id}' (large changes). Do NOT start a new project."
            )
        messages.append({"role": "system", "content": "\n".join(_ap_lines)})
        print(f"[CHAT] Injected active project context: {_active_project.get('name', '?')} ({len(_ap_files)} files)")

    for m in req.messages:
        _content = m["content"]
        if persona_placeholder_ctx and m.get("role") in {"assistant", "system"}:
            _content = _replace_persona_placeholders(_content, **persona_placeholder_ctx)
        _msg = {"role": m["role"], "content": _content}
        # Pass image attachments through to vision-capable Ollama models.
        # Non-vision models silently ignore the field.
        if m.get("images"):
            _msg["images"] = m["images"]
        messages.append(_msg)

    # ── Build Ollama-native tool definitions ──
    available_tool_names = set()
    ollama_tools = []
    requested_tool_ids = list(req.tool_ids or [])
    connector_tool_id_map = connector_tool_id_map or {}
    connector_tool_name_map = connector_tool_name_map or {}

    for tid in requested_tool_ids:
        if tid == "codeagent":
            for tname, tdef in CODEAGENT_TOOLS.items():
                if tname not in ("deep_research", "conspiracy_research"):
                    ollama_tools.append(tdef)
                    available_tool_names.add(tname)
        elif tid in CODEAGENT_TOOLS:
            ollama_tools.append(CODEAGENT_TOOLS[tid])
            available_tool_names.add(tid)
        elif tid in custom_tool_id_map:
            ct = custom_tool_id_map[tid]
            tool_params = parse_tool_params(ct.get("code", ""), ct["name"])
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": ct["name"],
                    "description": ct.get("description", f"Custom tool: {ct['name']}"),
                    "parameters": tool_params,
                }
            })
            available_tool_names.add(ct["name"])
        elif tid in connector_tool_id_map:
            ct = connector_tool_id_map[tid]
            ollama_tools.append(tool_def_from_connector_tool(ct))
            available_tool_names.add(ct["tool_name"])
        else:
            for tname, tdef in CODEAGENT_TOOLS.items():
                if tname == tid:
                    ollama_tools.append(tdef)
                    available_tool_names.add(tname)

    # Include deep_research for all codeagent sessions; conspiracy_research only when explicitly listed
    if "codeagent" in requested_tool_ids:
        if "deep_research" in CODEAGENT_TOOLS and "deep_research" not in available_tool_names:
            ollama_tools.append(CODEAGENT_TOOLS["deep_research"])
            available_tool_names.add("deep_research")
    # Also include special tools if explicitly listed by name
    for tname in ("deep_research", "conspiracy_research"):
        if tname in requested_tool_ids:
            if tname in CODEAGENT_TOOLS and tname not in available_tool_names:
                ollama_tools.append(CODEAGENT_TOOLS[tname])
                available_tool_names.add(tname)

    # ── Always include execute_code + download_file so any chat can generate visuals ──
    _visual_tools = ("execute_code", "download_file")
    for _vt in _visual_tools:
        if _vt not in available_tool_names and _vt in CODEAGENT_TOOLS:
            ollama_tools.append(CODEAGENT_TOOLS[_vt])
            available_tool_names.add(_vt)

    print(f"[CHAT]   Tools: {sorted(available_tool_names)}")

    # ── Quick Search: gate → rewrite → search → rank → fetch → inject ──
    if "quick_search" in requested_tool_ids:
        try:
            qs = await run_quick_search_for_chat(
                http,
                config.OLLAMA_URL,
                config.WORKSPACE_MODEL or config.DEFAULT_MODEL,
                events, conv_id, messages,
                default_model=config.DEFAULT_MODEL,
                chat_model=req.model or "",
            )
            if qs.get("context"):
                for m in reversed(messages):
                    if m["role"] == "user":
                        m["content"] = (m.get("content") or "") + "\n\n" + qs["context"]
                        break
        except Exception as e:
            print(f"[CHAT]   Quick search error: {e}")
            try:
                await events.emit(conv_id, "tool_done", {
                    "tool": "quick_search", "icon": "alert-circle",
                    "status": f"Search failed: {e}",
                })
            except Exception:
                pass
            # Tell the model search is unavailable so it doesn't confidently
            # hallucinate current-event facts. Prepended to the latest user
            # message so it stays scoped to this turn.
            _fail_note = (
                "\n\n[SYSTEM: Web search was unavailable for this turn. "
                "If asked about current events, recent news, or specific "
                "factual claims, say you can't verify them rather than guessing.]"
            )
            for _m in reversed(messages):
                if _m["role"] == "user":
                    _m["content"] = (_m.get("content") or "") + _fail_note
                    break

    # Inject visualization hint for non-coder chats that have execute_code.
    _has_full_codeagent = bool(available_tool_names & (CODEAGENT_TOOLS_SET - {"execute_code", "download_file"}))
    if _is_v2_persona and _has_full_codeagent:
        _current_ctx = config.coerce_num_ctx(model_options.get("num_ctx"), fallback=config.DEFAULT_NUM_CTX)
        _v2_ctx = max(_current_ctx, config.CODER_V2_MIN_NUM_CTX)
        if _v2_ctx != model_options.get("num_ctx"):
            print(f"[CHAT] Coder Bot v2 num_ctx raised to {_v2_ctx} (was {model_options.get('num_ctx')})")
        model_options["num_ctx"] = _v2_ctx
    if not _has_full_codeagent and "execute_code" in available_tool_names:
        _viz_hint = (
            "\n\n## Visualization Capability\n"
            "You have access to execute_code for arithmetic, aggregation, statistics, parsing, "
            "and data transformation. When a visual aid would genuinely help, compute the "
            "numbers with execute_code, then emit an inline ```chart or ```pygraph fence using "
            "the computed values. Use ```mermaid fences for diagrams and `$...$` / `$$...$$` "
            "for math. Do not save image files for charts or diagrams.\n"
        )
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += _viz_hint
        else:
            messages.insert(0, {"role": "system", "content": _viz_hint.strip()})

    # Research-only sessions (deep_research / conspiracy_research enabled without
    # codeagent) need an explicit nudge — without it the model sees the tool but
    # just answers from memory. Skipped when full codeagent is on; that prompt
    # already covers tool use.
    _research_tools_present = available_tool_names & {"deep_research", "conspiracy_research"}
    if _research_tools_present and not _has_full_codeagent:
        _names = " or ".join(sorted(_research_tools_present))
        _research_sys = (
            "\n\n## RESEARCH PROTOCOL (MANDATORY)\n"
            f"The user has explicitly enabled **{_names}** for this turn. That means "
            "they want a multi-source web-researched answer, not your own analysis. "
            f"Your FIRST response MUST be a call to {_names}.\n"
            "\n"
            "**This applies even when context is attached.** Attached PDFs, pasted "
            "text, or knowledge-base excerpts describe the USER (their data, their "
            "situation) — they are NOT a substitute for external research. The user "
            "wants you to compare, benchmark, or contextualize that attached data "
            "against external sources you fetch with the tool.\n"
            "\n"
            "- Call the tool with a clear `topic` derived from the user's question. "
            "Pull comparison terms, entity names, or benchmarks out of the attached "
            "context to make the topic specific.\n"
            "- Use `depth` to match the user's request (1=quick, 3=standard, 5=exhaustive).\n"
            "- Wait for the tool result, then synthesize a final report that "
            "cross-references the attached data against what the tool returned. "
            "Cite sources from the tool result.\n"
            "- Do NOT write a written answer before calling the tool. Do NOT say "
            "\"based on the attached document...\" as your first move.\n"
            "- Only skip the tool for greetings, clarification questions, or trivial "
            "definitional questions where external research adds nothing.\n"
        )
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += _research_sys
        else:
            messages.insert(0, {"role": "system", "content": _research_sys.strip()})

    # Inject tool-use system prompt when full codeagent tools are available
    if _has_full_codeagent:
        tool_sys = "\n\n## CODING AGENT PROTOCOL (MANDATORY)\n"

        if "generate_code" in available_tool_names:
            tool_sys += (
                "### PRIMARY WORKFLOW: generate_code\n"
                "For coding tasks, call generate_code FIRST with a COMPLETE task description.\n"
                "It builds entire projects autonomously. Call it ONCE. If it fails, use write_file + run_shell.\n\n"
            )

        tool_sys += (
            "### RULES\n"
            "1. FIRST response MUST be a tool call.\n"
            "2. NEVER write code in chat text — use execute_code, write_file, or generate_code.\n"
            "3. execute_code = run code directly (NO stdin, NO sys.argv). For scripts with args: write_file + run_shell.\n"
            "4. When code fails: read the error, fix the ROOT CAUSE, try DIFFERENTLY.\n"
            "5. After success: download_file/download_project, then summarize for user. STOP.\n\n"
            "### AVOID\n"
            "- Do NOT start dev servers (npm start, flask run) — they hang forever.\n"
            "- Do NOT use input() — no stdin available.\n"
            "- Do NOT repeat failed commands without changing something.\n"
        )

        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += tool_sys
        else:
            messages.insert(0, {"role": "system", "content": tool_sys.strip()})

    # If no tools, don't include tool_call instructions in fallback prompt
    if not ollama_tools:
        for m in messages:
            if m["role"] == "system" and "tool_call" in m.get("content", ""):
                m["content"] = m["content"].replace(
                    '<tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>', "")

    # Default repeat_penalty for tool-using agents to prevent degenerate loops
    if "repeat_penalty" not in model_options and available_tool_names:
        model_options["repeat_penalty"] = 1.1

    _is_coder = _has_full_codeagent
    MAX_ROUNDS = config.MAX_AGENT_ROUNDS_CODER if _is_coder else config.MAX_AGENT_ROUNDS
    MAX_CONTEXT_CHARS = 80000  # ~20k tokens — prune old tool results beyond this

    # Phase 0.6: create the assistant-message stub at stream start so disconnects
    # don't lose work. The agent updates this row at every round boundary; if the
    # client closes the tab mid-stream, the most recent saved content is what
    # the frontend sees on reload.
    _assistant_msg_id = None
    _stream_started_at = datetime.utcnow().isoformat()
    if not ephemeral:
        try:
            _assistant_msg_id = await db.add_message(conv_id, "assistant", "", metadata={
                "stream_started_at": _stream_started_at,
                "in_progress": True,
            })
            # Tell the frontend the message_id so it can PATCH the final state on
            # stream-complete instead of POSTing a duplicate.
            yield f"data: {json.dumps({'type': 'init', 'message_id': _assistant_msg_id})}\n\n"
        except Exception as _ase:
            print(f"[CHAT] Failed to create assistant message stub: {_ase}")
    _text_fallback_done = False
    if _is_cloud_provider and available_tool_names:
        print(f"[CHAT] Cloud model {req.model} using text tool-call fallback")
        ollama_tools = []
        inject_text_tool_prompt(messages, available_tool_names)
        _text_fallback_done = True
    _prev_tool_key = None  # Track previous tool call to detect loops
    _tool_history = []     # Last N tool keys for near-duplicate detection
    _dup_break_count = 0   # How many times we broke out of duplicate loops
    _blocked_tool_state = {"key": "", "count": 0}
    _last_error_sig = None  # Signature of last tool error for loop detection
    _error_repeat_count = 0  # Consecutive times we've seen the same error
    _generate_code_fail_rounds = 0  # Counter: successful rounds since generate_code failure (0 = no failure or just failed)
    _generate_code_done = False    # Guard: stop tool calls after successful generate_code
    _rescue_count = 0              # How many times we rescued code blocks
    _oom_retries = 0               # OOM context halving retries
    # Effort-level self-review rounds: 0=off, capped at 3
    _review_round = 0
    _review_budget = max(0, min(3, int(getattr(req, "effort_rounds", 0) or 0)))
    _best_review_content = ""  # Longest detailed answer seen so far — used for anti-regression

    for round_num in range(MAX_ROUNDS):
        content = ""
        thinking = ""
        tool_calls = []
        gen_tokens = 0
        prompt_tokens = 0
        _gen_pill_started = False  # First "generating" pill uses tool_start; subsequent ones use tool_progress so the frontend collapses them into one updating pill
        _streamed_content = False
        _has_tools = bool(available_tool_names)

        # ── Context window management: prune old tool results to stay under budget ──
        _ctx_size = sum(len(m.get("content", "")) for m in messages)
        if _ctx_size > MAX_CONTEXT_CHARS and len(messages) > 6:
            # Summarize old tool results (keep system prompt + last 6 messages intact)
            for mi in range(1, len(messages) - 6):
                m = messages[mi]
                if m["role"] == "tool" and len(m.get("content", "")) > 500:
                    # Truncate old tool results to first 200 chars + note
                    orig = m["content"]
                    m["content"] = orig[:200] + f"\n\n[... {len(orig)} chars truncated to save context ...]"
            _new_size = sum(len(m.get("content", "")) for m in messages)
            print(f"[CHAT]   Context pruned: {_ctx_size} -> {_new_size} chars")

        if round_num > 0:
            await events.emit(conv_id, "tool_start", {"tool": "processing", "status": "🔄 Processing tool results...", "icon": "activity"})

        payload = {
            "model": _provider_model_name if _model_provider == "ollama" else req.model,
            "messages": messages,
            "stream": True,
            "options": model_options,
            "keep_alive": "10m",
        }
        print(f"[CHAT] Sending to Ollama: model={req.model} options={model_options}")
        # Thinking control: Persona override wins, then request setting, then model default.
        effective_think_budget = persona_think_budget if persona_think_budget is not None else getattr(req, "think_budget", None)
        if effective_think_budget is not None:
            if effective_think_budget == 0:
                payload["think"] = False
            else:
                payload["think"] = True
        if ollama_tools:
            payload["tools"] = ollama_tools

        # Keepalive before Ollama call — prevents browser/proxy timeout during prompt eval
        yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

        # Estimate prompt tokens so the frontend counter reflects context-in-flight,
        # not just live generation. Real prompt_eval_count arrives only at done=True.
        _est_prompt_pre = sum(len(m.get("content", "")) for m in messages) // 4
        if _est_prompt_pre:
            yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': 0, 'prompt_tokens': _est_prompt_pre, 'live': True})}\n\n"

        try:
            if _is_cloud_provider:
                print(f"[CHAT] Sending to {model_providers.provider_label(_model_provider)}: model={req.model} options={model_options}")
                _in_thinking = False
                _thinking_buf = ""
                _chunk_buf = ""
                _repeat_window = ""
                _live_gen_tokens = 0
                async for event in model_providers.stream_provider_chat(
                    http,
                    req.model,
                    messages,
                    options=model_options,
                ):
                    etype = event.get("type")
                    if etype == "usage":
                        gen_tokens = int(event.get("gen_tokens") or 0)
                        prompt_tokens = int(event.get("prompt_tokens") or 0)
                        if gen_tokens or prompt_tokens:
                            yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': gen_tokens, 'prompt_tokens': prompt_tokens})}\n\n"
                        continue
                    if etype == "thinking":
                        _thinking_token = event.get("content", "")
                        if not _thinking_token:
                            continue
                        _thinking_buf += _thinking_token
                        _in_thinking = True
                        _live_gen_tokens += 1
                        if _live_gen_tokens % 10 == 0:
                            yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': _live_gen_tokens, 'prompt_tokens': prompt_tokens, 'live': True})}\n\n"
                        if len(_thinking_buf) % 100 < len(_thinking_token):
                            snip = _thinking_buf[-60:].replace("\n", " ")
                            await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": _thinking_buf[-3000:]})})
                        continue
                    if etype != "token":
                        continue
                    token = event.get("content", "")
                    if not token:
                        continue

                    if "<think>" in token:
                        _in_thinking = True
                        token = token.split("<think>", 1)[1]
                    if _in_thinking:
                        if "</think>" in token:
                            before_end = token.split("</think>", 1)[0]
                            after_end = token.split("</think>", 1)[1]
                            _thinking_buf += before_end
                            thinking = _thinking_buf
                            _in_thinking = False
                            token = after_end
                            if thinking:
                                snip = thinking[-60:].replace("\n", " ")
                                await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": thinking[-3000:]})})
                        else:
                            _thinking_buf += token
                            if len(_thinking_buf) % 100 < len(token):
                                snip = _thinking_buf[-60:].replace("\n", " ")
                                await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": _thinking_buf[-3000:]})})
                            continue

                    if not token:
                        continue
                    content += token
                    _live_gen_tokens += 1
                    if _live_gen_tokens % 10 == 0:
                        yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': _live_gen_tokens, 'prompt_tokens': prompt_tokens, 'live': True})}\n\n"

                    _repeat_window = (_repeat_window + token)[-200:]
                    _in_fence = (content.count("```") % 2) == 1
                    if not _in_fence and len(_repeat_window) >= 120:
                        for plen in range(2, 25):
                            pat = _repeat_window[-plen:]
                            if not pat.strip():
                                continue
                            count = _repeat_window.count(pat)
                            min_count = 20 if plen <= 4 else 8
                            if count >= min_count and count * plen > len(_repeat_window) * 0.85:
                                print(f"[CHAT]   Repetition detected: {pat!r} x{count} — stopping generation")
                                content = content[:content.rfind(pat)]
                                break
                        else:
                            pat = None
                        if pat:
                            break

                    if _has_tools and len(content) % 200 < len(token):
                        _evt_type = "tool_start" if not _gen_pill_started else "tool_progress"
                        _gen_pill_started = True
                        _shown_tokens = max(_live_gen_tokens, (len(content) + 3) // 4)
                        await events.emit(conv_id, _evt_type, {
                            "tool": "generating",
                            "status": f"✍️ Generating... (~{_shown_tokens} tokens)",
                            "icon": "edit",
                        })

                    _chunk_buf += token
                    if len(_chunk_buf) >= 8:
                        yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
                        _streamed_content = True
                        _chunk_buf = ""
                        await asyncio.sleep(0)

                if _chunk_buf:
                    yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
                    _streamed_content = True
                if _in_thinking and _thinking_buf:
                    thinking = _thinking_buf
                    _in_thinking = False
                raise _ProviderStreamComplete()

            async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat",
                                   json=payload, timeout=300) as resp:
                if resp.status_code != 200:
                    error_body = (await resp.aread()).decode()[:500]
                    if "not found" in error_body.lower():
                        _msg = f"Model '{req.model}' not found in Ollama. It may have been deleted. Please select a different model."
                        print(f"[CHAT] Model not found: {req.model}")
                        await events.emit(conv_id, "tool_end", {"tool": "processing", "status": f"Model not found: {req.model}", "icon": "alert"})
                        yield f"data: {json.dumps({'type': 'error', 'error': _msg})}\n\n"
                        return
                    if "does not support tools" in error_body.lower() and not _text_fallback_done:
                        # Model doesn't support native tools — switch to text-based
                        print(f"[CHAT] Model {req.model} rejected native tools — switching to text-based")
                        ollama_tools = []
                        inject_text_tool_prompt(messages, available_tool_names)
                        _text_fallback_done = True
                        continue
                    elif any(s in error_body.lower() for s in ("requires more system memory", "out of memory", "llama runner process has terminated", "failed to allocate")) and _oom_retries < 3:
                        # OOM — halve num_ctx and retry (up to 3 times)
                        old_ctx = model_options.get("num_ctx", 0)
                        new_ctx = max(2048, old_ctx // 2)
                        if new_ctx < old_ctx:
                            _oom_retries += 1
                            model_options["num_ctx"] = new_ctx
                            print(f"[CHAT] OOM with num_ctx={old_ctx}, retrying with {new_ctx} (attempt {_oom_retries})")
                            await events.emit(conv_id, "tool_start", {
                                "tool": "processing", "icon": "activity",
                                "status": f"Model needs too much VRAM at {old_ctx} ctx, retrying with {new_ctx}..."
                            })
                            continue
                        else:
                            await events.emit(conv_id, "error", {"status": f"Ollama OOM even at {new_ctx} ctx"})
                            yield f"data: {json.dumps({'type': 'error', 'error': error_body[:300]})}\n\n"
                            return
                    else:
                        err_lower = error_body.lower()
                        print(f"[CHAT] Ollama HTTP {resp.status_code}: {error_body[:300]}")
                        # Mirror the corrupt-model classification from the stream-chunk path —
                        # Ollama can surface the same conditions either inline or as an HTTP error body.
                        if any(s in err_lower for s in ("input stream", "failed to load", "invalid model", "ggml", "unexpected eof")):
                            _corrupt_msg = (
                                f"Model '{req.model}' failed to load — it may be corrupt, incomplete, "
                                f"or hit a context-buffer issue. Try `ollama stop {req.model}` and retry, "
                                f"or reduce num_ctx."
                            )
                            await events.emit(conv_id, "tool_end", {"tool": "processing", "status": _corrupt_msg, "icon": "alert"})
                            yield f"data: {json.dumps({'type': 'error', 'error': _corrupt_msg})}\n\n"
                            return
                        await events.emit(conv_id, "error", {"status": f"Ollama HTTP {resp.status_code}"})
                        yield f"data: {json.dumps({'type': 'error', 'error': error_body[:300]})}\n\n"
                        return
                else:
                    _in_thinking = False
                    _thinking_buf = ""
                    _chunk_buf = ""
                    _repeat_window = ""  # Rolling window for repetition detection
                    _live_gen_tokens = 0  # Live token counter for streaming updates
                    # Tool-enabled rounds stream draft content live. If the
                    # completed round resolves to a tool call, the existing
                    # clear event removes that draft before tool execution.

                    # Pipe aiter_lines into a queue so we can read with timeout
                    # without corrupting the httpx stream (asyncio.wait_for cancels)
                    _line_q = asyncio.Queue()
                    _SENTINEL = object()

                    async def _drain_ollama():
                        try:
                            async for _ol in resp.aiter_lines():
                                await _line_q.put(_ol)
                        except Exception as _drain_err:
                            print(f"[CHAT]   drain_ollama exception: {type(_drain_err).__name__}: {_drain_err!r}")
                            await _line_q.put(("__error__", _drain_err))
                        finally:
                            await _line_q.put(_SENTINEL)

                    _drain_task = asyncio.create_task(_drain_ollama())

                    while True:
                        try:
                            line = await asyncio.wait_for(_line_q.get(), timeout=15)
                        except asyncio.TimeoutError:
                            # No data from Ollama in 15s — send keepalive to prevent browser disconnect
                            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                            continue
                        if line is _SENTINEL:
                            break
                        if isinstance(line, tuple) and len(line) == 2 and line[0] == "__error__":
                            raise line[1]
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue

                        # Detect Ollama error in stream (e.g. CUDA OOM, corrupt model)
                        if chunk.get("error"):
                            _ollama_err = chunk["error"][:300]
                            print(f"[CHAT]   Ollama stream error: {_ollama_err}")
                            # Surface OOM errors clearly
                            if "out of memory" in _ollama_err.lower():
                                _oom_hint = f"GPU out of memory with num_ctx={model_options.get('num_ctx', 'default')}. Try a smaller context size or smaller model."
                                await events.emit(conv_id, "error", {"status": f"GPU OOM: {_oom_hint}"})
                                yield f"data: {json.dumps({'type': 'error', 'error': _oom_hint})}\n\n"
                                return
                            # Corrupt or broken model
                            if any(s in _ollama_err.lower() for s in ("input stream", "failed to load", "invalid model", "ggml", "unexpected eof")):
                                _corrupt_msg = f"Model '{req.model}' failed to load — it may be corrupt or incomplete. Try deleting and re-downloading it."
                                await events.emit(conv_id, "tool_end", {"tool": "processing", "status": _corrupt_msg, "icon": "alert"})
                                yield f"data: {json.dumps({'type': 'error', 'error': _corrupt_msg})}\n\n"
                                return
                            await events.emit(conv_id, "error", {"status": f"Ollama: {_ollama_err[:120]}"})
                            yield f"data: {json.dumps({'type': 'error', 'error': _ollama_err})}\n\n"
                            return

                        msg_chunk = chunk.get("message", {})
                        token = msg_chunk.get("content", "")

                        # Handle Ollama native thinking field (qwen3, qwen3.5, etc.)
                        # These models stream thinking in a separate "thinking" field
                        _thinking_token = msg_chunk.get("thinking", "")
                        if _thinking_token:
                            _thinking_buf += _thinking_token
                            _in_thinking = True
                            _live_gen_tokens += 1
                            if _live_gen_tokens % 10 == 0:
                                yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': _live_gen_tokens, 'prompt_tokens': prompt_tokens, 'live': True})}\n\n"
                            if len(_thinking_buf) % 100 < len(_thinking_token):
                                snip = _thinking_buf[-60:].replace("\n", " ")
                                await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": _thinking_buf[-3000:]})})
                            if not token:
                                continue  # No content yet, just thinking

                        # If we were in native thinking mode and now have content, thinking is done
                        if _in_thinking and token and not _thinking_token:
                            thinking = _thinking_buf
                            _in_thinking = False
                            if thinking:
                                snip = thinking[-60:].replace("\n", " ")
                                await events.emit(conv_id, "thought_done", {
                                    "status": f"💭 {snip}...",
                                    "detail": json.dumps({"thinking": thinking[-2000:]}),
                                })

                        if token:
                            # Handle thinking tokens (inline <think> tags — deepseek, etc.)
                            if "<think>" in token:
                                _in_thinking = True
                                token = token.split("<think>", 1)[1]
                            if _in_thinking:
                                if "</think>" in token:
                                    before_end = token.split("</think>", 1)[0]
                                    after_end = token.split("</think>", 1)[1]
                                    _thinking_buf += before_end
                                    thinking = _thinking_buf
                                    _in_thinking = False
                                    token = after_end
                                    if thinking:
                                        snip = thinking[-60:].replace("\n", " ")
                                        await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": thinking[-3000:]})})
                                else:
                                    _thinking_buf += token
                                    if len(_thinking_buf) % 100 < len(token):
                                        snip = _thinking_buf[-60:].replace("\n", " ")
                                        await events.emit(conv_id, "thinking", {"status": f"💭 {snip}...", "detail": json.dumps({"thinking": _thinking_buf[-3000:]})})
                                    continue

                            if token:
                                content += token
                                _live_gen_tokens += 1

                                # Emit live token count every 10 tokens
                                if _live_gen_tokens % 10 == 0:
                                    yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': _live_gen_tokens, 'prompt_tokens': prompt_tokens, 'live': True})}\n\n"

                                # Repetition detection: catch true degenerate loops (e.g. "the the the…")
                                # without killing legitimate repeated structure (chart labels, JSON arrays,
                                # tables). Skipped inside fenced blocks; outside, requires >85% window
                                # coverage and higher counts for short patterns.
                                _repeat_window = (_repeat_window + token)[-200:]
                                _in_fence = (content.count("```") % 2) == 1
                                if not _in_fence and len(_repeat_window) >= 120:
                                    for plen in range(2, 25):
                                        pat = _repeat_window[-plen:]
                                        # Skip whitespace-only patterns (ASCII art, tables, formatted output)
                                        if not pat.strip():
                                            continue
                                        count = _repeat_window.count(pat)
                                        # Short patterns (≤4 chars) need much higher counts: separators
                                        # like ", " or "}, {" repeat heavily in normal structured prose.
                                        min_count = 20 if plen <= 4 else 8
                                        if count >= min_count and count * plen > len(_repeat_window) * 0.85:
                                            print(f"[CHAT]   Repetition detected: {pat!r} x{count} — stopping generation")
                                            content = content[:content.rfind(pat)]
                                            break
                                    else:
                                        pat = None
                                    if pat:
                                        break

                                if _has_tools:
                                    # Keep the activity pill in token units so
                                    # it matches the header context meter.
                                    if len(content) % 200 < len(token):
                                        _evt_type = "tool_start" if not _gen_pill_started else "tool_progress"
                                        _gen_pill_started = True
                                        _shown_tokens = max(_live_gen_tokens, (len(content) + 3) // 4)
                                        await events.emit(conv_id, _evt_type, {
                                            "tool": "generating",
                                            "status": f"✍️ Generating... (~{_shown_tokens} tokens)",
                                            "icon": "edit",
                                        })

                                # Stream assistant text live even when tools
                                # are enabled. Tool-call rounds clear this
                                # provisional text after parsing completes.
                                _chunk_buf += token
                                if len(_chunk_buf) >= 8 or chunk.get("done"):
                                    yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
                                    _streamed_content = True
                                    _chunk_buf = ""
                                    await asyncio.sleep(0)

                        # Track token counts from Ollama
                        if chunk.get("done"):
                            if _chunk_buf:
                                yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
                                _streamed_content = True
                                _chunk_buf = ""
                            # Finalize thinking if we were in native thinking mode
                            if _in_thinking and _thinking_buf:
                                thinking = _thinking_buf
                                _in_thinking = False
                            gen_tokens = chunk.get("eval_count", 0)
                            prompt_tokens = chunk.get("prompt_eval_count", 0)
                            if gen_tokens or prompt_tokens:
                                yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': gen_tokens, 'prompt_tokens': prompt_tokens})}\n\n"

                        if msg_chunk.get("tool_calls"):
                            tool_calls = msg_chunk["tool_calls"]
                        if chunk.get("done"):
                            if msg_chunk.get("tool_calls"):
                                tool_calls = msg_chunk["tool_calls"]
                            break

                    # Clean up the drain task
                    if not _drain_task.done():
                        _drain_task.cancel()
                        try:
                            await _drain_task
                        except (asyncio.CancelledError, Exception):
                            pass

        except _ProviderStreamComplete:
            pass
        except Exception as e:
            # Log the actual cause — previously this catch silently emitted str(e) to the
            # client, leaving the journal blank when Ollama streams died in unexpected ways.
            print(f"[CHAT]   Round {round_num} exception: {type(e).__name__}: {e!r}")
            traceback.print_exc()
            err_msg = str(e) or f"{type(e).__name__} (no message)"
            err_lower = err_msg.lower()
            _provider_label = model_providers.provider_label(_model_provider) if _is_cloud_provider else "Ollama"
            # Provide clearer error messages for common failures
            if not _is_cloud_provider and ("peer closed" in err_lower or "incomplete chunked" in err_lower):
                err_msg = (f"Ollama connection dropped while streaming model '{req.model}'. "
                           f"This usually means the model is too large for available GPU memory (VRAM). "
                           f"Try a smaller model or reduce num_ctx. Original: {err_msg[:200]}")
            elif "timeout" in err_lower or "timed out" in err_lower:
                err_msg = f"{_provider_label} request timed out for model '{req.model}'. The model may be too slow or overloaded. {err_msg[:200]}"
            elif not _is_cloud_provider and ("input stream" in err_lower or "failed to load" in err_lower or "ggml" in err_lower):
                err_msg = (f"Model '{req.model}' failed mid-stream — it may be corrupt or hit a context-buffer issue. "
                           f"Try `ollama stop {req.model}` and retry, or reduce num_ctx. Original: {err_msg[:200]}")
            # Persist whatever streamed before the failure and clear the
            # in_progress flag — otherwise the stub reloads empty/stale and the
            # user loses the partial answer they were watching.
            if _assistant_msg_id is not None:
                try:
                    _partial = content if content.strip() else ""
                    _final = (_partial + f"\n\n_[interrupted: {err_msg[:160]}]_") if _partial \
                        else f"_[no response — {err_msg[:160]}]_"
                    await db.update_message(_assistant_msg_id, content=_final,
                                            metadata={"in_progress": False, "stream_error": True})
                except Exception as _pe:
                    print(f"[CHAT]   partial-persist failed (non-fatal): {_pe}")
            await events.emit(conv_id, "error", {"status": f"{_provider_label}: {err_msg[:200]}"})
            yield f"data: {json.dumps({'type': 'error', 'error': err_msg})}\n\n"
            return

        # Build the full message object for conversation history
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls

        # ── Text-based tool call fallback ──
        _text_tool_call_round = False
        if not tool_calls and content and available_tool_names:
            tool_calls = parse_text_tool_calls(content, available_tool_names)
            if tool_calls:
                _text_tool_call_round = True
                content = strip_tool_calls(content)
                # Clean up residual garbage (backticks, braces, etc.) left after stripping
                cleaned_residue = re.sub(r'[`{}\[\]\s"\']', '', content).strip()
                if len(cleaned_residue) < 10:
                    content = ""
                msg["content"] = content
                for tc in tool_calls:
                    print(f"[CHAT]   text-parsed tool call: {tc['function']['name']}")

        # ── Drop hallucinated tool calls when no tools are available ──
        _hallucinated_dropped = False
        if tool_calls and not available_tool_names:
            print(f"[CHAT]   Ignoring {len(tool_calls)} hallucinated tool calls — no tools available")
            tool_calls = []
            _hallucinated_dropped = True

        # ── Code block rescue: when model dumps code in chat instead of using tools ──
        # Skip rescue if model was just told to stop looping, or if generate_code already failed
        # (prevents infinite loop: generate_code fails -> model dumps code -> rescue -> execute -> fail -> repeat)
        if not tool_calls and content and _has_full_codeagent and _rescue_count < 3 and _generate_code_fail_rounds < 1:
            code_blocks = re.findall(r'```(\w*)\n(.*?)```', content, re.DOTALL)
            if code_blocks and not any(cb[1].strip().startswith('{') for cb in code_blocks):
                # Model wrote code blocks without making tool calls — rescue via write_file + run_shell
                for lang, code in code_blocks:
                    code = code.strip()
                    if not code or len(code) < 30:
                        continue
                    # Skip if it looks like ASCII art or output, not real code
                    if not any(kw in code for kw in ("import ", "def ", "class ", "print(", "return ", "function ", "const ", "let ", "var ", "for ", "while ", "if ", "#!", "from ")):
                        continue
                    # Determine language
                    exec_lang = lang.lower() if lang else "python"
                    if exec_lang in ("", "text", "txt", "markdown", "md"):
                        exec_lang = "python"
                    # Only rescue if it looks like actual code (not prose)
                    if exec_lang in ("python", "python3", "py", "javascript", "js", "bash", "sh",
                                     "rust", "go", "java", "c", "cpp", "ruby", "php", "typescript", "ts"):
                        _ext_map = {"python": "py", "python3": "py", "py": "py", "javascript": "js", "js": "js",
                                    "bash": "sh", "sh": "sh", "typescript": "ts", "ts": "ts",
                                    "rust": "rs", "go": "go", "c": "c", "cpp": "cpp", "java": "java",
                                    "ruby": "rb", "php": "php"}
                        filepath = f"/root/_rescued_{round_num}.{_ext_map.get(exec_lang, 'py')}"
                        if exec_lang in ("python", "python3", "py"):
                            run_cmd = f"python3 {filepath}"
                        elif exec_lang in ("bash", "sh"):
                            run_cmd = f"bash {filepath}"
                        elif exec_lang in ("javascript", "js"):
                            run_cmd = f"node {filepath}"
                        elif exec_lang in ("typescript", "ts"):
                            run_cmd = f"npx ts-node {filepath}"
                        elif exec_lang == "go":
                            run_cmd = f"go run {filepath}"
                        elif exec_lang == "rust":
                            run_cmd = f"rustc {filepath} -o /root/_rescued_{round_num} && /root/_rescued_{round_num}"
                        elif exec_lang in ("c", "cpp"):
                            compiler = "gcc" if exec_lang == "c" else "g++"
                            run_cmd = f"{compiler} {filepath} -o /root/_rescued_{round_num} && /root/_rescued_{round_num}"
                        else:
                            run_cmd = f"python3 {filepath}"
                        tool_calls = [
                            {"function": {"name": "write_file", "arguments": {"path": filepath, "content": code}}},
                            {"function": {"name": "run_shell", "arguments": {"command": run_cmd}}},
                        ]
                        _rescue_count += 1
                        print(f"[CHAT]   code-block-rescue: extracted {exec_lang} code ({len(code)} chars) → write_file + run_shell")
                        # Inject feedback so the model learns to use tools directly
                        messages.append({"role": "tool", "content": "SYSTEM: Your code was rescued from chat text. Use tools (write_file, execute_code, generate_code) directly — never put code in chat."})
                        content = ""
                        msg["content"] = ""
                        break
                # Past max rescue attempts → inject stern message instead
                if not tool_calls and _rescue_count >= 3:
                    messages.append({"role": "tool", "content": "SYSTEM: STOP writing code in chat text. You MUST use tools. Call write_file or execute_code for ALL code."})
                    content = ""
                    msg["content"] = ""

        print(f"[CHAT] Round {round_num}: content={len(content)} thinking={len(thinking)} tool_calls={len(tool_calls)} gen_tokens={gen_tokens} prompt_tokens={prompt_tokens}")
        if thinking and not ephemeral:
            print(f"[CHAT]   thinking: {thinking[:200]!r}")
        if content and not ephemeral:
            print(f"[CHAT]   content: {content[:200]!r}")
        if tool_calls and not ephemeral:
            print(f"[CHAT]   tool_calls: {json.dumps(tool_calls)[:300]}")

        # Phase 0.6: snapshot the assistant message at every round boundary.
        # On disconnect, the most recent snapshot is what the frontend renders
        # when the user reopens the conversation. We tag it with run_ids of any
        # runs created during this stream so the RunCard component can rebuild.
        if _assistant_msg_id is not None:
            try:
                _runs_now = await db.get_runs_by_conversation(conv_id, limit=20)
                _stream_run_ids = [r["id"] for r in _runs_now
                                    if r.get("started_at", "") >= _stream_started_at]
                await db.update_message(_assistant_msg_id,
                    content=content,
                    metadata={
                        "stream_started_at": _stream_started_at,
                        "in_progress": True,
                        "round": round_num,
                        "run_ids": _stream_run_ids,
                    })
            except Exception as _use:
                print(f"[CHAT]   round-save failed (non-fatal): {_use}")

        # Emit final thinking content
        if thinking:
            await events.emit(conv_id, "thought_done", {
                "status": thinking[-80:].replace("\n", " ") + ("..." if len(thinking) > 80 else ""),
                "detail": json.dumps({"thinking": thinking}),
            })

        if tool_calls:
            # ── Guard: after successful generate_code, allow review + re-invocation ──
            # (No longer blocking tools — the overseer needs to review and potentially re-invoke)

            # ── Duplicate / near-duplicate detection ──
            if tool_calls:
                _tool_key = json.dumps([(tc.get("function", {}).get("name"), json.dumps(tc.get("function", {}).get("arguments", {}), sort_keys=True)) for tc in tool_calls], sort_keys=True)
                _tc_names_dup = [tc.get("function", {}).get("name", "") for tc in tool_calls]

                # The v2 fix loop is review → fixer → review → fixer …, so
                # run_review, run_acceptance_review, run_fixer, and run_aider_fix are designed to be called multiple
                # times with identical arguments. The v2 workflow gate (in
                # tools.py) enforces correctness here; the duplicate detector
                # should NOT second-guess it. Exempt these from both exact
                # and near-duplicate checks. ask_project is also exempted
                # because Q&A is naturally multi-turn (follow-up questions
                # often share keywords/topics).
                _LOOP_TOOLS = {"run_review", "run_acceptance_review", "run_fixer", "run_aider_fix", "ask_project"}
                _all_loop_tools = bool(_tc_names_dup) and all(
                    n in _LOOP_TOOLS for n in _tc_names_dup
                )

                # Exact duplicate: same as immediately previous round
                _is_dup = _tool_key == _prev_tool_key
                # Near-duplicate: same tool key seen in last 3 rounds
                # BUT: allow re-running test commands (run_shell/execute_code)
                # if a write_file happened in between (file was modified)
                if not _is_dup and _tool_key in _tool_history:
                    _is_test_rerun = all(n in ("run_shell", "execute_code") for n in _tc_names_dup)
                    _had_write_since = _prev_tool_key != _tool_key and any(
                        '"write_file"' in h or '"file_editor"' in h
                        for h in _tool_history[_tool_history.index(_tool_key)+1:]
                    ) if _tool_key in _tool_history else False
                    if _is_test_rerun and _had_write_since:
                        print(f"[CHAT]   Allowing re-test after file modification")
                    elif _all_loop_tools:
                        print(f"[CHAT]   Allowing re-run of v2 loop tool(s): {_tc_names_dup}")
                    else:
                        _is_dup = True
                        print(f"[CHAT]   Near-duplicate detected (seen in last 3 rounds)")

                # Even an exact back-to-back duplicate is fine for loop tools —
                # the gate will refuse if the workflow is wrong.
                if _is_dup and _all_loop_tools:
                    print(f"[CHAT]   Allowing exact re-run of v2 loop tool(s) "
                          f"(gate enforces workflow): {_tc_names_dup}")
                    _is_dup = False

                if _is_dup:
                    _dup_break_count += 1
                    print(f"[CHAT]   Duplicate tool call detected (#{_dup_break_count}) — breaking loop")
                    if _dup_break_count >= 2:
                        if await _is_active_uploaded_manual_loop(conv_id, _tc_names_dup):
                            print("[CHAT]   Repeated uploaded-project manual tool duplicate — stopping turn")
                            await _mark_latest_uploaded_workflow_blocked(conv_id)
                            _dup_summary = await _duplicate_manual_tool_summary(conv_id, _tc_names_dup)
                            yield f"data: {json.dumps({'type': 'token', 'content': _dup_summary})}\n\n"
                            if _assistant_msg_id is not None:
                                try:
                                    await db.update_message(
                                        _assistant_msg_id,
                                        content=_dup_summary,
                                        metadata={
                                            "stream_started_at": _stream_started_at,
                                            "in_progress": False,
                                            "blocked_loop": True,
                                            "blocked_tool": ",".join(_tc_names_dup),
                                            "round": round_num,
                                        },
                                    )
                                except Exception as _de:
                                    print(f"[CHAT] duplicate-loop persist failed (non-fatal): {_de}")
                            await events.emit(conv_id, "complete", {"status": "Complete (blocked)"})
                            yield f"data: {json.dumps({'type': 'done', 'model': req.model, 'message_id': _assistant_msg_id, 'blocked': True})}\n\n"
                            return
                        messages.append({"role": "tool", "content": "STOP. You are stuck in a loop. Summarize what you accomplished and respond to the user NOW. Do not call any more tools."})
                    else:
                        messages.append({"role": "tool", "content": "You already called this tool with the same arguments. Do NOT repeat the same call. Provide your final response to the user now."})
                    continue

                _prev_tool_key = _tool_key
                _tool_history.append(_tool_key)
                if len(_tool_history) > 5:
                    _tool_history.pop(0)

            if _streamed_content and _text_tool_call_round:
                yield f"data: {json.dumps({'type': 'clear'})}\n\n"
            if content:
                cleaned = re.sub(r'```\w*\n.*?```', '', content, flags=re.DOTALL).strip()
                msg["content"] = cleaned

            messages.append(msg)
            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

            # ── Classify tools for parallel vs sequential execution ──
            _parsed_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except (json.JSONDecodeError, ValueError):
                        if not ephemeral:
                            print(f"[CHAT] Warning: failed to parse tool args JSON for {tool_name}: {tool_args[:200]!r}")
                        tool_args = {}
                _parsed_calls.append((tool_name, tool_args))

            # Check if all calls are parallel-safe (different read targets)
            _all_parallel = (
                len(_parsed_calls) > 1
                and all(n in _PARALLEL_SAFE_TOOLS for n, _ in _parsed_calls)
            )
            # Also allow parallel write_file if all paths are different
            if not _all_parallel and len(_parsed_calls) > 1:
                _names = [n for n, _ in _parsed_calls]
                if all(n in (_PARALLEL_SAFE_TOOLS | {"write_file"}) for n in _names):
                    _write_paths = [a.get("path", "") for n, a in _parsed_calls if n == "write_file"]
                    if len(_write_paths) == len(set(_write_paths)):
                        _all_parallel = True

            if _all_parallel:
                print(f"[CHAT]   Running {len(_parsed_calls)} tools in parallel")

            for batch_start in range(0, len(_parsed_calls), max(1, len(_parsed_calls) if _all_parallel else 1)):
                batch_end = len(_parsed_calls) if _all_parallel else batch_start + 1
                batch = _parsed_calls[batch_start:batch_end]

                _futures = []
                _metas = []  # (tool_name, tool_args, icon, label, detail)
                for tool_name, tool_args in batch:
                    # Block unauthorized tools
                    if tool_name not in available_tool_names:
                        print(f"[CHAT]   Blocked unauthorized tool: {tool_name} (allowed: {sorted(available_tool_names)})")
                        messages.append({"role": "tool", "content": f"Error: tool '{tool_name}' is not available in this session."})
                        continue

                    if ephemeral:
                        print(f"[CHAT]   Executing tool: {tool_name}(args redacted for ghost mode)")
                    else:
                        print(f"[CHAT]   Executing tool: {tool_name}({json.dumps(tool_args)[:200]})")

                    _tool_icon, _tool_label = _TOOL_ICONS.get(tool_name, ("tool", f"🔧 Running {tool_name}"))
                    _tool_detail = ""
                    if tool_name == "run_shell":
                        _tool_detail = f": {tool_args.get('command', '')[:60]}"
                    elif tool_name == "execute_code":
                        _tool_detail = f" ({tool_args.get('language', 'code')})"
                    elif tool_name == "write_file":
                        _tool_detail = f": {tool_args.get('path', '')}"
                    elif tool_name == "generate_code":
                        _tool_detail = f" ({tool_args.get('language', '')})"

                    await events.emit(conv_id, "tool_start", {
                        "tool": tool_name, "icon": _tool_icon,
                        "status": f"{_tool_label}{_tool_detail}",
                    })

                    # Execute via integrated CodeAgent — with keepalive loop
                    _tf = asyncio.get_running_loop().create_future()
                    _tool_chars = [0]
                    async def _run_tool_bg(_n=tool_name, _a=tool_args, _c=conv_id, _f=_tf, _tc=_tool_chars, _kb=persona_kb_ids):
                        try:
                            r = await exec_tool(http, events, _n, _a, _c, custom_tool_map, connector_tool_name_map=connector_tool_name_map, conv_model=req.model, kb_ids=_kb, artifact_message_id=_assistant_msg_id)
                            _tc[0] = len(r) if r else 0
                            if not _f.done(): _f.set_result(r)
                        except Exception as _e:
                            if not _f.done(): _f.set_exception(_e)

                    _spawn_bg(_run_tool_bg())
                    _futures.append((_tf, _tool_chars, tool_name, _tool_icon, _tool_label, _tool_detail))

                # Wait for all futures in this batch
                _base_ctx = sum(len(m.get("content", "")) for m in messages) // 4
                _loop = asyncio.get_running_loop()
                _tool_start_time = _loop.time()
                while _futures and not all(f[0].done() for f in _futures):
                    await asyncio.sleep(2)
                    for _tf, _tool_chars, _tn, _ti, _tl, _td in _futures:
                        if not _tf.done():
                            _elapsed = _loop.time() - _tool_start_time
                            if _tn != "generate_code":
                                await events.emit(conv_id, "tool_progress", {
                                    "tool": _tn, "icon": _ti,
                                    "status": f"{_tl}{_td} ({int(_elapsed)}s)",
                                })
                    _est_tool_tokens = sum(max(tc[0] // 4, 50) for _, tc, *_ in _futures)
                    _est_ctx = _base_ctx + _est_tool_tokens
                    yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': 0, 'prompt_tokens': _est_ctx, 'live': True})}\n\n"
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

                # Collect results in order
                for _fi, (_tf, _tool_chars, tool_name, _tool_icon, _tool_label, _tool_detail) in enumerate(_futures):
                    try:
                        tool_result = _tf.result()
                    except Exception as te:
                        tool_result = f"**Tool error ({tool_name}):** {str(te)}"

                    # Truncate huge results — keep head + tail (errors are usually at the bottom)
                    MAX_TOOL_RESULT = 24000
                    if len(tool_result) > MAX_TOOL_RESULT:
                        orig_len = len(tool_result)
                        head = tool_result[:4000]
                        tail = tool_result[-8000:]
                        tool_result = head + f"\n\n[... {orig_len - 12000} chars omitted ...]\n\n" + tail

                    messages.append({"role": "tool", "content": tool_result})
                    print(f"[CHAT]   Tool result ({tool_name}): {len(tool_result)} chars")

                    if _record_blocked_tool_result(_blocked_tool_state, tool_name, tool_result):
                        print(f"[CHAT]   Repeated BLOCKED tool result for {tool_name} — stopping turn")
                        await _mark_latest_uploaded_workflow_blocked(conv_id)
                        _blocked_summary = await _blocked_tool_summary(conv_id, tool_name, tool_result)
                        yield f"data: {json.dumps({'type': 'token', 'content': _blocked_summary})}\n\n"
                        if _assistant_msg_id is not None:
                            try:
                                await db.update_message(
                                    _assistant_msg_id,
                                    content=_blocked_summary,
                                    metadata={
                                        "stream_started_at": _stream_started_at,
                                        "in_progress": False,
                                        "blocked_loop": True,
                                        "blocked_tool": tool_name,
                                        "round": round_num,
                                    },
                                )
                            except Exception as _be:
                                print(f"[CHAT] blocked-loop persist failed (non-fatal): {_be}")
                        await events.emit(conv_id, "complete", {"status": "Complete (blocked)"})
                        yield f"data: {json.dumps({'type': 'done', 'model': req.model, 'message_id': _assistant_msg_id, 'blocked': True})}\n\n"
                        return

                    # After plan_project: nudge model to use generate_code for large projects
                    if tool_name == "plan_project" and len(tool_result) > 1000:
                        # Count file references in the plan to gauge project size
                        _file_refs = tool_result.count(".py") + tool_result.count(".js") + tool_result.count(".ts") + tool_result.count(".html") + tool_result.count(".go") + tool_result.count(".rs")
                        if _file_refs >= 3:
                            messages.append({"role": "tool", "content": (
                                "SYSTEM: This is a large project with multiple files. "
                                "Use generate_code to build it — the coding agent will implement "
                                "the entire plan autonomously. Pass the full task description and "
                                "language. After it finishes, review the output, run tests, and deliver."
                            )})
                            print(f"[CHAT]   plan_project returned large plan ({_file_refs} file refs) — nudging to generate_code")

                    # Track generate_code failures — temporarily disable code-block-rescue
                    if tool_name == "generate_code" and tool_result.startswith("ERROR"):
                        _generate_code_fail_rounds = -2
                        _rescue_count = 0
                        print("[CHAT]   generate_code failed — pausing code-block-rescue for 2 rounds")
                    elif _generate_code_fail_rounds < 0:
                        _generate_code_fail_rounds += 1

                    # When generate_code succeeds (PROJECT COMPLETE), enable overseer review
                    if tool_name == "generate_code" and "PROJECT COMPLETE" in tool_result:
                        _generate_code_done = True
                        print("[CHAT]   generate_code succeeded — overseer reviewing output")
                        messages.append({"role": "tool", "content": (
                            "SYSTEM: The coding agent has finished. Review the file contents above "
                            "and evaluate whether the output matches the user's request. "
                            "If the code is just boilerplate/scaffolding and doesn't implement "
                            "the actual features requested, call generate_code again with the same "
                            "project_id and a more detailed task. Otherwise present the results."
                        )})

                if _all_parallel:
                    break  # All were in one batch

                # Detect repeated errors — inject guidance, then force-stop if stuck
                if tool_name in ("execute_code", "run_shell") and ("FAILED" in tool_result or "Error" in tool_result or "Traceback" in tool_result):
                    _err_lines = [l.strip() for l in tool_result.splitlines() if l.strip() and ("Error" in l or "FAILED" in l)]
                    _err_sig = _err_lines[-1][:80] if _err_lines else tool_result[:80]
                    if _err_sig == _last_error_sig:
                        _error_repeat_count += 1
                    else:
                        _last_error_sig = _err_sig
                        _error_repeat_count = 1

                    if _error_repeat_count >= 3:
                        print(f"[CHAT]   Same error repeated {_error_repeat_count}x — force stopping tool loop")
                        _hint = ""
                        if "EOFError" in tool_result or "EOF when reading" in tool_result:
                            _hint = "STOP. The sandbox has NO stdin. input() will ALWAYS crash. Use write_file to save the script, then run_shell to run it with hardcoded test values. Do NOT use execute_code for scripts that need input."
                        elif "IndexError" in tool_result and "argv" in tool_result:
                            _hint = "STOP. execute_code does NOT support command-line arguments — sys.argv only contains the script name. Use write_file to save the script, then run_shell(command='python3 /root/script.py arg1 arg2') to run it with arguments."
                        elif "ModuleNotFoundError" in tool_result or "No module named" in tool_result:
                            _hint = "STOP. Install the missing package with run_shell(command='pip3 install <package>') BEFORE running the code."
                        else:
                            _hint = f"STOP. You've hit the same error {_error_repeat_count} times. You MUST try a completely different approach. Explain what went wrong and what you'll do differently."
                        messages.append({"role": "tool", "content": f"SYSTEM: {_hint}"})
                    elif _error_repeat_count == 2:
                        # First repeat — gentle nudge
                        if "argv" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: execute_code has no command-line arguments. Use write_file + run_shell instead for scripts that need sys.argv."})
                        elif "EOFError" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: input() does not work in this sandbox. Use hardcoded values or sys.argv via write_file + run_shell."})
                    elif _error_repeat_count == 1:
                        # First occurrence — provide specific guidance
                        if "ConnectionRefusedError" in tool_result or "ECONNREFUSED" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: Connection refused. The server isn't running. Start it first with run_shell, or check the host/port."})
                        elif "FileNotFoundError" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: File doesn't exist. Check the path with list_files, or create it with write_file first."})
                        elif "SyntaxError" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: Syntax error at the line shown. Fix the code with write_file and re-run."})
                        elif "PermissionError" in tool_result:
                            messages.append({"role": "tool", "content": "HINT: Permission denied. Check the file path and permissions. Try using /root/ for output files."})
                else:
                    _last_error_sig = None
                    _error_repeat_count = 0

                # Emit ctx_update after tool result so frontend token counter updates live
                _est_prompt = sum(len(m.get("content", "")) for m in messages) // 4
                yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': 0, 'prompt_tokens': _est_prompt, 'live': True})}\n\n"

                # Auto-index research results into persona's RAG memory
                if (not ephemeral
                        and req.persona_id
                        and tool_name in rag.RESEARCH_TOOLS
                        and len(tool_result) > 100):
                    try:
                        _query_for_index = ""
                        if isinstance(tool_args, dict):
                            _query_for_index = tool_args.get("query", "") or tool_args.get("url", "") or tool_args.get("topic", "")
                        _spawn_bg(
                            rag.index_research(req.persona_id, tool_name, _query_for_index, tool_result, conv_id)
                        )
                    except Exception as _rag_e:
                        print(f"[RAG] Auto-index error: {_rag_e}")

            # Short-circuit: if ask_project succeeded as a non-change-request
            # in this round, stream the QA envelope's answer verbatim and exit
            # the agent loop. The QA run card already renders the rich grounded
            # answer with code blocks and citations; without this, the LLM
            # would paraphrase it on the next round and lose that detail.
            _called_ask_project = any(
                _f[2] == "ask_project" for _f in (_futures or [])
            )
            if _called_ask_project:
                try:
                    _qa_runs = await db.get_runs_by_conversation(conv_id, limit=10)
                    for _qr in _qa_runs:
                        if _qr.get("role") != "qa":
                            continue
                        # Only short-circuit when the QA succeeded AND wasn't a
                        # change request AND belongs to this stream — without
                        # the stream-start filter, a failed ask_project this
                        # turn would replay the previous turn's stale answer.
                        if _qa_run_qualifies(_qr, _stream_started_at):
                            _qa_env = _qr.get("result_envelope") or {}
                            _qa_answer = _qa_env["answer"]
                            print(f"[CHAT]   QA short-circuit: streaming "
                                  f"{len(_qa_answer)} chars from {_qr.get('id')}")
                            for _i in range(0, len(_qa_answer), 8):
                                yield f"data: {json.dumps({'type': 'token', 'content': _qa_answer[_i:_i+8]})}\n\n"
                                await asyncio.sleep(0)
                            if _assistant_msg_id is not None:
                                try:
                                    await db.update_message(
                                        _assistant_msg_id,
                                        content=_qa_answer,
                                        metadata={
                                            "stream_started_at": _stream_started_at,
                                            "in_progress": False,
                                            "qa_short_circuit": True,
                                            "qa_run_id": _qr.get("id"),
                                        },
                                    )
                                except Exception as _pe:
                                    print(f"[CHAT]   QA persist failed (non-fatal): {_pe}")
                            await events.emit(conv_id, "complete", {"status": "Complete"})
                            _done_payload = {
                                "type": "done", "model": req.model,
                                "message_id": _assistant_msg_id,
                            }
                            yield f"data: {json.dumps(_done_payload)}\n\n"
                            return
                        # Latest qa run didn't qualify — fall through to LLM
                        break
                except Exception as _qe:
                    print(f"[CHAT]   QA short-circuit lookup failed (non-fatal): {_qe}")

            continue

        # No tool calls — we have a final response
        if content:
            # If content was buffered (tool mode), flush it now as the final answer
            if _has_tools and not _streamed_content:
                for i in range(0, len(content), 8):
                    yield f"data: {json.dumps({'type': 'token', 'content': content[i:i+8]})}\n\n"
                    await asyncio.sleep(0)

            # ── Effort-level self-review: loop back with a critique prompt ──
            if _review_round < _review_budget:
                # ── Anti-regression guard ──
                # When the first answer was already correct, later review rounds
                # tend to shrink into terse meta-commentary ("the original is
                # accurate, no changes needed"). If the just-produced content is
                # drastically shorter than the best detailed answer we've seen,
                # treat this round as a "nothing to fix" confirmation: restore
                # the best content, skip further review, and emit the done event.
                if (len(_best_review_content) >= 500
                        and len(content) < int(0.5 * len(_best_review_content))):
                    print(f"[CHAT]   Review round {_review_round + 1} regressed "
                          f"({len(content)} vs best {len(_best_review_content)} chars) — "
                          f"keeping best answer, stopping refinement")
                    yield f"data: {json.dumps({'type': 'clear'})}\n\n"
                    for i in range(0, len(_best_review_content), 8):
                        yield f"data: {json.dumps({'type': 'token', 'content': _best_review_content[i:i+8]})}\n\n"
                        await asyncio.sleep(0)
                    content = _best_review_content
                    msg["content"] = _best_review_content
                    # Fall through to the finalization block below
                else:
                    # Update best-seen content if this round is at least as good
                    if len(content) >= len(_best_review_content):
                        _best_review_content = content
                    _review_round += 1
                    print(f"[CHAT]   Review round {_review_round}/{_review_budget} — re-examining answer")
                    # Commit the just-finished answer to history so the model sees what it said
                    messages.append(msg)
                    # Frontend: wipe the streamed answer, then show refinement progress.
                    # Only emit via the chat stream — the frontend synthesizes the pill
                    # from refinement_start. An extra EventBus emit would duplicate it.
                    yield f"data: {json.dumps({'type': 'clear'})}\n\n"
                    yield f"data: {json.dumps({'type': 'refinement_start', 'round': _review_round, 'total': _review_budget})}\n\n"
                    # Inject the critique prompt as a user turn
                    messages.append({
                        "role": "user",
                        "content": (
                            "Re-examine your previous response for factual errors, logical "
                            "gaps, inaccurate claims, unclear phrasing, missing context, or "
                            "overlooked angles. Then output the FINAL user-facing answer.\n\n"
                            "CRITICAL RULES for your output:\n"
                            "1. Your output IS the final answer shown to the user. It replaces "
                            "the previous answer entirely.\n"
                            "2. Do NOT write meta-commentary like 'the previous answer is "
                            "correct', 'no changes needed', 'I verified...' — just output "
                            "the answer itself.\n"
                            "3. If the previous answer was already accurate and complete, "
                            "output it AGAIN in full (same level of detail, tables, examples) "
                            "— do not shorten or summarize it.\n"
                            "4. If you found issues, output the improved full answer.\n"
                            "5. You may call tools (research, fetch_url, execute_code) first "
                            "to verify facts before writing the final answer."
                        ),
                    })
                    continue

            messages.append(msg)
            # Record token usage for analytics
            if (gen_tokens or prompt_tokens) and not ephemeral:
                try:
                    await db.record_token_usage(conv_id, req.model, getattr(req, 'persona_id', '') or '', prompt_tokens, gen_tokens)
                except Exception as _te:
                    print(f"[CHAT] Token recording error: {_te}")
            if not ephemeral:
                # Memory extraction is best-effort and used to block the turn
                # for up to 90s (two 45s LLM calls) AFTER the answer was visibly
                # done. Run it in the background so `done` fires immediately;
                # the suggesters emit their own SSE events over the persistent
                # conversation bus.
                if global_memory_enabled:
                    _spawn_bg(_suggest_global_memories(
                        req, http, events, conv_id,
                        last_user_msg, content, _assistant_msg_id,
                    ))
                _spawn_bg(_suggest_workspace_memories(
                    req, http, events, conv_id,
                    workspace_memory_info.get("workspace"),
                    last_user_msg, content, _assistant_msg_id,
                ))
            await events.emit(conv_id, "complete", {"status": "Complete"})
            _done_payload = {"type": "done", "model": req.model, "message_id": _assistant_msg_id}
            if _review_round > 0:
                _done_payload["refinements"] = _review_round
            yield f"data: {json.dumps(_done_payload)}\n\n"
            return
        else:
            # Empty content — try to recover
            if round_num >= 3:
                # Don't leave an empty in_progress stub behind on reload.
                if _assistant_msg_id is not None and not (content or "").strip():
                    try:
                        await db.update_message(
                            _assistant_msg_id,
                            content="_[the model produced no text response]_",
                            metadata={"in_progress": False, "empty_response": True},
                        )
                    except Exception:
                        pass
                await events.emit(conv_id, "complete", {"status": "Complete"})
                _done_payload = {"type": "done", "model": req.model, "message_id": _assistant_msg_id}
                if _review_round > 0:
                    _done_payload["refinements"] = _review_round
                yield f"data: {json.dumps(_done_payload)}\n\n"
                return
            print(f"[CHAT]   Empty content (round {round_num}), gen_tokens={gen_tokens}, thinking={len(thinking)}")

            # Model over-thought: produced thinking but no content
            if thinking and not content:
                print(f"[CHAT]   Over-thought ({len(thinking)} chars thinking, 0 content) — nudging")
                messages.append({"role": "assistant", "content": ""})
                if _hallucinated_dropped:
                    messages.append({"role": "user", "content": "You do NOT have any tools. Do NOT attempt to call functions or tools. Answer the question directly using the web search results already provided in the conversation. Summarize what the search results say. Do NOT claim you lack data — the search results ARE your data. Respond in plain text NOW."})
                else:
                    messages.append({"role": "user", "content": "You were thinking but didn't produce a response. Please answer concisely now."})
                if "num_predict" not in model_options:
                    model_options["num_predict"] = 4096
                continue

            # Zero tokens with native tools — switch to text-based
            if gen_tokens == 0 and ollama_tools:
                print(f"[CHAT]   Zero tokens with native tools — switching to text-based")
                ollama_tools = []
                inject_text_tool_prompt(messages, available_tool_names)
                continue

            # Nudge the model to respond
            if _has_full_codeagent:
                messages.append({"role": "user", "content": "Use your tools to accomplish the task. Call execute_code, write_file, or run_shell now."})
            else:
                messages.append({"role": "user", "content": "Please provide a response."})
            continue

    # Reached MAX_ROUNDS without the model emitting final user-facing text.
    # Synthesize a fallback summary so the conversation isn't a blank message —
    # tell the user we ran out of round budget, list the runs created, and
    # what the last reviewer (if any) reported. Stream + persist.
    _fallback_lines = [
        f"⚠️ **Ran out of round budget** ({MAX_ROUNDS} rounds) before reaching a final answer.",
    ]
    try:
        _all_runs = await db.get_runs_by_conversation(conv_id, limit=30)
        # Only count runs that were created during this stream.
        _stream_runs = [r for r in _all_runs if r.get("started_at", "") >= _stream_started_at]

        _builders = [r for r in _stream_runs if r.get("role", "").startswith("builder")]
        _reviewers = [r for r in _stream_runs if r.get("role") == "reviewer"]

        if _builders:
            _b_succ = sum(1 for r in _builders if r.get("status") == "succeeded")
            _b_fail = len(_builders) - _b_succ
            _fallback_lines.append(
                f"\n**Builder runs**: {len(_builders)} "
                f"({_b_succ} succeeded, {_b_fail} failed). "
                f"Latest project workspace: `{(_builders[0].get('result_envelope') or {}).get('project_dir', '?')}`."
            )

        if _reviewers:
            _last_rev = _reviewers[0]
            _env = _last_rev.get("result_envelope") or {}
            _rstatus = _env.get("status", "?")
            _issues = _env.get("issues") or []
            _fallback_lines.append(
                f"\n**Last reviewer run**: status=`{_rstatus}` "
                f"(build exit={_env.get('build_exit', '?')}, "
                f"test exit={_env.get('test_exit', '?')})."
            )
            if _issues:
                _fallback_lines.append(f"\nReviewer flagged {len(_issues)} issue(s):")
                for _i, _iss in enumerate(_issues[:5], 1):
                    _fallback_lines.append(
                        f"  {_i}. [{_iss.get('severity', '?')}] "
                        f"{_iss.get('file', '?')}"
                        + (f":{','.join(str(x) for x in _iss.get('lines') or [])}" if _iss.get('lines') else "")
                        + f" — {_iss.get('summary', '')[:160]}"
                    )
        elif _builders:
            _fallback_lines.append(
                "\nNo reviewer run was completed — the project's build/test status was not verified."
            )

        _fallback_lines.append(
            "\n**To continue**: ask me to pick up where I left off, or refine the request. "
            "The work that ran is durable — runs above survived the budget exhaustion."
        )
    except Exception as _fbe:
        print(f"[CHAT] fallback summary failed (non-fatal): {_fbe}")
        _fallback_lines.append(
            "\nThe agent loop stopped before producing a final answer. "
            "Try asking again or refining the request."
        )

    _fallback_content = "\n".join(_fallback_lines)
    # Stream as a single token event so the frontend renders it live.
    yield f"data: {json.dumps({'type': 'token', 'content': _fallback_content})}\n\n"

    # Persist into the assistant message so refresh / reopen shows it too.
    if _assistant_msg_id is not None:
        try:
            await db.update_message(
                _assistant_msg_id,
                content=_fallback_content,
                metadata={
                    "stream_started_at": _stream_started_at,
                    "in_progress": False,
                    "max_rounds_reached": True,
                    "round": MAX_ROUNDS,
                },
            )
        except Exception as _pe:
            print(f"[CHAT] fallback persist failed (non-fatal): {_pe}")

    await events.emit(conv_id, "complete", {"status": "Complete (max rounds)"})
    _done_payload = {"type": "done", "model": req.model, "message_id": _assistant_msg_id}
    if _review_round > 0:
        _done_payload["refinements"] = _review_round
    yield f"data: {json.dumps(_done_payload)}\n\n"
