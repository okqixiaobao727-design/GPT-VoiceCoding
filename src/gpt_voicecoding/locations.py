"""Where this product's own files are, said once.

Three layers ask this question and none of them may ask another. Bridge Core's
persistence writes the state file. The Claude adapter publishes the engine's
approval address so a hook process — which runs with no configuration, no
environment and no engine to ask — can find it. Installation writes down whether
the user wants the artifacts installed at all. Adapters may not import ``core``
(ADR 0001, enforced in ``tests/test_architecture.py``), so the answer cannot live
where the state file's answer used to live alone.

So it lives here: a leaf that imports nothing from this package and that every
layer may import. A second spelling of one of these paths is the shape of
[#47](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/47), where
the control-plane socket path is built independently in Swift and in Python with
no test holding the two together.

``base_dir`` runs through every function for one reason: a test that wrote to the
real application-support directory would be a test that installs this product on
the machine running it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Under the user's home. macOS only, like the rest of the product.
APP_SUPPORT_PARTS: Final = ("Library", "Application Support", "GPT-VoiceCoding")

#: The engine's own corner of it: state, log, configuration, published address.
ENGINE_DIR_NAME: Final = "engine"

#: What the engine publishes for hook processes to read. Not configuration and
#: not state — an address, true only while the engine that wrote it is serving.
ADDRESS_FILE_NAME: Final = "address.json"

#: Where the Codex login `LaunchAgent` sends the job's output. Not the engine's
#: log and never `bridge.logFile`: ADR 0004's rotation is rename-and-reopen and
#: this descriptor is held by launchd, which cannot be told to reopen anything.
CODEX_DAEMON_LOG_NAME: Final = "codex-daemon.log"

#: Whether the user wants the installation. Beside the engine's directory rather
#: than inside it, because it outlives any one engine and is not the engine's.
INSTALLATION_FILE_NAME: Final = "installation.json"


def product_directory(base_dir: Path | None = None) -> Path:
    """Everything this product owns on disk, under one directory."""
    return base_dir if base_dir is not None else Path.home().joinpath(*APP_SUPPORT_PARTS)


def engine_directory(base_dir: Path | None = None) -> Path:
    return product_directory(base_dir) / ENGINE_DIR_NAME


def address_path(base_dir: Path | None = None) -> Path:
    """Where the engine says it is, for a process nobody could hand a variable to."""
    return engine_directory(base_dir) / ADDRESS_FILE_NAME


def codex_daemon_log_path(base_dir: Path | None = None) -> Path:
    """Where the login job says why it could not start Codex's shared daemon."""
    return product_directory(base_dir) / CODEX_DAEMON_LOG_NAME


def installation_path(base_dir: Path | None = None) -> Path:
    """Where the user's answer to "do you want this installed" is kept."""
    return product_directory(base_dir) / INSTALLATION_FILE_NAME
