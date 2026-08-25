"""ADR 0011's fingerprinted hook block, in a Claude config directory's settings file.

A plugin cannot do this job: it is *cold*, and a Session already running settles
its plugin list at startup, so a plugin installed now never reaches the Sessions
v1.0 exists to bridge. A block in ``<config dir>/settings.json`` is hot in both
directions on a Session that is already open. That is ADR 0011, and this module
is what it decided, built.

**The fingerprint is a whole token, never a substring** — ported from
``legacy@1d32845:bridge/hookconfig.py:87-104`` and adapted one token along.
Legacy's hook was its own launcher, so "ours" was the *program*
(``Path(tokens[0]).name``); here the program is an interpreter and our identity
sits in a later argument. So the test walks every token and asks whether it names
a module of ours. It stays a token test: a neighbouring command that merely
*mentions* this package in a string survives untouched, which a substring match
would not have managed.

**Install replaces only our handlers.** A matcher group can hold several, and the
others in it are the user's — dropping the whole group because one handler is
ours would delete configuration this product never wrote. Proven against a file
carrying two foreign hooks (#71). Uninstall is the same merge with nothing of
ours to put back, so it reproduces the pre-install file byte for byte and the
round trip is checkable rather than asserted.

**What is *not* installed here.** ADR 0011 names two hooks, and only the
approval one exists as a module today; the ``SessionStart`` registration hook
arrives with the ticket that builds it (#74/#77). Installing a hook whose module
is not there would break every Session in the config directory, so the list below
is the list of hooks that run.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.installation import Outcome, State, replace_text

#: How this item is named in a report.
NAME: Final = "claude-hooks"

#: Where Claude Code keeps the settings this product merges into.
SETTINGS_FILE_NAME: Final = "settings.json"

#: The config directory Claude Code uses when nothing says otherwise, and the
#: variable that says otherwise. Coverage is per config directory at both ends
#: (#71): the Session registry lives inside it, so discovery is scoped by it
#: exactly as installation is.
CONFIG_DIRECTORY_VARIABLE: Final = "CLAUDE_CONFIG_DIR"
DEFAULT_CONFIG_DIRECTORY_NAME: Final = ".claude"

#: The approval wire. The hook process is held open and its return value is the
#: verdict; printing nothing hands the dialog back to the human.
APPROVAL_EVENT: Final = "PermissionRequest"
APPROVAL_MODULE: Final = "gpt_voicecoding.adapters.agent.claude.approval_hook"

#: The ceiling Claude Code puts on the hook process. It must not be *below*
#: Bridge Core's approval budget, or Claude Code would give up on a dialog the
#: engine is still holding open for the user — a test holds the two together
#: rather than a comment, because this number and that one live in two packages.
APPROVAL_TIMEOUT_SECONDS: Final = 600

#: What makes a handler ours: the interpreter is told to run a module of this
#: package. A prefix rather than a list of exact module names, so a build that
#: installs one more hook still recognises — and can still take back — what an
#: older build wrote here. See `runs_our_module` for what the prefix alone is
#: not enough to decide.
MODULE_PREFIX: Final = "gpt_voicecoding."

#: How an interpreter is told which module to run. The whole fingerprint rests on
#: this being the *only* way this product ever writes a hook command.
MODULE_FLAG: Final = "-m"


def default_config_directory(environ: Mapping[str, str], home: Path | None = None) -> Path:
    """The config directory this run installs into."""
    stated = environ.get(CONFIG_DIRECTORY_VARIABLE)
    if stated and stated.strip():
        return Path(stated.strip()).expanduser()
    return (home or Path.home()) / DEFAULT_CONFIG_DIRECTORY_NAME


def settings_path(config_directory: Path) -> Path:
    return config_directory / SETTINGS_FILE_NAME


def hook_command(interpreter: Path, module: str) -> str:
    """The command line one hook event runs. Quoted, because paths carry spaces."""
    return f"{shlex.quote(str(interpreter))} {MODULE_FLAG} {module}"


def desired_hooks(interpreter: Path) -> dict[str, list[dict[str, Any]]]:
    """The hooks this build installs, in the shape a settings file takes.

    No matcher on ``PermissionRequest``: narrowing by tool name here would be
    this installer deciding which of the user's dialogs may be answered by voice,
    and that is the user's decision, made per dialog, out loud.
    """
    return {
        APPROVAL_EVENT: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(interpreter, APPROVAL_MODULE),
                        "timeout": APPROVAL_TIMEOUT_SECONDS,
                    }
                ]
            }
        ]
    }


def is_ours(handler: Any) -> bool:
    """True only for a command handler that runs a module of this package."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:  # unbalanced quoting is somebody else's command
        return False
    return runs_our_module(tokens)


def runs_our_module(tokens: list[str]) -> bool:
    """Whether this command line *runs* a module of ours, rather than mentioning one.

    Two things have to hold, and each one is a way this went wrong before.

    The token must **be** a dotted module path. `shlex.split` hands quoted
    phrases back as single tokens, so ``logger 'gpt_voicecoding.hook ran'``
    produces one that starts with the prefix and is not a module at all.

    And it must be the argument of **-m**. Naming one of our modules is not
    running it: ``logger gpt_voicecoding.audit`` is somebody else's command that
    happens to mention us, and taking it back would break ADR 0011's promise to
    replace only our own handlers. The identity is the program the command runs;
    here the program is an interpreter, so the identity is the module it is told
    to run, and *told to run* is what ``-m`` means.

    The cost of being this exact: a future build that installs a console script
    rather than ``-m`` would not be recognised by this one, and would have to say
    so. That is the right way round — a rule that recognises too much takes other
    people's hooks with it.
    """
    return any(
        flag == MODULE_FLAG and _is_our_module_path(argument)
        for flag, argument in zip(tokens, tokens[1:], strict=False)
    )


