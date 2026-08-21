"""The channel, packaged as a Claude Code plugin, because that is what avoids a dialog.

Registering the channel as a plain MCP server (`claude mcp add`, then a
`server:` selector) is possible and costs a full-screen confirmation on every
single launch — which breaks the one thing this product exists to do, start a
Session by voice alone. Read out of Claude Code 2.1.222 and confirmed live by
the reference implementation: that confirmation is rendered only for a
`kind: "server"` channel entry, which can only ever be admitted with its `dev`
flag set, which only the development-channels flag sets. A `kind: "plugin"`
entry takes the other branch and is admitted with `dev` false whenever it
appears in the effective channel allowlist.

So the dialog is removed by never triggering it, and never by answering it.

Two runtime facts found the hard way shape what is rendered here, and neither is
documented:

- The plugin's MCP server must be declared **inline** under `mcpServers` in
  `plugin.json`. A `.mcp.json` inside the plugin directory is not loaded, and
  `claude plugin details` reports the opposite of the truth for both shapes.
- Claude Code copies an installed plugin into
  `plugins/cache/<marketplace>/<plugin>/<version>`, and that cache **survives**
  `claude plugin marketplace remove`. A fixed version would let a stale manifest
  outlive a reinstall, so the published version carries a fingerprint of the
  manifest it describes: identical inputs give an identical version, and any
  change to what the manifest says is a new directory by construction.

**No interpreter is named here.** Which Python runs the channel server is a
property of the deployment — the bundle's own interpreter, or whatever the
developer's checkout uses — so it is an argument, and the launcher and the
bundle are what supply it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.protocol import SERVER_NAME

#: The module the interpreter is asked to run. The server is part of this
#: package, so there is no separate script to install or keep in step.
CHANNEL_MODULE = "gpt_voicecoding.adapters.agent.claude.channel"

#: What the plugin and its one-plugin marketplace call themselves.
#:
#: The marketplace is **not** called `gpt-voicecoding`, and that is a decision
#: rather than an accident: the reference implementation already registers a
#: marketplace under that exact name, pointing at its own runtime. Two
#: generations of one product have to be installable side by side for as long as
#: the migration lasts — the same reason this engine's state lives beside the old
#: bridge's rather than on top of it — and a marketplace that replaced the
#: legacy one would take the working channel down with it.
#:
#: It is also not a migration alias to be renamed back later. Claude Code caches
#: an installed plugin by name and version and that cache outlives the
#: marketplace it came from, so a planned rename is scheduled identity churn.
#: One name, chosen once.
PLUGIN_NAME = "gpt-voicecoding-session-channel"
MARKETPLACE_NAME = "gpt-voicecoding-channel"

#: The half of the version a human reads. The other half is the fingerprint.
PLUGIN_BASE_VERSION = "1.0.0"

#: Where Claude Code looks for both manifests.
MANIFEST_DIRECTORY = ".claude-plugin"

#: How much of the manifest digest goes into the version: long enough that two
#: manifests will not collide, short enough to stay a readable directory name.
_FINGERPRINT_LENGTH = 12

PLUGIN_DESCRIPTION = (
    "Private Session Channel for GPT-VoiceCoding: carries the user's own spoken words "
    "into this Work Session, and reports back that this session received them."
)


def _body(interpreter: str | Path) -> dict[str, Any]:
    """Everything the plugin manifest says except its version.

    Split out because the version is a fingerprint of exactly this, so it cannot
    be part of what is fingerprinted.
    """
    return {
        "name": PLUGIN_NAME,
        "description": PLUGIN_DESCRIPTION,
        # Inline, because a `.mcp.json` in the plugin directory is not loaded.
        "mcpServers": {SERVER_NAME: {"command": str(interpreter), "args": ["-m", CHANNEL_MODULE]}},
        # The first-class way a plugin declares a channel: `server` names a key
        # of this plugin's own `mcpServers` above.
        "channels": [{"server": SERVER_NAME}],
    }


def plugin_version(interpreter: str | Path) -> str:
    """`<base>-<fingerprint>`, changing exactly when the manifest changes."""
    body = json.dumps(_body(interpreter), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{PLUGIN_BASE_VERSION}-{fingerprint[:_FINGERPRINT_LENGTH]}"


def plugin_manifest(interpreter: str | Path) -> dict[str, Any]:
    """`plugin.json`: the channel's MCP server, and the channel that binds it."""
    body = _body(interpreter)
    return {
        "name": body["name"],
        "version": plugin_version(interpreter),
        **{key: value for key, value in body.items() if key != "name"},
    }


def marketplace_manifest() -> dict[str, Any]:
    """`marketplace.json`: a one-plugin marketplace rooted at itself.

    The plugin lives at the marketplace root (`source: "./"`) so the directory
    that is registered and the directory Claude Code caches are the same one.
    """
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "GPT-VoiceCoding"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": "./",
                "description": PLUGIN_DESCRIPTION,
            }
        ],
    }


def channel_selector() -> str:
    """What a launch passes to `--channels`. The `plugin:` form is load-bearing."""
    return f"plugin:{PLUGIN_NAME}@{MARKETPLACE_NAME}"


def write_plugin(directory: Path, interpreter: str | Path) -> Path:
    """Lay both manifests down, and answer with the directory that was written."""
    manifests = directory / MANIFEST_DIRECTORY
    manifests.mkdir(parents=True, exist_ok=True)
    _write(manifests / "plugin.json", plugin_manifest(interpreter))
    _write(manifests / "marketplace.json", marketplace_manifest())
    return directory


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
