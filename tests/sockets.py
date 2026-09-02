"""A socket somebody answers, for the tests that need liveness to be real.

A module of its own rather than a `conftest` member, because `from conftest
import …` resolves to whichever `conftest` reached `sys.path` first and this
suite has two — the rule `test_layout.py` holds, from #93. The `socket_root`
fixture these sockets are bound under *is* a fixture, so it stays in
`tests/conftest.py`.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Deep enough that a probe nobody accepts cannot fill it. A real listener
#: accepts, so its backlog drains; a test socket that never does would start
#: refusing connections, and a refused `AF_UNIX` connect is indistinguishable
#: from a dead one — which is exactly the answer a liveness probe must not get
#: wrong. Measured: with a backlog of 1, eight engines probing each other all
#: read every live socket as debris.
LISTEN_BACKLOG = 128


@contextmanager
def listening(path: Path) -> Iterator[Path]:
    """A bound, listening socket, removed again on the way out."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen(LISTEN_BACKLOG)
        yield path
    finally:
        server.close()
        path.unlink(missing_ok=True)
