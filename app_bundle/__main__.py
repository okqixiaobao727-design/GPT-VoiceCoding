"""``python -m app_bundle`` — what `scripts/build-app.sh` runs.

The version check is here rather than in the shell script because this is the
entry point everyone actually uses, script or not. macOS ships 3.9 as `python3`,
and the failure without this is a `SyntaxError` from a module the reader has no
reason to be looking at.
"""

from __future__ import annotations

import sys

#: The floor `pyproject.toml` sets for the project, which this shares.
REQUIRES = (3, 12)

if sys.version_info < REQUIRES:
    wanted = ".".join(str(part) for part in REQUIRES)
    running = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        f"the app-bundle pipeline needs Python {wanted} or newer, and this is {running} "
        f"({sys.executable}). macOS ships 3.9 as `python3`; point at a newer one."
    )

from app_bundle.run import main  # noqa: E402 - the version check has to come first

sys.exit(main())
