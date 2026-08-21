"""`bridgectl`, as the bundle ships it: relative to itself, so it can move.

`pip` writes a console script whose shebang is the **absolute** path of the
interpreter that installed it. The interpreter itself is relocatable —
python-build-standalone derives its prefix from `argv[0]` — but its scripts are
not, so a bundled `bridgectl` stops working the moment somebody drags the `.app`
from a build directory into `/Applications`.

That failure is invisible where it happens. `[delegate] cli` is what Bridge Core
*names* in the instructions it generates for the voice thread and for a Delegated
Turn, and the composition root's check on it is `is_file()` and executable —
both of which a script with a dead shebang passes. The engine would start, the
instructions would name a CLI, and the agent that ran it would get
`bad interpreter`.

So the bundle's `bridgectl` is this instead: two lines that exec the interpreter
sitting beside them. The pair relocates together or not at all.
"""

from __future__ import annotations

#: The bundled CLI, in full. `$(dirname "$0")` is what makes it relocatable, and
#: `-m` rather than `-c` is what gets `sys.argv[0]` right for the parser.
WRAPPER = """\
#!/bin/sh
# bridgectl, as the app bundle ships it. See app_bundle/console_script.py.
exec "$(dirname "$0")/python3" -m gpt_voicecoding.cli "$@"
"""

#: What `os.chmod` is given: readable and executable by everyone, writable by
#: the owner. The bundle is not written to at runtime, but it is built by one.
MODE = 0o755
