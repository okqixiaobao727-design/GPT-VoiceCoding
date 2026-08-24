"""The build pipeline's plan side, which is the half that can be silently wrong.

`codesign --verify --deep --strict` is the only after-the-fact oracle this
pipeline has, and it does not walk `Contents/Resources` — which is exactly why
the ticket locked an explicit enumerate-and-sign instead of `--deep`. So a
missed `.so` produces an app that verifies clean and fails at the one moment it
matters. These tests are the oracle `codesign` cannot be: what is signable, and
in what order.
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from app_bundle import console_script, inputs, lock, mach_o, signing
from app_bundle import run as bundle_run
from app_bundle.plan import BuildPlan

from gpt_voicecoding import config

#: What every binary in a current arm64 tree actually starts with.
MACH_O = b"\xcf\xfa\xed\xfe"


def make_engine_tree(root: Path) -> None:
    """A miniature of the assembled engine tree, with every shape that has bitten.

    All of these exist in the shipping bundle: a suffix-less interpreter, a
    symlink beside it, an extension module under `lib-dynload`, a dylib that
    arrives inside a *data* directory rather than beside a module, a console
    script that is text, and Python source that outnumbers the code to sign by
    two orders of magnitude.
    """
    (root / "bin").mkdir(parents=True)
    (root / "bin/python3.12").write_bytes(MACH_O + b"interpreter")
    (root / "bin/python3").symlink_to("python3.12")
    (root / "bin/bridgectl").write_text("#!/bundled/python3\nfrom gpt_voicecoding.cli import main")
    (root / "lib/python3.12/lib-dynload").mkdir(parents=True)
    (root / "lib/python3.12/lib-dynload/_crypt.cpython-312-darwin.so").write_bytes(MACH_O + b"x")
    (root / "lib/libpython3.12.dylib").write_bytes(MACH_O + b"y")
    portaudio = root / "lib/python3.12/site-packages/_sounddevice_data/portaudio-binaries"
    portaudio.mkdir(parents=True)
    (portaudio / "libportaudio.dylib").write_bytes(MACH_O + b"z")
    package = root / "lib/python3.12/site-packages/gpt_voicecoding"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.0.0'")


class TestEveryConsoleScriptRelocates:
    def test_installed_python_scripts_move_with_the_engine(self, tmp_path: Path) -> None:
        engine = tmp_path / "built-engine"
        bin_directory = engine / "bin"
        bin_directory.mkdir(parents=True)
        python = bin_directory / "python3"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)

        names = ("cffi-gen-src", "pyav", "future-dependency-script")
        for name in names:
            script = bin_directory / name
            script.write_text(f"#!{python}\nraise SystemExit(0)\n")
            script.chmod(0o755)

        console_script.relocate_all(bin_directory)

        assert {name: (bin_directory / name).read_text().splitlines()[0] for name in names} == {
            "cffi-gen-src": "#!/bin/sh",
            "pyav": "#!/bin/sh",
            "future-dependency-script": "#!/bin/sh",
        }
        moved = tmp_path / "moved-engine"
        engine.rename(moved)
        for name in names:
            subprocess.run([moved / "bin" / name], check=True)
        commands = tmp_path / "commands"
        commands.mkdir()
        linked = commands / "bridgectl"
        linked.symlink_to(moved / "bin/future-dependency-script")
        subprocess.run([linked], check=True)

    def test_non_python_entries_are_left_as_installed(self, tmp_path: Path) -> None:
        bin_directory = tmp_path / "engine/bin"
        bin_directory.mkdir(parents=True)
        python = bin_directory / "python3"
        python.write_bytes(MACH_O + b"interpreter")
        python.chmod(0o755)
        shell_script = bin_directory / "pip"
        shell_script.write_text("#!/bin/sh\nexit 0\n")
        shell_script.chmod(0o755)
        binary = bin_directory / "python3.12"
        binary.write_bytes(MACH_O + b"binary")
        binary.chmod(0o755)
        linked = bin_directory / "pip3"
        linked.symlink_to("pip")
        before = {path.name: path.read_bytes() for path in (python, shell_script, binary)}

        console_script.relocate_all(bin_directory)

        assert {path.name: path.read_bytes() for path in (python, shell_script, binary)} == before
        assert linked.readlink() == Path("pip")


class TestWhatIsSignable:
    """Signable means Mach-O, and Mach-O means the first four bytes say so."""

    @pytest.mark.parametrize(
        "magic",
        [
            b"\xcf\xfa\xed\xfe",  # 64-bit, little-endian — every arm64 and x86_64 binary here
            b"\xce\xfa\xed\xfe",  # 32-bit, little-endian
            b"\xfe\xed\xfa\xcf",  # 64-bit, big-endian
            b"\xfe\xed\xfa\xce",  # 32-bit, big-endian
            b"\xca\xfe\xba\xbe",  # a fat archive of the above
            b"\xbf\xba\xfe\xca",  # a 64-bit fat archive, byte-swapped
        ],
    )
    def test_every_mach_o_magic_counts(self, magic: bytes) -> None:
        assert mach_o.is_mach_o(magic + b"the rest of the file")

    @pytest.mark.parametrize(
        "header",
        [
            b"#!/usr/bin/env python3\n",  # a console script — text, not code to sign
            b"PK\x03\x04",  # a wheel or a zip
            b"\x7fELF",  # Linux, which cannot get here but says so clearly if it does
            b"",  # an empty file
            b"\xcf\xfa\xed",  # three bytes: not enough to be anything
        ],
    )
    def test_nothing_else_does(self, header: bytes) -> None:
        assert not mach_o.is_mach_o(header)

    def test_it_reads_bytes_and_not_a_path(self) -> None:
        """A pure function of bytes, so it is testable with no filesystem at all.

        The walk that applies it has to touch a disk; this does not, and keeping
        the two apart is what makes the interesting half assertable.
        """
        assert mach_o.is_mach_o(b"\xcf\xfa\xed\xfe") is True


class TestTheWalk:
    def test_it_finds_every_mach_o_wherever_it_hides(self, tmp_path: Path) -> None:
        make_engine_tree(tmp_path)
        found = {path.relative_to(tmp_path).as_posix() for path in mach_o.signable(tmp_path)}
        assert found == {
            "bin/python3.12",
            "lib/libpython3.12.dylib",
            "lib/python3.12/lib-dynload/_crypt.cpython-312-darwin.so",
            "lib/python3.12/site-packages/_sounddevice_data/portaudio-binaries/libportaudio.dylib",
        }

    def test_it_never_returns_a_symlink(self, tmp_path: Path) -> None:
        """`bin/python3` points at `bin/python3.12`.

        Signing through the link would sign one file twice under two names, and
        the second signature is the one that survives — which is how a plan that
        looks complete produces a tree that is not.
        """
        make_engine_tree(tmp_path)
        assert not any(path.is_symlink() for path in mach_o.signable(tmp_path))

    def test_the_order_is_stable_across_runs(self, tmp_path: Path) -> None:
        """The signing order derives from this one, so it may not drift."""
        make_engine_tree(tmp_path)
        assert mach_o.signable(tmp_path) == mach_o.signable(tmp_path)
        assert list(mach_o.signable(tmp_path)) == sorted(mach_o.signable(tmp_path))

    def test_an_empty_tree_is_not_an_error(self, tmp_path: Path) -> None:
        assert mach_o.signable(tmp_path) == ()


def make_app(root: Path, *, with_engine: bool = True) -> Path:
    """An assembled bundle, in the shape the pipeline produces one."""
    app = root / "GPT-VoiceCoding.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/GPTVoiceCodingShell").write_bytes(MACH_O + b"the shell")
    (app / "Contents/Info.plist").write_text("<plist/>")
    if with_engine:
        make_engine_tree(app / "Contents/Resources/engine")
    return app


class TestBundleSelfContainment:
    def test_a_bundle_with_only_the_user_config_placeholder_passes(self, tmp_path: Path) -> None:
        app = make_app(tmp_path, with_engine=False)
        example = app / "Contents/Resources/config.example.toml"
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text('workspace = "/Users/you/Documents/coding/GPT-VoiceCoding"\n')

        bundle_run.verify_self_contained(
            app,
            source_root=Path("/Users/simon/Documents/coding/GPT-VoiceCoding"),
        )

    def test_a_source_checkout_reference_anywhere_in_the_bundle_fails(self, tmp_path: Path) -> None:
        app = make_app(tmp_path, with_engine=False)
        source_root = Path(
            "/Users/simon/Documents/coding/GPT-VoiceCoding/.claude/worktrees/issue-12-app-bundle"
        )
        provenance = (
            app / "Contents/Resources/engine/lib/python3.12/site-packages/"
            "gpt_voicecoding-0.0.0.dist-info/direct_url.json"
        )
        provenance.parent.mkdir(parents=True)
        provenance.write_text(f'{{"url": "file://{source_root}"}}')

        with pytest.raises(bundle_run.BuildFailed, match="direct_url.json"):
            bundle_run.verify_self_contained(app, source_root=source_root)

    def test_a_worktree_build_also_rejects_the_main_checkout_path(self, tmp_path: Path) -> None:
        checkout = tmp_path / "GPT-VoiceCoding"
        git_directory = checkout / ".git"
        worktree_git_directory = git_directory / "worktrees/43-43"
        worktree_git_directory.mkdir(parents=True)
        worktree = checkout / ".claude/worktrees/43-43"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {worktree_git_directory}\n")
        app = make_app(tmp_path / "artifact", with_engine=False)
        leaked = app / "Contents/Resources/channel.json"
        leaked.parent.mkdir(parents=True, exist_ok=True)
        leaked.write_text(f'{{"interpreter": "{checkout}/.venv/bin/python"}}')

        with pytest.raises(bundle_run.BuildFailed, match="channel.json"):
            bundle_run.verify_self_contained(app, source_root=worktree)

    def test_an_external_worktree_path_is_also_rejected(self, tmp_path: Path) -> None:
        checkout = tmp_path / "repository/GPT-VoiceCoding"
        worktree_git_directory = checkout / ".git/worktrees/issue-12-app-bundle"
        worktree_git_directory.mkdir(parents=True)
        worktree = tmp_path / "worktrees/issue-12-app-bundle"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {worktree_git_directory}\n")
        app = make_app(tmp_path / "artifact", with_engine=False)
        script = app / "Contents/Resources/engine/bin/cffi-gen-src"
        script.parent.mkdir(parents=True)
        script.write_text(f"#!{worktree}/shell/.build/GPT-VoiceCoding.app/python3\n")

        with pytest.raises(bundle_run.BuildFailed, match="cffi-gen-src"):
            bundle_run.verify_self_contained(app, source_root=worktree)

    def test_the_local_install_does_not_leave_source_provenance(self, tmp_path: Path) -> None:
        plan = BuildPlan.resolve(machine="arm64", build_root=tmp_path)
        python = plan.engine_root / "bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        provenance = (
            plan.engine_root / "lib/python3.12/site-packages/"
            "gpt_voicecoding-0.0.0.dist-info/direct_url.json"
        )
        provenance.parent.mkdir(parents=True)
        provenance.write_text(f'{{"url": "file://{inputs.REPO_ROOT}"}}')
        record = provenance.with_name("RECORD")
        record.write_text(
            f"{provenance.parent.name}/direct_url.json,sha256=ticket-43,103\n"
            f"{provenance.parent.name}/METADATA,,\n"
        )

        bundle_run.install(plan)
        bundle_run.remove_install_provenance(plan)

        assert not provenance.exists()
        assert "direct_url.json" not in record.read_text()


class TestTheSigningPlan:
    """Inside-out, once each, and the app last.

    `codesign` seals a bundle at the moment it signs it, so anything written or
    re-signed underneath afterwards leaves a bundle whose seal no longer matches
    its contents. The order is therefore load-bearing, and it is the part
    `--verify` will happily agree with right up until it does not.
    """

    def plan(self, app: Path) -> signing.SigningPlan:
        return signing.plan_for(app, entitlements=Path("engine.entitlements"))

    def test_the_app_is_signed_last(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        steps = self.plan(app).steps
        assert steps[-1].path == app

    def test_nothing_is_signed_twice(self, tmp_path: Path) -> None:
        steps = self.plan(make_app(tmp_path)).steps
        assert len({step.path for step in steps}) == len(steps)

    def test_every_mach_o_in_the_bundle_is_in_the_plan(self, tmp_path: Path) -> None:
        """The enumeration is the whole point: `--deep` never walks Resources."""
        app = make_app(tmp_path)
        planned = {step.path for step in self.plan(app).steps}
        assert set(mach_o.signable(app)) <= planned

    def test_deeper_files_are_signed_before_shallower_ones(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        depths = [len(step.path.parts) for step in self.plan(app).steps[:-1]]
        assert depths == sorted(depths, reverse=True)

    def test_only_the_bundled_interpreter_carries_the_microphone_entitlement(
        self, tmp_path: Path
    ) -> None:
        """The app's Info.plist holds the sentence; the interpreter holds the right.

        Verified by the TCC probe: the prompt names the app and shows the app's
        usage string, and `python3.12` never appears — but it is `python3.12` that
        opens the device, so it is `python3.12` that is signed for it.
        """
        app = make_app(tmp_path)
        entitled = [step.path for step in self.plan(app).steps if step.entitlements is not None]
        assert entitled == [app / "Contents/Resources/engine/bin/python3.12"]

    def test_the_entitlement_lands_on_the_file_and_not_on_the_link(self, tmp_path: Path) -> None:
        """`engine/bin/python3` is a symlink; signing it signs its target twice."""
        app = make_app(tmp_path)
        entitled = next(step for step in self.plan(app).steps if step.entitlements is not None)
        assert not entitled.path.is_symlink()

    def test_a_bundle_with_no_engine_still_has_a_plan(self, tmp_path: Path) -> None:
        """The `--engine`-less developer build is not a special case, just a shorter one."""
        app = make_app(tmp_path, with_engine=False)
        steps = self.plan(app).steps
        assert [step.path for step in steps] == [
            app / "Contents/MacOS/GPTVoiceCodingShell",
            app,
        ]
        assert all(step.entitlements is None for step in steps)


LOCKED = """\
# generated by `python -m app_bundle lock`
aiortc==1.13.0 \\
    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111
