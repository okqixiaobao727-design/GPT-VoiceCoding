"""How both lanes turn a project and a title into one Session Name.

**Pure, and shared.** Two lanes compose the same shape from different facts —
Claude's official roster `name`, Codex's daemon `Thread.name` — and a rule that
lived twice would drift twice. Nothing here touches the filesystem, a process or
a clock: the caller resolves the project half (`_project.py`) and hands it in,
which is what keeps this testable against strings rather than against whichever
repositories happen to be on the machine.

**The title is validated the way the reference implementation validated it**
(`legacy@1d32845:bridge/labels.py:97-106`, *adapted*): non-empty after stripping,
and one line. Legacy raised `SessionLabelError` on both, from a resolver called
at the moment a Session reported its own title; here the caller is a discovery
pass over every row on the machine every few seconds, and an exception per
unnameable row would make a naming problem look like a lane failure. So the
answer is `None` — "this Session has no name yet" — which is the state the
roster already renders (`core/sessions.py`).

**What is deliberately not here.** No transcript-derived title, no `ai-title`,
no mutable history: legacy measured that route at 30% of Sessions never writing
a title and the rest writing one a median of 440 s late
(`legacy@1d32845:bridge/labels.py:73-84`, *dropped, because* a record a product
writes when it feels like it cannot carry a name the user speaks with). A name
composed here is composed from a fact the lane already had.
"""

from __future__ import annotations

from gpt_voicecoding.seams.identity import NAME_SEPARATOR, SessionName


def compose(project_name: str, title: str) -> SessionName | None:
    """``<project> · <title>``, or `None` when the parts cannot make one.

    Both halves are stripped first, because the sources are other people's
    strings: a roster field and a daemon field, either of which may arrive
    padded. A title that is empty or spans more than one line is refused rather
    than flattened — a Session Name is spoken and typed after `@`, so a name
    carrying a newline would be a name the user cannot say back.

    The separator is refused inside either half by `SessionName` itself, and
    that refusal is caught here for the same reason as the rest: a Codex thread
    the user named `a · b` is a Session that has no name, not a lane that broke.
    """
    project = project_name.strip()
    task = title.strip()
    if not project or not task:
        return None
    if len(task.splitlines()) > 1:
        return None
    if NAME_SEPARATOR.strip() in project or NAME_SEPARATOR.strip() in task:
        return None
    return SessionName(project=project, task=task)
