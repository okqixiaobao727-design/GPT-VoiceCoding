"""The channel's plugin packaging: the two manifests, and the version that moves.

Everything asserted here was found the hard way by the reference implementation
and is invisible in the documentation — the inline `mcpServers`, the `plugin:`
selector that avoids a launch dialog, and a version that changes with the
manifest because Claude Code's plugin cache outlives the marketplace it came
from.
"""

from __future__ import annotations

import json
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude.plugin import (
    CHANNEL_MODULE,
    MANIFEST_DIRECTORY,
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    channel_selector,
    marketplace_manifest,
    plugin_manifest,
    plugin_version,
    write_plugin,
)
from gpt_voicecoding.adapters.agent.claude.protocol import SERVER_NAME


class TestWhatTheManifestMustSay:
    def test_the_mcp_server_is_declared_inline(self) -> None:
        """A `.mcp.json` inside the plugin directory is not loaded — verified live."""
        manifest = plugin_manifest("/opt/vc/bin/python3")
        assert manifest["mcpServers"][SERVER_NAME] == {
            "command": "/opt/vc/bin/python3",
            "args": ["-m", CHANNEL_MODULE],
        }

    def test_the_channel_names_this_plugins_own_server(self) -> None:
        assert plugin_manifest("python3")["channels"] == [{"server": SERVER_NAME}]

    def test_no_interpreter_is_baked_in(self) -> None:
        """Which Python runs the channel belongs to the deployment, not to this module."""
        rendered = json.dumps(plugin_manifest("/somewhere/else/python3"))
        assert "/somewhere/else/python3" in rendered
        assert "/opt/vc" not in rendered

    def test_the_selector_is_the_plugin_form_that_avoids_the_launch_dialog(self) -> None:
        """A `server:` selector can only be admitted with `dev` set, which draws the dialog."""
        assert channel_selector() == f"plugin:{PLUGIN_NAME}@{MARKETPLACE_NAME}"

    def test_the_marketplace_is_not_the_one_the_reference_implementation_registers(self) -> None:
        """Two generations of one product have to be installable side by side.

        The reference implementation registers a marketplace called
        `gpt-voicecoding`, pointing at its own runtime and carrying the channel
        that is presently in use. A second marketplace under that name would
        replace it.
        """
        assert MARKETPLACE_NAME == "gpt-voicecoding-channel"
        assert channel_selector().endswith("@gpt-voicecoding-channel")

    def test_the_marketplace_is_rooted_at_the_plugin_itself(self) -> None:
        """So the directory that is registered and the one Claude caches are one."""
        marketplace = marketplace_manifest()
        assert marketplace["plugins"] == [
            {
                "name": PLUGIN_NAME,
                "source": "./",
                "description": plugin_manifest("python3")["description"],
            }
        ]


class TestTheVersionThatOutlivesAReinstall:
    def test_the_same_manifest_gives_the_same_version(self) -> None:
        """A no-op reinstall must stay a no-op."""
        assert plugin_version("python3") == plugin_version("python3")

    def test_a_changed_manifest_gives_a_changed_version(self) -> None:
        """The cache survives `marketplace remove`, so only this makes a change visible."""
        assert plugin_version("python3") != plugin_version("/opt/vc/bin/python3")

    def test_the_version_the_manifest_declares_is_the_one_it_fingerprints(self) -> None:
        assert plugin_manifest("python3")["version"] == plugin_version("python3")


class TestLayingItDown:
    def test_both_manifests_are_written_where_claude_code_looks(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "python3")
        manifests = tmp_path / MANIFEST_DIRECTORY
        assert json.loads((manifests / "plugin.json").read_text())["name"] == PLUGIN_NAME
        assert json.loads((manifests / "marketplace.json").read_text())["name"] == MARKETPLACE_NAME

    def test_writing_twice_leaves_exactly_one_of_each(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "python3")
        write_plugin(tmp_path, "python3")
        assert sorted(path.name for path in (tmp_path / MANIFEST_DIRECTORY).iterdir()) == [
            "marketplace.json",
            "plugin.json",
        ]
