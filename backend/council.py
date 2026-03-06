"""
Council chat — multi-model parallel streaming with AI peer voting.
"""
import asyncio
import json
import re
import urllib.parse

import config
import database as db


async def stream_council_chat(http, events, council, req_messages, conv_id, quick_search: bool = False):
    """Async generator that streams council member responses, voting, and host synthesis."""
    members = council.get("members", [])
    host_model = council.get("host_model", config.DEFAULT_MODEL)
    host_sys = council.get("host_system_prompt", "")
    messages = req_messages

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

    member_responses = {}
    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    if last_user_msg and conv_id:
        await db.add_message(conv_id, "user", last_user_msg)

    output_q: asyncio.Queue = asyncio.Queue()

    async def query_member(member: dict):
        mid = member["id"]
        model = member["model"]
        sys_p = member.get("system_prompt", "")
        if search_context:
            sys_p = (sys_p + search_context) if sys_p else search_context

        msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
        payload = {
            "model": model,
            "messages": ([{"role": "system", "content": sys_p}] if sys_p else []) + msgs,
            "stream": True,
            "options": {}
        }
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
                                            "content": content})
                    if chunk.get("done"):
                        break
        except Exception as e:
            await output_q.put({"type": "council_token", "member_id": mid,
                                "model": model, "content": f"\n[Error: {e}]"})
        member_responses[mid] = full
        await output_q.put({"type": "council_done", "member_id": mid, "model": model})

    # Launch all member tasks
    tasks = [asyncio.create_task(query_member(m)) for m in members]

    # Yield tokens as they arrive from output_q
    done_count = 0
    total = len(members)
    while done_count < total or not output_q.empty():
        try:
            item = await asyncio.wait_for(output_q.get(), timeout=0.05)
            if item["type"] == "council_done":
                done_count += 1
            yield f"data: {json.dumps(item)}\n\n"
        except asyncio.TimeoutError:
            if done_count >= total:
                break

    await asyncio.gather(*tasks, return_exceptions=True)

    # Persist member responses to DB
    for member in members:
        mid = member["id"]
        content = member_responses.get(mid, "")
        if content:
            await db.add_message(conv_id, "assistant", content,
                                 metadata={"council_member_id": mid,
                                           "council_model": member["model"],
                                           "council_persona": member.get("persona_name", "")})

    # ── AI Peer Voting Phase ──
    vote_details = []
    vote_tally = {}
    updated_points = {}

    responding_members = [m for m in members if member_responses.get(m["id"])]
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
            vote_prompt = (
                f'The council was asked: "{last_user_msg[:300]}"\n\n'
                f'Your response: "{member_responses.get(mid, "")[:300]}"\n\n'
                f'Now vote for the BEST response from the other council members. '
                f'You CANNOT vote for yourself.\n\n'
                f'Other responses:\n{options_text}\n\n'
                f'Reply in EXACTLY this format (nothing else):\n'
                f'VOTE: [exact name from above]\n'
                f'REASON: [one sentence explaining your choice]'
            )
            try:
                r = await http.post(f"{config.OLLAMA_URL}/api/chat", json={
                    "model": member["model"],
                    "messages": [{"role": "user", "content": vote_prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 120}
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
        all_resp = "\n\n".join(
            f"[{member.get('persona_name') or member['model']}]: {member_responses.get(member['id'], '')}"
            for member in members if member_responses.get(member["id"])
        )
        vote_summary = ""
        if vote_details:
            vote_lines = [
                f"- {v['voter_name']} voted for {v['voted_for_name']}: \"{v['reason']}\""
                for v in vote_details
            ]
            vote_summary = "\n\nPeer vote results:\n" + "\n".join(vote_lines)
        host_msgs = [
            {"role": "system", "content": host_sys or "You are the council moderator. Synthesize the council responses and provide a final verdict or summary."},
            {"role": "user", "content": f"Question: {last_user_msg}\n\nCouncil responses:\n{all_resp}{vote_summary}\n\nProvide a synthesis and final verdict. Reference the peer votes if relevant."}
        ]
        payload = {"model": host_model, "messages": host_msgs, "stream": True, "options": {}}
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
                                           "votes": vote_details, "tally": vote_tally})

    yield f"data: {json.dumps({'type': 'council_complete'})}\n\n"
