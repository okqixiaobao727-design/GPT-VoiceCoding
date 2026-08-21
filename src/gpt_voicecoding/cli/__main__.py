"""``python -m gpt_voicecoding.cli`` — the same surface as the console script.

The engine half has had this shape all along (``gpt_voicecoding.engine.__main__``);
this is its counterpart. It matters to the app bundle: `pip` writes a console
script with an **absolute** shebang naming the interpreter that installed it, so
a bundled `bridgectl` stops working the moment the `.app` is moved. The bundle
ships a two-line wrapper that execs the interpreter beside it and runs this
module instead — the interpreter relocates, and so does the CLI.
"""

from __future__ import annotations

import sys

from gpt_voicecoding.cli import main

sys.exit(main())
