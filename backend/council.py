"""
Council chat — multi-model parallel streaming with AI peer voting and debate rounds.
"""
import asyncio
import json
import re
import urllib.parse

import config
import database as db


def _is_gibberish(text: str, threshold: float = 0.3) -> bool:
    """Detect incoherent model output by checking the ratio of real English words."""
    if not text or len(text) < 50:
        return False
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())
    if len(words) < 10:
        return False
    # Common English words — if fewer than threshold are recognizable, it's gibberish
    common = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her",
        "was", "one", "our", "out", "has", "his", "how", "its", "may", "new", "now",
        "old", "see", "way", "who", "did", "get", "let", "say", "she", "too", "use",
        "this", "that", "with", "have", "from", "they", "been", "said", "each", "which",
        "their", "will", "other", "about", "many", "then", "them", "some", "could",
        "would", "make", "like", "time", "just", "know", "take", "people", "into",
        "year", "your", "good", "very", "when", "what", "there", "also", "after",
        "should", "think", "because", "these", "than", "first", "must", "being",
        "through", "most", "where", "much", "before", "between", "does", "however",
        "while", "such", "even", "though", "well", "still", "risk", "data", "based",
        "model", "system", "company", "group", "specific", "hiring", "tool",
    }
    recognized = sum(1 for w in words if w in common)
    ratio = recognized / len(words)
    return ratio < threshold


