"""Preserve user clarification text on continued modern jobs."""

def effective_task(job):
    original = job.get('user_task', job.get('task', ''))
    notes = job.get('scope_clarifications') or []
    if not notes:
        return original
    return original + '\n\nUSER CLARIFICATIONS (latest takes precedence):\n' + '\n'.join(n['text'] for n in notes)
