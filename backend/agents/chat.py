"""
Chat streaming agent — the core multi-round tool-calling loop.
Extracted from main.py to keep the agent logic isolated.
"""
import asyncio
import json
import os
import re

import config
import database as db
import rag
from tools import CODEAGENT_TOOLS, exec_tool, parse_text_tool_calls, strip_tool_calls
from events import inject_text_tool_prompt, parse_tool_params


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
                       "list_files", "download_file", "download_project", "delete_file"}


async def chat_stream_generate(req, http, events, custom_tool_map, custom_tool_id_map):
    """Async generator that yields SSE events for a streaming chat with tool-calling.

    Args:
        req: ChatRequest pydantic model
        http: httpx.AsyncClient
        events: EventBus instance
        custom_tool_map: dict of custom tools keyed by name
        custom_tool_id_map: dict of custom tools keyed by id
    """
    conv_id = req.conversation_id
    await events.emit(conv_id, "tool_start", {"tool": "processing", "status": "🔮 Connecting to neural oracle...", "icon": "activity"})

    print(f"[CHAT] conv={conv_id} model={req.model} tool_ids={req.tool_ids} msgs={len(req.messages)} persona={req.persona_id}")

    # Resolve persona (model config) if provided — apply parameters and KB
    model_options = {}
    kb_context = ""
    persona_system_prompt = None
    persona_kb_ids = []
    if req.persona_id:
        all_configs = await db.get_model_configs()
        mc = next((c for c in all_configs if c["id"] == req.persona_id), None)
        if mc:
            persona_system_prompt = mc.get("system_prompt") or None
            params = mc.get("parameters", {})
            for key in ("temperature", "num_ctx", "top_p", "top_k"):
                if params.get(key) is not None:
                    model_options[key] = params[key]

            # Extract latest user message for RAG queries
            user_query = ""
            for m in reversed(req.messages):
                if m.get("role") == "user" and m.get("content"):
                    user_query = m["content"]
                    break

            kb_ids = mc.get("kb_ids", [])
            persona_kb_ids = kb_ids

            # ── RAG config defaults (used by both KB and research memory queries) ──
            _rag_research_top_k = 4
            _rag_research_max_chars = 3000

            # ── RAG: Query attached knowledge bases ──
            if kb_ids and user_query:
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
            if user_query:
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
    if req.num_ctx and "num_ctx" not in model_options:
        model_options["num_ctx"] = req.num_ctx
    if req.temperature is not None and "temperature" not in model_options:
        model_options["temperature"] = req.temperature
    if req.top_p is not None and "top_p" not in model_options:
        model_options["top_p"] = req.top_p
    if req.top_k is not None and "top_k" not in model_options:
        model_options["top_k"] = req.top_k
    if req.repeat_penalty is not None and "repeat_penalty" not in model_options:
        model_options["repeat_penalty"] = req.repeat_penalty

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
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.extend([{"role": m["role"], "content": m["content"]} for m in req.messages])

    # ── Build Ollama-native tool definitions ──
    available_tool_names = set()
    ollama_tools = []

    for tid in req.tool_ids:
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
        else:
            for tname, tdef in CODEAGENT_TOOLS.items():
                if tname == tid:
                    ollama_tools.append(tdef)
                    available_tool_names.add(tname)

    # Include deep_research for all codeagent sessions; conspiracy_research only when explicitly listed
    if "codeagent" in req.tool_ids:
        if "deep_research" in CODEAGENT_TOOLS and "deep_research" not in available_tool_names:
            ollama_tools.append(CODEAGENT_TOOLS["deep_research"])
            available_tool_names.add("deep_research")
    # Also include special tools if explicitly listed by name
    for tname in ("deep_research", "conspiracy_research"):
        if tname in req.tool_ids:
            if tname in CODEAGENT_TOOLS and tname not in available_tool_names:
                ollama_tools.append(CODEAGENT_TOOLS[tname])
                available_tool_names.add(tname)

    print(f"[CHAT]   Tools: {sorted(available_tool_names)}")

    # Inject tool-use system prompt when tools are available
    if available_tool_names & CODEAGENT_TOOLS_SET:
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

    MAX_ROUNDS = getattr(config, "MAX_AGENT_ROUNDS", 12)
    MAX_CONTEXT_CHARS = 50000  # ~12.5k tokens — prune old tool results beyond this
    _text_fallback_done = False
    _prev_tool_key = None  # Track previous tool call to detect loops
    _tool_history = []     # Last N tool keys for near-duplicate detection
    _dup_break_count = 0   # How many times we broke out of duplicate loops
    _last_error_sig = None  # Signature of last tool error for loop detection
    _error_repeat_count = 0  # Consecutive times we've seen the same error
    _generate_code_failed = False  # Guard: disable code-block-rescue after generate_code failure
    _generate_code_done = False    # Guard: stop tool calls after successful generate_code
    _rescue_count = 0              # How many times we rescued code blocks

    for round_num in range(MAX_ROUNDS):
        content = ""
        thinking = ""
        tool_calls = []
        gen_tokens = 0
        prompt_tokens = 0

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
            "model": req.model,
            "messages": messages,
            "stream": True,
            "options": model_options,
        }
        if ollama_tools:
            payload["tools"] = ollama_tools

        try:
            async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat",
                                   json=payload, timeout=300) as resp:
                if resp.status_code != 200:
                    error_body = (await resp.aread()).decode()[:500]
                    if "does not support tools" in error_body.lower() and not _text_fallback_done:
                        # Model doesn't support native tools — switch to text-based
                        print(f"[CHAT] Model {req.model} rejected native tools — switching to text-based")
                        ollama_tools = []
                        inject_text_tool_prompt(messages, available_tool_names)
                        _text_fallback_done = True
                        continue
                    else:
                        await events.emit(conv_id, "error", {"status": f"Ollama HTTP {resp.status_code}"})
                        yield f"data: {json.dumps({'type': 'error', 'error': error_body[:300]})}\n\n"
                        return
                else:
                    _in_thinking = False
                    _thinking_buf = ""
                    _chunk_buf = ""
                    _repeat_window = ""  # Rolling window for repetition detection
                    _live_gen_tokens = 0  # Live token counter for streaming updates
                    # Buffer mode: when tools are active, don't stream content immediately
                    # so code blocks don't flash in chat before tool calls are detected
                    _has_tools = bool(available_tool_names)

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue

                        # Detect Ollama error in stream (e.g. CUDA OOM)
                        if chunk.get("error"):
                            _ollama_err = chunk["error"][:300]
                            print(f"[CHAT]   Ollama stream error: {_ollama_err}")
                            # Surface OOM errors clearly
                            if "out of memory" in _ollama_err.lower():
                                _oom_hint = f"GPU out of memory with num_ctx={model_options.get('num_ctx', 'default')}. Try a smaller context size or smaller model."
                                await events.emit(conv_id, "error", {"status": f"GPU OOM: {_oom_hint}"})
                                yield f"data: {json.dumps({'type': 'error', 'error': _oom_hint})}\n\n"
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
                                await events.emit(conv_id, "thinking", {"status": f"💭 {snip}..."})
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
                                        await events.emit(conv_id, "thinking", {"status": f"💭 {snip}..."})
                                else:
                                    _thinking_buf += token
                                    if len(_thinking_buf) % 100 < len(token):
                                        snip = _thinking_buf[-60:].replace("\n", " ")
                                        await events.emit(conv_id, "thinking", {"status": f"💭 {snip}..."})
                                    continue

                            if token:
                                content += token
                                _live_gen_tokens += 1

                                # Emit live token count every 10 tokens
                                if _live_gen_tokens % 10 == 0:
                                    yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': _live_gen_tokens, 'prompt_tokens': prompt_tokens, 'live': True})}\n\n"

                                # Repetition detection: check last 200 chars for short repeating patterns
                                _repeat_window = (_repeat_window + token)[-200:]
                                if len(_repeat_window) >= 120:
                                    for plen in range(2, 25):
                                        pat = _repeat_window[-plen:]
                                        # Skip whitespace-only patterns (common in ASCII art, tables, formatted output)
                                        if not pat.strip():
                                            continue
                                        count = _repeat_window.count(pat)
                                        if count >= 8 and count * plen > len(_repeat_window) * 0.5:
                                            print(f"[CHAT]   Repetition detected: {pat!r} x{count} — stopping generation")
                                            content = content[:content.rfind(pat)]
                                            break
                                    else:
                                        pat = None
                                    if pat:
                                        break

                                if _has_tools:
                                    # Buffer mode: emit a progress pill instead of streaming tokens
                                    # Show what the model is working on via SSE event
                                    if len(content) % 200 < len(token):
                                        await events.emit(conv_id, "tool_start", {
                                            "tool": "generating",
                                            "status": f"✍️ Generating... ({len(content)} chars)",
                                            "icon": "edit",
                                        })
                                    await asyncio.sleep(0)
                                else:
                                    # No tools — stream content directly to chat
                                    _chunk_buf += token
                                    if len(_chunk_buf) >= 8 or chunk.get("done"):
                                        yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
                                        _chunk_buf = ""
                                        await asyncio.sleep(0)

                        # Track token counts from Ollama
                        if chunk.get("done"):
                            if _chunk_buf:
                                yield f"data: {json.dumps({'type': 'token', 'content': _chunk_buf})}\n\n"
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

        except Exception as e:
            err_msg = str(e) or "Connection failed or timeout"
            await events.emit(conv_id, "error", {"status": f"Ollama: {err_msg[:120]}"})
            yield f"data: {json.dumps({'type': 'error', 'error': err_msg})}\n\n"
            return

        # Build the full message object for conversation history
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls

        # ── Text-based tool call fallback ──
        if not tool_calls and content and available_tool_names:
            tool_calls = parse_text_tool_calls(content, available_tool_names)
            if tool_calls:
                content = strip_tool_calls(content)
                # Clean up residual garbage (backticks, braces, etc.) left after stripping
                cleaned_residue = re.sub(r'[`{}\[\]\s"\']', '', content).strip()
                if len(cleaned_residue) < 10:
                    content = ""
                msg["content"] = content
                for tc in tool_calls:
                    print(f"[CHAT]   text-parsed tool call: {tc['function']['name']}")

        # ── Code block rescue: when model dumps code in chat instead of using tools ──
        # Skip rescue if model was just told to stop looping, or if generate_code already failed
        # (prevents infinite loop: generate_code fails -> model dumps code -> rescue -> execute -> fail -> repeat)
        if not tool_calls and content and (available_tool_names & CODEAGENT_TOOLS_SET) and _rescue_count < 1 and not _generate_code_failed:
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
                # Second rescue attempt → inject stern message instead
                if not tool_calls and _rescue_count >= 1:
                    messages.append({"role": "tool", "content": "SYSTEM: STOP writing code in chat text. You MUST use tools. Call write_file or execute_code for ALL code."})
                    content = ""
                    msg["content"] = ""

        print(f"[CHAT] Round {round_num}: content={len(content)} thinking={len(thinking)} tool_calls={len(tool_calls)} gen_tokens={gen_tokens} prompt_tokens={prompt_tokens}")
        if thinking:
            print(f"[CHAT]   thinking: {thinking[:200]!r}")
        if content:
            print(f"[CHAT]   content: {content[:200]!r}")
        if tool_calls:
            print(f"[CHAT]   tool_calls: {json.dumps(tool_calls)[:300]}")

        # Emit final thinking content
        if thinking:
            await events.emit(conv_id, "thought_done", {
                "status": thinking[-80:].replace("\n", " ") + ("..." if len(thinking) > 80 else ""),
                "detail": json.dumps({"thinking": thinking}),
            })

        if tool_calls:
            # ── Guard: after successful generate_code, block further tool calls ──
            if _generate_code_done:
                _tc_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
                # Only allow download_project after generate_code success
                if not any(n == "download_project" for n in _tc_names):
                    print(f"[CHAT]   Blocking tool calls after generate_code success: {_tc_names}")
                    tool_calls = []
                    # Fall through to "no tool calls" path below

            # ── Duplicate / near-duplicate detection ──
            if tool_calls:
                _tool_key = json.dumps([(tc.get("function", {}).get("name"), json.dumps(tc.get("function", {}).get("arguments", {}), sort_keys=True)) for tc in tool_calls], sort_keys=True)
                _tc_names_dup = [tc.get("function", {}).get("name", "") for tc in tool_calls]

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
                    else:
                        _is_dup = True
                        print(f"[CHAT]   Near-duplicate detected (seen in last 3 rounds)")

                if _is_dup:
                    _dup_break_count += 1
                    print(f"[CHAT]   Duplicate tool call detected (#{_dup_break_count}) — breaking loop")
                    if _dup_break_count >= 2:
                        messages.append({"role": "tool", "content": "STOP. You are stuck in a loop. Summarize what you accomplished and respond to the user NOW. Do not call any more tools."})
                    else:
                        messages.append({"role": "tool", "content": "You already called this tool with the same arguments. Do NOT repeat the same call. Provide your final response to the user now."})
                    continue

                _prev_tool_key = _tool_key
                _tool_history.append(_tool_key)
                if len(_tool_history) > 5:
                    _tool_history.pop(0)

            if content:
                if _has_tools:
                    # Content was buffered (not streamed) — strip code, keep prose for history
                    cleaned = re.sub(r'```\w*\n.*?```', '', content, flags=re.DOTALL).strip()
                    msg["content"] = cleaned
                else:
                    # Content was streamed — tell frontend to discard it
                    yield f"data: {json.dumps({'type': 'clear'})}\n\n"
                    cleaned = re.sub(r'```\w*\n.*?```', '', content, flags=re.DOTALL).strip()
                    msg["content"] = cleaned

            messages.append(msg)
            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except (json.JSONDecodeError, ValueError):
                        print(f"[CHAT] Warning: failed to parse tool args JSON for {tool_name}: {tool_args[:200]!r}")
                        tool_args = {}

                print(f"[CHAT]   Executing tool: {tool_name}({json.dumps(tool_args)[:200]})")

                # Tool-specific icons and status labels for progress pills
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
                    "deep_research": ("microscope", "🔬 Deep research in progress"),
                    "conspiracy_research": ("eye", "🕵️ Investigating"),
                }
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
                _tf = asyncio.get_event_loop().create_future()
                _tool_chars = [0]  # mutable counter for live progress
                async def _run_tool_bg(_n=tool_name, _a=tool_args, _c=conv_id, _f=_tf, _tc=_tool_chars, _kb=persona_kb_ids):
                    try:
                        r = await exec_tool(http, events, _n, _a, _c, custom_tool_map, conv_model=req.model, kb_ids=_kb)
                        _tc[0] = len(r) if r else 0
                        if not _f.done(): _f.set_result(r)
                    except Exception as _e:
                        if not _f.done(): _f.set_exception(_e)

                asyncio.create_task(_run_tool_bg())

                _base_ctx = sum(len(m.get("content", "")) for m in messages) // 4
                _tool_start_time = asyncio.get_event_loop().time()
                while not _tf.done():
                    await asyncio.sleep(2)
                    if not _tf.done():
                        _elapsed = asyncio.get_event_loop().time() - _tool_start_time
                        _elapsed_str = f"{int(_elapsed)}s"
                        await events.emit(conv_id, "tool_start", {
                            "tool": tool_name, "icon": _tool_icon,
                            "status": f"{_tool_label}{_tool_detail} ({_elapsed_str})",
                        })
                        # Estimate growing context: base + tool chars so far + time-based estimate
                        # Research tools accumulate ~200 chars/sec on average
                        _est_tool_tokens = max(_tool_chars[0] // 4, int(_elapsed * 50))
                        _est_ctx = _base_ctx + _est_tool_tokens
                        yield f"data: {json.dumps({'type': 'ctx_update', 'gen_tokens': 0, 'prompt_tokens': _est_ctx, 'live': True})}\n\n"
                        yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

                try:
                    tool_result = _tf.result()
                except Exception as te:
                    tool_result = f"**Tool error ({tool_name}):** {str(te)}"

                # Truncate huge results
                MAX_TOOL_RESULT = 12000
                if len(tool_result) > MAX_TOOL_RESULT:
                    orig_len = len(tool_result)
                    tool_result = tool_result[:MAX_TOOL_RESULT] + f"\n\n[TRUNCATED — result was {orig_len} chars]"

                messages.append({"role": "tool", "content": tool_result})
                print(f"[CHAT]   Tool result ({tool_name}): {len(tool_result)} chars")

                # Track generate_code failures to prevent code-block-rescue loops
                if tool_name == "generate_code" and tool_result.startswith("ERROR"):
                    _generate_code_failed = True
                    print("[CHAT]   generate_code failed — disabling code-block-rescue for this session")

                # When generate_code succeeds (PROJECT COMPLETE), force the model to respond
                # Don't let it waste rounds inspecting files that OpenHands already tested
                if tool_name == "generate_code" and "PROJECT COMPLETE" in tool_result:
                    _generate_code_done = True
                    print("[CHAT]   generate_code succeeded — forcing final response (no more tool calls)")
                    messages.append({"role": "tool", "content": (
                        "SYSTEM: The project is COMPLETE and TESTED by OpenHands. "
                        "Do NOT call list_files, read_file, or any other tool. "
                        "Respond to the user NOW with the project summary and download link."
                    )})
                    break  # Exit the tool_calls loop, go to next round for final response

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
                if req.persona_id and tool_name in rag.RESEARCH_TOOLS and len(tool_result) > 100:
                    try:
                        _query_for_index = ""
                        if isinstance(tool_args, dict):
                            _query_for_index = tool_args.get("query", "") or tool_args.get("url", "") or tool_args.get("topic", "")
                        asyncio.create_task(
                            rag.index_research(req.persona_id, tool_name, _query_for_index, tool_result, conv_id)
                        )
                    except Exception as _rag_e:
                        print(f"[RAG] Auto-index error: {_rag_e}")

            continue

        # No tool calls — we have a final response
        if content:
            # If content was buffered (tool mode), flush it now as the final answer
            if _has_tools:
                for i in range(0, len(content), 8):
                    yield f"data: {json.dumps({'type': 'token', 'content': content[i:i+8]})}\n\n"
                    await asyncio.sleep(0)
            messages.append(msg)
            await events.emit(conv_id, "complete", {"status": "Complete"})
            yield f"data: {json.dumps({'type': 'done', 'model': req.model})}\n\n"
            return
        else:
            # Empty content — try to recover
            if round_num >= 3:
                await events.emit(conv_id, "complete", {"status": "Complete"})
                yield f"data: {json.dumps({'type': 'done', 'model': req.model})}\n\n"
                return
            print(f"[CHAT]   Empty content (round {round_num}), gen_tokens={gen_tokens}, thinking={len(thinking)}")

            # Model over-thought: produced thinking but no content
            if thinking and not content:
                print(f"[CHAT]   Over-thought ({len(thinking)} chars thinking, 0 content) — nudging")
                messages.append({"role": "assistant", "content": ""})
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
            if available_tool_names & CODEAGENT_TOOLS_SET:
                messages.append({"role": "user", "content": "Use your tools to accomplish the task. Call execute_code, write_file, or run_shell now."})
            else:
                messages.append({"role": "user", "content": "Please provide a response."})
            continue

    await events.emit(conv_id, "complete", {"status": "Complete (max rounds)"})
    yield f"data: {json.dumps({'type': 'done', 'model': req.model})}\n\n"
