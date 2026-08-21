"""The hook, packaged as a session-scoped plugin, because that is what scopes it.

A `PermissionRequest` hook can be registered three ways, and the difference
between them is not convenience — it is blast radius.

- Writing it into `~/.claude/settings.json` means this engine read-modify-writes
  a user-owned file it otherwise never touches, and one bad merge takes somebody
  else's hooks with it.
- Shipping it inside the installed channel plugin means the hook fires for
  **every** Claude Code session on the machine, including ones this engine never
  launched, gated only by the hook's own good manners.
- `claude --plugin-dir <path>` loads a plugin **for that session only** — verified
  live against 2.1.238, with a plugin carrying nothing but `plugin.json` and
  `hooks/hooks.json`. No marketplace, no `claude plugin install`, no
  administrator-owned managed-settings entry, and nothing written outside this
  engine's own runtime tree.

The third is what this module renders, and it is what makes "no phantom events"
structural rather than disciplinary: a Session that did not get the flag has no
hook to fire, so there is no foreign dialog for the engine to refuse. The hook's
own bootstrap-variable check remains as a second line, not as the scope.

**A separate directory from the channel plugin, and it has to be.** The channel
is selected as `plugin:<name>@<marketplace>`, which only resolves for a plugin
installed from a registered marketplace; `--plugin-dir` has no marketplace. One
directory cannot be both without being loaded twice, so there are two plugins.
The name below is chosen once and does not change, for the same reason the
channel plugin's does not: Claude Code caches a plugin by name and version, and a
planned rename is scheduled identity churn.

**The version is a fingerprint of what the plugin actually says**, exactly as the
channel plugin's is, so a changed hook command is a new directory by construction
and no stale copy can outlive it.

**No interpreter is named here.** Which Python runs the hook is a property of the
deployment — the same rule, and the same reason, as the channel server's (ADR
0006): the manifest takes it as an argument, and the launcher and the bundle are
what supply it.

Install is `write_hook_plugin` and uninstall is `remove_hook_plugin`, which takes
back exactly the two files an install wrote and no more. Neither touches a
settings file — which is the whole point.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.approval import HOOK_EVENT

#: The module Claude Code is asked to run, one process per displayed dialog. It
#: is part of this package, so there is no separate script to install or keep in
#: step with the engine that answers it.
HOOK_MODULE: Final = "gpt_voicecoding.adapters.agent.claude.approval_hook"

#: One name, chosen once. See the module note.
HOOK_PLUGIN_NAME: Final = "gpt-voicecoding-approval-hook"

#: The half of the version a human reads. The other half is the fingerprint.
HOOK_PLUGIN_BASE_VERSION: Final = "1.0.0"

#: Where Claude Code looks for the manifest, and where it looks for the hooks.
MANIFEST_DIRECTORY: Final = ".claude-plugin"
HOOKS_DIRECTORY: Final = "hooks"
HOOKS_FILE: Final = "hooks.json"

#: How much of the digest goes into the version: long enough not to collide,
#: short enough to stay a readable directory name.
_FINGERPRINT_LENGTH: Final = 12

HOOK_PLUGIN_DESCRIPTION: Final = (
    "Approval Relay for GPT-VoiceCoding: carries the user's spoken allow or deny into this "
    "Work Session's pending permission dialog, and hands the dialog back untouched when no "
    "answer arrives."
)

#: How long Claude Code gives the hook, in seconds. It is Claude Code's own
#: default and is stated rather than left implicit, because an unstated budget is
#: one that moves when the product's default moves. It is deliberately *not*
#: derived from `CorePolicy.approval_budget_seconds`: that budget belongs to
#: Bridge Core, which answers `ask` when it runs out, and this one is only the
#: outer wall the hook process lives inside. Two names for two facts that happen
#: to be the same number today.
HOOK_TIMEOUT_SECONDS: Final = 600


def hooks_document(interpreter: str | Path) -> dict[str, Any]:
    """`hooks/hooks.json`: one hook, on one event, with no matcher.

    No matcher, so every dialog is offered — the point of the Approval Relay is
    the away-from-keyboard case, and a tool allow-list here would be this engine
    quietly deciding which of the user's permission prompts they are allowed to
    answer by voice. The ceiling on what a spoken word may *grant* is enforced in
    `approval.py`, where it belongs; what may be *asked* is not narrowed at all.
    """
    return {
        "hooks": {
            HOOK_EVENT: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            # Quoted, because this is a shell command line and a
                            # perfectly ordinary interpreter path — anything under
                            # "Application Support", for one — contains a space.
                            # Unquoted, that launch fails with 127 and the only
                            # symptom is a permission dialog nobody ever answers.
                            "command": f"{shlex.quote(str(interpreter))} -m {HOOK_MODULE}",
                            "timeout": HOOK_TIMEOUT_SECONDS,
                        }
                    ]
                }
            ]
        }
    }


def hook_plugin_version(interpreter: str | Path) -> str:
    """`<base>-<fingerprint>`, changing exactly when what the plugin says changes."""
    body = json.dumps(
        hooks_document(interpreter), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{HOOK_PLUGIN_BASE_VERSION}-{fingerprint[:_FINGERPRINT_LENGTH]}"


def hook_plugin_manifest(interpreter: str | Path) -> dict[str, Any]:
    """`plugin.json`. It declares no MCP server and no channel: this plugin is the hook."""
    return {
        "name": HOOK_PLUGIN_NAME,
        "version": hook_plugin_version(interpreter),
        "description": HOOK_PLUGIN_DESCRIPTION,
    }


#: Exactly what an install writes, relative to the plugin directory. Uninstall
#: removes these and nothing else, so one list is the definition of both halves.
MANIFEST_FILE: Final = Path(MANIFEST_DIRECTORY) / "plugin.json"
RENDERED_FILES: Final = (MANIFEST_FILE, Path(HOOKS_DIRECTORY) / HOOKS_FILE)


def write_hook_plugin(directory: Path, interpreter: str | Path) -> Path:
    """Lay the plugin down, and answer with the directory a launch should be given."""
    bodies = (hook_plugin_manifest(interpreter), hooks_document(interpreter))
    for relative, document in zip(RENDERED_FILES, bodies, strict=True):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, document)
    return directory


def remove_hook_plugin(directory: Path) -> bool:
    """The uninstall path: take back exactly the files an install wrote.

    Two rules, and the second is the one that matters.

    It refuses to touch anything that is not recognisably ours — a directory
    without our manifest, or with somebody else's name in it — because an
    uninstall that takes a caller's word for which directory to delete is a
    recursive delete with a configuration file for an argument.

    And even once it is ours, it **removes the files it wrote and nothing
    else**, then takes the directories back only if they are empty. A recursive
    delete of the whole directory would take a caller's own contents with it the
    moment anyone rendered the plugin into a directory that had something in it
    — which is a path this engine's own configuration could hand it. Leaving a
    non-empty directory behind is the right failure: the plugin is gone, and
    whatever else was in there is not this function's to judge.
    """
    manifest = directory / MANIFEST_FILE
    try:
        document: Any = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or document.get("name") != HOOK_PLUGIN_NAME:
        return False

    for relative in RENDERED_FILES:
        with contextlib.suppress(OSError):
            (directory / relative).unlink()
    # Deepest first, and `rmdir` rather than a tree removal, so a directory
    # somebody else put something in survives.
    for relative in RENDERED_FILES:
        with contextlib.suppress(OSError):
            (directory / relative).parent.rmdir()
    with contextlib.suppress(OSError):
        directory.rmdir()
    return not manifest.exists()


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
