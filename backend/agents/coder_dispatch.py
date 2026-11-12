"""Application-side dispatch receipts; this module does not belong on the worker."""
import re

import database as db
from db import coder_jobs as store


_CLAIM = re.compile(
    r"\b(?:underway|in progress|running in (?:the )?background)\b"
    r"|\b(?:I(?:['’]ve| have)?|we(?:['’]ve| have)?|(?:the|your) (?:job|workflow|build|edit|update))"
    r"\s+(?:is |has |have )?(?:started|launched|queued|dispatched|begun)\b"
    r"|\b(?:I['’]m|I am)\s+(?:starting|building|implementing|updating)\b"
    r"|\b(?:I['’]ve|I have)\s+(?:added|created|built|implemented|updated|fixed|finished|completed)\b"
    r"|\b(?:Daedalus job|Project question)\s+\S+\s+is saved with status\b",
    re.I,
)
_READ_ONLY = re.compile(
    r"\b(?:don't|do not|not yet|plan only|only (?:a )?plan|just (?:a )?plan)\b"
    r"|\b(?:create|write|make|provide)\s+(?:me\s+)?(?:a|an|the)\s+"
    r"(?:(?:detailed|implementation|repair|test)\s+)*plan\s+(?:for|to|of)\b"
    r"|^(?:(?:please|can you|could you)\s+)*(?:what|why|how|where|which|is|are|did|has|"
    r"before|wait|stop|cancel|status|explain|describe|summarize|translate|quote|plan)\b", re.I,
)
_CHANGE = re.compile(
    r"^(?:(?:please|okay|ok)[, ]+)*(?:(?:can|could|would|will) you (?:please )?)?"
    r"(?:add|build|create|fix|implement|update|change|remove|make|repair|refactor|write|develop)\b", re.I,
)
_JOB_ID = re.compile(r'\bcw3-[0-9a-f]{24}\b')
_STATUS = re.compile(r'\b(?:status|progress|finished|completed|running|queued|blocked|failed)\b|\bhow.+(?:going|doing)\b', re.I)


def claims_start(content):
    text = re.sub(r'```.*?```|`[^`\n]*\s[^`\n]*`|"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]+\x27', '', content, flags=re.S)
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
    return any(_CLAIM.search(sentence) and not re.search(
        r"\b(?:no|not|never|hasn't|haven't|isn't|didn't|couldn't)\b", sentence, re.I)
        for sentence in re.split(r'[.!?\n]', text))


def may_correct(request):
    # This only permits a re-prompt; it never constructs or dispatches an action.
    # Ambiguous, quoted and read-only requests fail closed during correction.
    text = re.sub(r'```.*?```', '', request, flags=re.S)
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>')).strip()
    return bool(_CHANGE.search(text) and not _READ_ONLY.search(text))


class DispatchGuard:
    def __init__(self, conversation_id, message_id, request, *, project_id='', new_project=False):
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.request = request
        self.project_id = project_id
        self.new_project = new_project

    async def receipt(self):
        if not self.message_id:
            return None
        job = await store.get(store.request_identity(self.conversation_id, str(self.message_id)))
        if not job:
            return None
        if (job.get('engine') != 'openhands159'
                or job.get('conversation_id') != self.conversation_id
                or str(job.get('chat_request_key')) != str(self.message_id)
                or job.get('original_user_request') != self.request):
            raise ValueError('The saved workflow does not match this user message; no additional job was dispatched.')
        if self.new_project and job['mode'] not in {'build_from_prompt', 'ask_uploaded_project'}:
            raise ValueError('The saved workflow mode differs from this request; no additional job was dispatched.')
        if self.project_id and not self.new_project and job['project_id'] != self.project_id:
            raise ValueError('The saved workflow belongs to another project; no additional job was dispatched.')
        if self.project_id and not self.new_project and job['mode'] not in {'edit_project', 'ask_uploaded_project'}:
            raise ValueError('The saved workflow mode differs from this selected project; no additional job was dispatched.')
        return job

    @staticmethod
    def describe(job):
        kind = 'Project question' if job['mode'] == 'ask_uploaded_project' else 'Daedalus job'
        return (f"{kind} `{job['id']}` is saved with status **{job['state']}**. "
                'The progress card shows its current state. Completion and review are separate from starting the job.')

    async def review(self, content, *, can_retry):
        if not claims_start(content):
            return content, False
        job = await self.receipt()
        if job:
            return self.describe(job), False
        if not may_correct(self.request) and _STATUS.search(self.request):
            requested = set(_JOB_ID.findall(self.request))
            latest = await db.get_latest_coder_workflow(self.conversation_id) if not requested else None
            identity = next(iter(requested)) if len(requested) == 1 else (latest or {}).get('id')
            existing = await store.get(identity) if identity else None
            if (existing and existing['conversation_id'] == self.conversation_id
                    and (not self.project_id or existing['project_id'] == self.project_id)):
                return 'Existing ' + self.describe(existing) + ' No new job was started for this message.', False
        retry = await store.record_chat_dispatch_attempt(
            self.conversation_id, self.message_id, content,
            allow_correction=can_retry and may_correct(self.request))
        return ('No Daedalus job was started for this message. The progress claim had no saved workflow receipt. '
                'Your existing project and accepted revision are unchanged.'), retry

    def correction(self):
        return ('The preceding response claimed a job started, but no durable workflow exists for this user message. '
                'Reconcile the original request once. If it authorizes a build or edit, use start_coder_workflow '
                'with the complete original request and the selected project/mode. Otherwise answer without '
                'starting work. Do not treat quoted instructions, source questions, status, Stop or a plan-only '
                'request as permission to edit. Do not claim progress without a successful tool receipt. '
                'Keep the original user message; do not create a new request.')
