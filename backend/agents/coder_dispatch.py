"""Application-side dispatch receipts; this module does not belong on the worker."""
import json
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


# What an answer to a QUESTION must say before it counts as claiming work: that a job started, or that the
# user's job is running now. A question cannot start a job, so "in progress" or "running in the background"
# as plain words (an answer about spinners, CI or background tasks) and "I've added an example below" are
# ordinary prose there. Found 2026-11-13: such answers were replaced by "No Daedalus job was started".
_PROGRESS = r"(?:underway|in progress|running in (?:the )?background)"
_JOB_CLAIM = re.compile(
    r"\b(?:I(?:['’]ve| have)?|we(?:['’]ve| have)?|(?:the|your) (?:job|workflow|build|edit|update))"
    r"\s+(?:is |has |have )?(?:started|launched|queued|dispatched|begun)\b"
    r"|\b(?:I['’]m|I am)\s+(?:starting|building|implementing|updating)\b"
    r"|\b(?:Daedalus job|Project question)\s+\S+\s+is saved with status\b"
    r"|\byour\b[^.!?\n]{0,40}?\b(?:job|workflow|build|edit|update|change|feature|fix|request|project)\b"
    r"[^.!?\n]{0,40}?\b" + _PROGRESS + r"\b"
    r"|\b(?:the|this)\s+(?:Daedalus\s+)?(?:job|workflow|build)\b[^.!?\n]{0,30}?\b" + _PROGRESS + r"\b",
    re.I,
)
_NEGATED = re.compile(r"\b(?:no|not|never|hasn't|haven't|isn't|didn't|couldn't)\b", re.I)
# "While the build is in progress, the badge is yellow" describes a condition; it reports nothing.
_CONDITION = re.compile(r"\b(?:while|when|whenever|if|until|once|whether|as long as)\b", re.I)


def claims_start(content, about_job=False):
    """True when a reply says work was started or done. ``about_job`` is for answers to questions: only
    a claim about the job itself counts (see _JOB_CLAIM), and not inside a "while/when/if" clause."""
    text = re.sub(r'```.*?```|`[^`\n]*\s[^`\n]*`|"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]+\x27', '', content, flags=re.S)
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
    for sentence in re.split(r'[.!?\n]', text):
        found = (_JOB_CLAIM if about_job else _CLAIM).search(sentence)
        if not found or _NEGATED.search(sentence):
            continue
        if about_job and _CONDITION.search(sentence[:found.start()]):
            continue
        return True
    return False


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

    async def saved(self):
        """(job, problem). ``problem`` is a sentence for the reply when this message already has a job
        that does not fit the request as sent now (another project or "new project" picked on a retry).
        The chat used to end with an empty reply there."""
        try:
            return await self.receipt(), ''
        except ValueError as error:
            return None, f'{error} Send it as a new message to use the project selected now.'

    @staticmethod
    def refusal(result):
        """Why the job router started nothing, as a sentence for the reply. '' when it gave no reason.
        The reply used to say only "the workflow tool did not produce a matching saved job"."""
        text = str(result or '').strip()
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return ' '.join(text.split())[:400] if re.match(r'(?i)error\b', text) else ''
        if data.get('request_started') is False and data.get('workflow_id') and data.get('state'):
            return (f"An earlier job in this chat is still {data['state']} (`{data['workflow_id']}`). "
                    'Wait for it to finish or stop it, then send this again.')
        reason = data.get('error') or (data.get('message') if data.get('request_started') is False else '')
        return ' '.join(str(reason or '').split())[:400]

    @staticmethod
    def not_started(reason=''):
        return ('No new Daedalus job was started for this message. ' + (reason or
                'The workflow tool did not produce a matching saved job. Check its result before retrying.'))

    @staticmethod
    def describe(job):
        kind = 'Project question' if job['mode'] == 'ask_uploaded_project' else 'Daedalus job'
        return (f"{kind} `{job['id']}` is saved with status **{job['state']}**. "
                'The progress card shows its current state. Completion and review are separate from starting the job.')

    async def review(self, content, *, can_retry):
        # A question cannot start a job, so its answer is checked only for claims about the job itself.
        # The broad check used to replace ordinary answers ("show an in-progress spinner like this…")
        # with "No Daedalus job was started for this message" (found 2026-11-13).
        if not claims_start(content, about_job=not may_correct(self.request)):
            return content, False
        job, problem = await self.saved()
        if problem:
            return problem, False
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
