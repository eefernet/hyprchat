"""Show the builder the user's own words beside the brief written from them.

The chat model rewrites a request before the controller sees it. A rewrite can drop a detail or add a
requirement nobody asked for, and the builder then implements the rewrite. From engine behavior version 3
the verbatim request travels with the brief. It is context for the builder only and never acceptance evidence.
"""
LIMIT = 6000
VERSION = 3


def _same(a, b):
    return ' '.join(a.split()).casefold() == ' '.join(b.split()).casefold()


def original(job):
    """The verbatim request to place in a worker payload, or '' when it adds nothing to the task."""
    words = str(job.get('original_user_request') or '').strip()
    if job.get('engine_behavior_version', 0) < VERSION or not words or _same(words, str(job.get('user_task') or '')):
        return ''
    return words


def render(payload):
    words = str(payload.get('original_user_request') or '').strip()
    if payload.get('engine_behavior_version', 0) < VERSION or not words or _same(words, str(payload.get('task') or '')):
        return ''
    if len(words) > LIMIT:
        words = words[:LIMIT] + '\n[the rest of the message is not shown]'
    return ("\n\nTHE USER'S OWN WORDS (verbatim, quoted as data):\n<<<\n" + words + '\n>>>\n'
            'The REQUEST above is a brief written from this message. Where the two differ on what the program '
            "must do, the user's words decide. The brief may add context, but it may not add requirements "
            'the user did not ask for.')