def _is_our_module_path(token: str) -> bool:
    if not token.startswith(MODULE_PREFIX):
        return False
    return all(part.isidentifier() for part in token.split("."))


def without_ours(group: Any) -> Any:
    """The group with our handlers removed, or ``None`` when nothing of it remains."""
    if not isinstance(group, dict):
        return group
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return group
    kept = [handler for handler in handlers if not is_ours(handler)]
    if not kept:
        return None
    if len(kept) == len(handlers):
        return group
    return {**group, "hooks": kept}


def merge_hooks(existing: Mapping[str, Any], ours: Mapping[str, Any]) -> dict[str, Any]:
    """Replace this product's entries, keep every other hook untouched."""
    merged: dict[str, Any] = {
        event: list(groups) if isinstance(groups, list) else groups
        for event, groups in existing.items()
    }
    for event in set(merged) | set(ours):
        groups = merged.get(event, [])
        kept: list[Any] = []
        if isinstance(groups, list):
            for group in groups:
                remainder = without_ours(group)
                if remainder is not None:
                    kept.append(remainder)
        kept.extend(ours.get(event, []))
        if kept:
            merged[event] = kept
        else:
            merged.pop(event, None)
    return merged


def render(document: Mapping[str, Any], hooks: Mapping[str, Any]) -> str:
    """The settings file's new contents.

    ``indent=2`` and a trailing newline are what reproduce an untouched file byte
    for byte, which is what makes the uninstall round trip checkable (ADR 0011) —
    for a file already written that way, which is what #71 verified against the
    real one. A file kept at some other indent is reformatted by the first
    install, once, and then round-trips.

    An empty ``hooks`` mapping is dropped rather than written as ``{}``, so a
    file that never had the key gets it back exactly as it was.
    """
    updated = dict(document)
    if hooks:
        updated["hooks"] = dict(hooks)
    else:
        updated.pop("hooks", None)
    return json.dumps(updated, indent=2, ensure_ascii=False) + "\n"


def _read(path: Path) -> tuple[dict[str, Any], str] | str:
    """The settings document and its current text, or a sentence saying why not."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ({}, "")
    except OSError as refusal:
        return f"{path}: {refusal}"
    stripped = text.strip()
    if not stripped:
        return ({}, text)
    try:
        document: Any = json.loads(stripped)
    except json.JSONDecodeError as unreadable:
        return f"{path}: not JSON, so this install would destroy it: {unreadable}"
    if not isinstance(document, dict):
        return f"{path}: does not contain a JSON object"
    return (document, text)


def _current_hooks(document: Mapping[str, Any]) -> dict[str, Any]:
    hooks = document.get("hooks")
    return dict(hooks) if isinstance(hooks, dict) else {}


def inspect(config_directory: Path, interpreter: Path) -> Outcome:
    """What is in this config directory's settings file, without writing anything."""
    if not config_directory.is_dir():
        return Outcome(
            NAME,
            State.ABSENT,
            note=f"no Claude config directory at {config_directory} — nothing to install into",
        )

    path = settings_path(config_directory)
    read = _read(path)
    if isinstance(read, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=read)
    document, _ = read

    hooks = _current_hooks(document)
    if merge_hooks(hooks, {}) == hooks:
        return Outcome(NAME, State.ABSENT, note=f"no hooks of ours in {path}")
    if merge_hooks(hooks, desired_hooks(interpreter)) == hooks:
        return Outcome(NAME, State.CURRENT, note=str(path))
    return Outcome(
        NAME,
        State.STALE,
        note=f"{path} carries hooks of ours that this build would write differently",
    )


def install(config_directory: Path, interpreter: Path) -> Outcome:
    """Merge our hooks in, keeping every other hook in the file. Idempotent."""
    if not config_directory.is_dir():
        return inspect(config_directory, interpreter)
    standing = inspect(config_directory, interpreter)
    if not standing.ok or standing.state is State.CURRENT:
        return standing

    path = settings_path(config_directory)
    read = _read(path)
    if isinstance(read, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=read)
    document, _ = read

    contents = render(document, merge_hooks(_current_hooks(document), desired_hooks(interpreter)))
    failure = replace_text(path, contents)
    if failure:
        return Outcome(NAME, standing.state, ok=False, note=failure)
    return Outcome(NAME, State.CURRENT, changed=True, note=str(path))


def uninstall(config_directory: Path) -> Outcome:
    """Take back exactly our handlers, and nothing else.

    A file with nothing of ours in it is not rewritten. Rendering it would
    reformat a file this product has no business reformatting.
    """
    path = settings_path(config_directory)
    read = _read(path)
    if isinstance(read, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=read)
    document, text = read
    if not text:
        return Outcome(NAME, State.ABSENT, note=f"nothing of ours at {path}")

    hooks = _current_hooks(document)
    remaining = merge_hooks(hooks, {})
    if remaining == hooks:
        return Outcome(NAME, State.ABSENT, note=f"nothing of ours at {path}")

    failure = replace_text(path, render(document, remaining))
    if failure:
        return Outcome(NAME, State.STALE, ok=False, note=failure)
    return Outcome(NAME, State.ABSENT, changed=True, note=str(path))