av==16.0.1 \\
    --hash=sha256:2222222222222222222222222222222222222222222222222222222222222222 \\
    --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333
"""


class TestTheLock:
    """A pinned lock is what stops the signable set changing underneath the signer.

    The wheels contribute the overwhelming majority of the Mach-O files in the
    bundle — the bare interpreter carries eleven, the engine with its voice extra
    carries about eighty-five — so an unpinned resolve is a different set of
    binaries every build, none of which anybody reviewed.
    """

    def test_it_reads_names_versions_and_hashes(self, tmp_path: Path) -> None:
        path = tmp_path / "aarch64-apple-darwin.lock"
        path.write_text(LOCKED)
        read = lock.read(path)
        assert [(one.name, one.version) for one in read] == [
            ("aiortc", "1.13.0"),
            ("av", "16.0.1"),
        ]
        assert len(read[1].hashes) == 2

    def test_an_entry_without_a_hash_is_refused(self, tmp_path: Path) -> None:
        """`--require-hashes` would refuse it too, but only after the download."""
        path = tmp_path / "aarch64-apple-darwin.lock"
        path.write_text("aiortc==1.13.0\n")
        with pytest.raises(lock.LockError, match="aiortc"):
            lock.read(path)

    def test_an_unpinned_entry_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "aarch64-apple-darwin.lock"
        path.write_text("aiortc>=1.9 \\\n    --hash=sha256:" + "1" * 64 + "\n")
        with pytest.raises(lock.LockError, match="=="):
            lock.read(path)

    def test_a_missing_lock_names_the_command_that_makes_one(self, tmp_path: Path) -> None:
        """The refusal a host with no lock for its triple gets.

        Falling back to an unpinned install is the silent fallback this project
        bans, in the one place where it would also mean signing a set of binaries
        nobody reviewed.
        """
        with pytest.raises(lock.LockError, match="python -m app_bundle lock"):
            lock.read(tmp_path / "x86_64-apple-darwin.lock")

    def test_the_shipped_lock_is_readable_and_pinned(self) -> None:
        """v0's own lock, held to the same rule as any other."""
        read = lock.read(inputs.lock_for("aarch64-apple-darwin"))
        assert read
        assert all(one.hashes for one in read)

    def test_every_pinned_interpreter_has_a_lock(self) -> None:
        """The two halves of one architecture's support, kept in step.

        An entry in `INTERPRETERS` with no lock beside it is a build that gets
        all the way to `pip` before it discovers it cannot proceed.
        """
        for triple in inputs.INTERPRETERS:
            assert inputs.lock_for(triple).exists(), triple


