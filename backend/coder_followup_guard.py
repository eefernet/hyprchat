"""Stop a follow-up from silently becoming a brand-new project.

A conversation that already has a Daedalus project sends changes to it with edit_project. The chat model
sometimes answers a follow-up ("add the other direction, keep the existing behaviour") with
build_from_prompt, which starts an unrelated project and discards the user's work so far from the new job's
view. The model must say so explicitly before that happens. Nothing here decides for the user: a deliberate
new project is one flag away, and the composer's own New project choice bypasses this entirely.
"""
MESSAGE = ('This conversation already has the project {name!r}. A change, addition or fix to it uses mode edit_project. '
           'Nothing was started. Call start_coder_workflow again: with mode edit_project if the user is changing that '
           'project, or with mode build_from_prompt and new_project true only if the user asked for a separate, unrelated project.')


def refusal(args, options, project):
    """An error for the chat model, or '' when the request may proceed."""
    if not project or options.get('new_project') or options.get('project_id'):
        return ''                                   # no active project, or the user chose in the composer
    if args.get('mode') != 'build_from_prompt' or args.get('new_project') is True:
        return ''
    return MESSAGE.format(name=project.get('name') or project.get('id') or 'project')