async def stream_council_chat(http, events, council, req_messages, conv_id, quick_search: bool = False, kb_ids: list = None):
    """Async generator that streams council member responses, voting, and host synthesis."""
    members = council.get("members", [])
    host_model = council.get("host_model", config.DEFAULT_MODEL)
    host_sys = council.get("host_system_prompt", "")
    debate_rounds = council.get("debate_rounds", 0) or 0
    messages = req_messages

    # ── Validate all models exist in Ollama ──
    try:
        _tags_r = await http.get(f"{config.OLLAMA_URL}/api/tags", timeout=10)
        if _tags_r.status_code == 200:
            _available = {m["name"] for m in _tags_r.json().get("models", [])}
            _fallback = next(iter(_available), config.DEFAULT_MODEL)
            # Fix host model if deleted
            if host_model not in _available:
                print(f"[COUNCIL] Host model '{host_model}' not found, falling back to '{_fallback}'")
                host_model = _fallback
            # Fix member models if deleted
            for member in members:
                if member.get("model") and member["model"] not in _available:
                    print(f"[COUNCIL] Member model '{member['model']}' not found, falling back to '{_fallback}'")
                    member["model"] = _fallback
    except Exception as _e:
        print(f"[COUNCIL] Could not validate models: {_e}")

    # ── RAG: Query attached knowledge bases for council context ──
    kb_context = ""
    if kb_ids:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if last_user:
            try:
                import rag
                _rag_cfg = config.DEFAULT_SETTINGS.get("rag", {})
                try:
                    import json as _json
                    with open(config.SETTINGS_PATH, "r") as _sf:
                        _rag_cfg = {**_rag_cfg, **_json.load(_sf).get("rag", {})}
                except Exception:
                    pass
                _top_k = int(_rag_cfg.get("top_k", 6))
                _max_chars = int(_rag_cfg.get("max_context_chars", 6000))

                chunks = await rag.query(kb_ids, last_user, top_k=_top_k)
                if chunks:
                    kb_context = rag.format_context(chunks, max_chars=_max_chars)
                    filenames = list(set(c["filename"] for c in chunks))
                    avg_score = sum(c["score"] for c in chunks) / len(chunks)
                    yield f"data: {json.dumps({'type': 'council_kb', 'status': f'Retrieved {len(chunks)} KB chunks from {', '.join(filenames[:3])} ({avg_score:.0%} relevance)'})}\n\n"
                    print(f"[COUNCIL RAG] Retrieved {len(chunks)} chunks (avg {avg_score:.2f}) for: {last_user[:80]!r}")
            except Exception as e:
                print(f"[COUNCIL RAG] KB query failed: {e}")

    # Quick search augmentation
    search_context = ""
    if quick_search and messages:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if last_user:
            try:
                params = urllib.parse.urlencode({"q": last_user[:200], "format": "json", "language": "en"})
                sr = await http.get(f"{config.SEARXNG_URL}/search?{params}", timeout=8)
                sdata = sr.json()
                snippets = []
                for item in sdata.get("results", [])[:4]:
                    title = item.get("title", "")
                    snippet = item.get("content", "")[:200]
                    url = item.get("url", "")
                    if title or snippet:
                        snippets.append(f"- {title}: {snippet} ({url})")
                if snippets:
                    search_context = "\n\n[Current web context:\n" + "\n".join(snippets) + "\n]"
            except Exception:
                pass

    member_responses = {}  # mid -> latest response
    all_round_responses = {}  # mid -> [round0, round1, ...]
    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    if last_user_msg and conv_id:
        await db.add_message(conv_id, "user", last_user_msg)

    # ── Helper: stream one round of member responses ──
    async def run_round(round_num: int, extra_context_fn=None):
        """Query all members in parallel for a given round. extra_context_fn(member) returns additional context."""
        output_q: asyncio.Queue = asyncio.Queue()
        round_responses = {}

        async def query_member(member: dict):
            mid = member["id"]
            model = member["model"]
            sys_p = member.get("system_prompt", "")
            if kb_context:
                kb_section = (
                    "\n\n=== RELEVANT KNOWLEDGE BASE CONTEXT ===\n"
                    "The following excerpts were retrieved from attached knowledge bases. "
                    "Use them to inform your response.\n\n" + kb_context
                )
                sys_p = (sys_p + kb_section) if sys_p else kb_section
            if search_context:
                sys_p = (sys_p + search_context) if sys_p else search_context
            if sys_p:
                sys_p += " Always respond in English."
            else:
                sys_p = "Always respond in English."

            msgs = [{"role": m["role"], "content": m["content"]} for m in messages]

            # Add debate context from previous rounds
            if extra_context_fn:
                extra = extra_context_fn(member)
                if extra:
                    msgs.append({"role": "user", "content": extra})

            payload = {
                "model": model,
                "messages": [{"role": "system", "content": sys_p}] + msgs,
                "stream": True,
                "options": {"num_ctx": 16384},
                "keep_alive": "30m",
            }
            full = ""
            max_attempts = 2
            for attempt in range(max_attempts):
                full = ""
                try:
                    async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat",
                                           json=payload, timeout=180) as resp:
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                            except Exception:
                                continue
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                full += content
                                await output_q.put({"type": "council_token",
                                                    "member_id": mid, "model": model,
                                                    "content": content, "round": round_num})
                            if chunk.get("done"):
                                break
                except Exception as e:
                    print(f"[COUNCIL] Member {member.get('persona_name', model)} error (attempt {attempt+1}): {e}")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(2)
                        continue
                    await output_q.put({"type": "council_token", "member_id": mid,
                                        "model": model, "content": f"\n[Error: {e}]", "round": round_num})
                # Got a non-empty response, break retry loop
                if full.strip():
                    break
                elif attempt < max_attempts - 1:
                    print(f"[COUNCIL] Member {member.get('persona_name', model)} empty response, retrying...")
                    await asyncio.sleep(2)
            round_responses[mid] = full
            await output_q.put({"type": "council_done", "member_id": mid, "model": model, "round": round_num})

        tasks = [asyncio.create_task(query_member(m)) for m in members]

        done_count = 0
        total = len(members)
        while done_count < total or not output_q.empty():
            try:
                item = await asyncio.wait_for(output_q.get(), timeout=0.05)
                if item["type"] == "council_done":
                    done_count += 1
                yield item
            except asyncio.TimeoutError:
                if done_count >= total:
                    break

        await asyncio.gather(*tasks, return_exceptions=True)

        # Update tracking — only overwrite if the member actually responded
        for mid, content in round_responses.items():
            if content.strip():
                member_responses[mid] = content
            else:
                print(f"[COUNCIL] Member {mid} produced empty response in round {round_num}, keeping previous")
            if mid not in all_round_responses:
                all_round_responses[mid] = []
            all_round_responses[mid].append(content if content.strip() else member_responses.get(mid, ""))

    # ── Round 0: Initial responses ──
    yield f"data: {json.dumps({'type': 'council_round', 'round': 0, 'total_rounds': debate_rounds + 1, 'label': 'Opening Statements'})}\n\n"

    async for item in run_round(0):
        yield f"data: {json.dumps(item)}\n\n"

    # Persist round 0 responses
    for member in members:
        mid = member["id"]
        content = member_responses.get(mid, "")
        if content:
            await db.add_message(conv_id, "assistant", content,
                                 metadata={"council_member_id": mid,
                                           "council_model": member["model"],
                                           "council_persona": member.get("persona_name", ""),
                                           "debate_round": 0})

    # ── Debate rounds ──
    for rnd in range(1, debate_rounds + 1):
        round_label = f"Rebuttal Round {rnd}" if debate_rounds > 1 else "Rebuttal Round"
        yield f"data: {json.dumps({'type': 'council_round', 'round': rnd, 'total_rounds': debate_rounds + 1, 'label': round_label})}\n\n"

        def make_debate_context(member):
            mid = member["id"]
            member_name = member.get("persona_name") or member["model"].split(":")[0]
            others_text = []
            for m in members:
                if m["id"] == mid:
                    continue
                name = m.get("persona_name") or m["model"].split(":")[0]
                prev = member_responses.get(m["id"], "")
                if prev and not _is_gibberish(prev):
                    others_text.append(f'[{name}]: {prev[:800]}')
            if not others_text:
                return None
            your_prev = member_responses.get(mid, "")[:500]
            return (
                f"This is debate round {rnd}. The other council members have responded.\n\n"
                f"Their responses:\n" + "\n\n".join(others_text) + "\n\n"
                f"Your previous response was:\n{your_prev}\n\n"
                f"Now respond to the other members' arguments. Challenge weak points, "
                f"defend your position, acknowledge good arguments, and refine your answer. "
                f"Be direct and engage specifically with what others said. Keep it concise."
            )

        async for item in run_round(rnd, extra_context_fn=make_debate_context):
            yield f"data: {json.dumps(item)}\n\n"

        # Persist debate round responses
        for member in members:
            mid = member["id"]
            content = member_responses.get(mid, "")
            if content:
                await db.add_message(conv_id, "assistant", content,
                                     metadata={"council_member_id": mid,
                                               "council_model": member["model"],
                                               "council_persona": member.get("persona_name", ""),
                                               "debate_round": rnd})

    # ── AI Peer Voting Phase ──
    vote_details = []
    vote_tally = {}
    updated_points = {}

    responding_members = [m for m in members if member_responses.get(m["id"]) and not _is_gibberish(member_responses[m["id"]])]
    if len(responding_members) > 1:
        yield f"data: {json.dumps({'type': 'council_voting'})}\n\n"

        async def query_member_vote(member: dict):
            mid = member["id"]
            member_name = member.get("persona_name") or member["model"].split(":")[0]
            others = [
                (m, member_responses[m["id"]])
                for m in responding_members
                if m["id"] != mid
            ]
            if not others:
                return None
            options_text = "\n\n".join(
                f'"{m.get("persona_name") or m["model"].split(":")[0]}":\n{content[:600]}'
                for m, content in others
            )
            round_info = f" after {debate_rounds + 1} rounds of debate" if debate_rounds > 0 else ""
            vote_prompt = (
                f'The council was asked: "{last_user_msg[:300]}"\n\n'
                f'Your final response{round_info}: "{member_responses.get(mid, "")[:300]}"\n\n'
                f'Now vote for the BEST response from the other council members. '
                f'You CANNOT vote for yourself.\n\n'
                f'Other final responses:\n{options_text}\n\n'
                f'Reply in EXACTLY this format (nothing else):\n'
                f'VOTE: [exact name from above]\n'
                f'REASON: [one sentence explaining your choice]'
            )
            try:
                r = await http.post(f"{config.OLLAMA_URL}/api/chat", json={
                    "model": member["model"],
                    "messages": [{"role": "user", "content": vote_prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 120},
                    "keep_alive": "30m",
                }, timeout=30)
                text = r.json()["message"]["content"].strip()
                vote_m = re.search(r'VOTE:\s*["\']?([^"\'\n\r]+)["\']?', text, re.IGNORECASE)
                reason_m = re.search(r'REASON:\s*(.+)', text, re.IGNORECASE | re.DOTALL)
                voted_name = vote_m.group(1).strip() if vote_m else ""
                reason = reason_m.group(1).strip()[:200] if reason_m else text[:150]
                voted_id = None
                best = 0
                for m, _ in others:
                    name = (m.get("persona_name") or m["model"].split(":")[0]).lower()
                    vn = voted_name.lower()
                    score = 2 if name == vn else (1 if name in vn or vn in name else 0)
                    if score > best:
                        best = score
                        voted_id = m["id"]
                if not voted_id:
                    voted_id = others[0][0]["id"]
                voted_m = next(m for m, _ in others if m["id"] == voted_id)
                return {
                    "voter_id": mid,
                    "voter_name": member_name,
                    "voted_for": voted_id,
                    "voted_for_name": voted_m.get("persona_name") or voted_m["model"].split(":")[0],
                    "reason": reason,
                }
            except Exception as e:
                print(f"[COUNCIL] Vote error for {member_name}: {e}")
                return None

        vote_tasks = [asyncio.create_task(query_member_vote(m)) for m in responding_members]
        raw_votes = await asyncio.gather(*vote_tasks, return_exceptions=True)

        for vote in raw_votes:
            if not vote or isinstance(vote, Exception):
                continue
            vote_details.append(vote)
            vid = vote["voted_for"]
            vote_tally[vid] = vote_tally.get(vid, 0) + 1
            yield f"data: {json.dumps({'type': 'council_vote', **vote})}\n\n"

        for mid, count in vote_tally.items():
            try:
                member = next(m for m in members if m["id"] == mid)
                new_pts = (member.get("points") or 0) + count
                await db.update_council_member(mid, points=new_pts)
                updated_points[mid] = new_pts
            except Exception as e:
                print(f"[COUNCIL] Points update error: {e}")

        yield f"data: {json.dumps({'type': 'council_votes', 'votes': vote_details, 'tally': vote_tally, 'updated_points': updated_points})}\n\n"

    # Host synthesis
    if host_model and member_responses:
        # Include debate history if there were rounds
        if debate_rounds > 0:
            rounds_text = []
            for rnd_idx in range(debate_rounds + 1):
                rnd_label = "Opening Statements" if rnd_idx == 0 else f"Rebuttal Round {rnd_idx}"
                round_entries = []
                for member in members:
                    mid = member["id"]
                    rounds = all_round_responses.get(mid, [])
                    if rnd_idx < len(rounds) and rounds[rnd_idx] and not _is_gibberish(rounds[rnd_idx]):
                        name = member.get("persona_name") or member["model"]
                        round_entries.append(f"[{name}]: {rounds[rnd_idx][:600]}")
                if round_entries:
                    rounds_text.append(f"── {rnd_label} ──\n" + "\n\n".join(round_entries))
            all_resp = "\n\n".join(rounds_text)
        else:
            all_resp = "\n\n".join(
                f"[{member.get('persona_name') or member['model']}]: {member_responses.get(member['id'], '')}"
                for member in members if member_responses.get(member["id"]) and not _is_gibberish(member_responses[member["id"]])
            )
        vote_summary = ""
        if vote_details:
            vote_lines = [
                f"- {v['voter_name']} voted for {v['voted_for_name']}: \"{v['reason']}\""
                for v in vote_details
            ]
            vote_summary = "\n\nPeer vote results:\n" + "\n".join(vote_lines)
        debate_note = f" The council debated for {debate_rounds + 1} rounds." if debate_rounds > 0 else ""
        host_system = (host_sys or "You are the council moderator. Synthesize the council responses and provide a final verdict or summary.") + " Always respond in English."
        if kb_context:
            host_system += (
                "\n\n=== RELEVANT KNOWLEDGE BASE CONTEXT ===\n"
                "The following excerpts were retrieved from attached knowledge bases. "
                "Use them to ground your synthesis.\n\n" + kb_context
            )
        host_msgs = [
            {"role": "system", "content": host_system},
            {"role": "user", "content": f"Question: {last_user_msg}\n\n{debate_note}Council responses:\n{all_resp}{vote_summary}\n\nProvide a synthesis and final verdict in English. Reference the peer votes and how positions evolved during the debate if relevant."}
        ]
        payload = {"model": host_model, "messages": host_msgs, "stream": True, "options": {"num_ctx": 16384}, "keep_alive": "30m"}
        host_full = ""
        try:
            async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat",
                                   json=payload, timeout=180) as resp:
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        host_full += content
                        yield f"data: {json.dumps({'type': 'council_host_token', 'content': content})}\n\n"
                    if chunk.get("done"):
                        break
        except Exception as e:
            yield f"data: {json.dumps({'type': 'council_host_token', 'content': f'[Host error: {e}]'})}\n\n"
        if host_full:
            council_id = council.get("id", "")
            await db.add_message(conv_id, "assistant", host_full,
                                 metadata={"council_host": True, "council_id": council_id,
                                           "votes": vote_details, "tally": vote_tally,
                                           "debate_rounds": debate_rounds})

    yield f"data: {json.dumps({'type': 'council_complete'})}\n\n"