class TestTheThingsThatMustAgree:
    """Facts that exist in two languages, or in two files, held to each other."""

    def test_the_shell_and_the_engine_name_the_same_default_socket_path(self) -> None:
        """The Swift shell and Python engine must meet at the same default address.

        If the two literals drift, the shell dials a socket the engine never
        bound and reports a missing engine while that engine is running.
        """
        swift = (inputs.SHELL_PACKAGE / "Sources/ShellCore/EngineLocation.swift").read_text()
        default = re.search(
            r'defaultSocketPath\(.*?"(?P<prefix>[^"\n]*)'
            r'\\\(uid \?\? getuid\(\)\)(?P<suffix>[^"\n]*)"',
            swift,
            re.DOTALL,
        )
        assert default is not None, "EngineLocation no longer states the default as one path"
        uid = 4242
        shell_path = Path(f"{default['prefix']}{uid}{default['suffix']}")

        assert shell_path == config.default_socket_path(uid)

    def test_the_shell_and_the_pipeline_name_the_same_interpreter_path(self) -> None:
        """`BundleLayout.engineInterpreterRelativePath` and `inputs.ENGINE_INTERPRETER`.

        The shell looks for the bundled interpreter at one path and the pipeline
        puts it at another, and nothing between them would notice: the shell's
        resolver simply falls through to `GPTVOICECODING_ENGINE_PYTHON` or `PATH`
        and takes the developer path — which works, and is not the bundled
        interpreter, and therefore is not what earns the microphone grant
        (ADR 0005). A silent downgrade to a working-but-wrong path is exactly the
        failure this repository refuses everywhere else.
        """
        swift = (inputs.SHELL_PACKAGE / "Sources/ShellCore/BundleLayout.swift").read_text()
        quoted = re.search(
            r'engineInterpreterRelativePath\s*=\s*"(?P<path>[^"]+)"',
            swift,
        )
        assert quoted is not None, "BundleLayout no longer states the path as a literal"
        assert quoted["path"] == inputs.ENGINE_INTERPRETER.as_posix()

    def test_the_bundle_identity_is_read_and_not_repeated(self) -> None:
        identity = inputs.identity()
        assert identity.identifier == "com.gptvoicecoding.GPT-VoiceCoding"
        assert identity.app_directory_name == "GPT-VoiceCoding.app"
        assert identity.executable == inputs.SHELL_PRODUCT

    def test_the_example_config_is_one_the_engine_would_accept(self, tmp_path: Path) -> None:
        """The shipped example has to be a *working* file, not an illustration.

        It is the first thing a new user copies into place, so a key it omits is
        an engine that refuses to start on their first run with a message about
        a section they were never shown. `[log]`'s three numbers have no default
        in code (ADR 0004) and this example did omit them, which is how this test
        came to exist.

        The adapter references are resolved for real; only the external binaries
        are stand-ins, and nothing at this layer looks at those.
        """
        example = (inputs.REPO_ROOT / "app_bundle" / inputs.CONFIG_EXAMPLE).read_text()
        placed = tmp_path / "config.toml"
        placed.write_text(example)
        read = config.load(placed)
        assert read.delegated_turn_model
        assert read.log.max_bytes > 0
        assert set(read.adapters.as_mapping()) >= {"call", "companion_channel", "session_launcher"}

    def test_the_example_config_points_at_the_cli_the_bundle_really_lays_out(self) -> None:
        """`[delegate] cli` has to be true, or the instructions naming it are not.

        Bridge Core puts this path into the instructions it generates, and the
        engine's own check on it is only "is a runnable file" — so an example
        that named the wrong place would produce instructions naming a CLI that
        is not there, which is the invented detail those instructions forbid.
        """
        example = (inputs.REPO_ROOT / "app_bundle" / inputs.CONFIG_EXAMPLE).read_text()
        assert f"/Contents/{inputs.RESOURCES.name}/{inputs.ENGINE_CLI.as_posix()}" in example


class TestTheProductDoesNotShipItsOwnBuildSystem:
    def test_the_wheel_does_not_contain_the_pipeline(self, tmp_path: Path) -> None:
        """`app_bundle` builds the product; it is not part of it.

        Asserted by building the real wheel rather than by reading
        `pyproject.toml`, because the thing that matters is what ends up in the
        artefact, and a packaging back-end is entitled to include more than the
        `packages` key names.
        """
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(tmp_path),
                str(inputs.REPO_ROOT),
            ],
            check=True,
            capture_output=True,
        )
        built = next(tmp_path.glob("*.whl"))
        with zipfile.ZipFile(built) as wheel:
            inside = wheel.namelist()
        assert not [name for name in inside if name.startswith("app_bundle")]
        assert any(name.startswith("gpt_voicecoding/") for name in inside)
