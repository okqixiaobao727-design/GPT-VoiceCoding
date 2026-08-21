"""The app-bundle pipeline: a clean checkout in, one signed `.app` out.

This lives at the repository root rather than under `src/`, because the product
does not ship its own build system. The wheel's contents are `src/gpt_voicecoding`
and nothing else, and a test asserts that this package never appears in it.

**The pipeline is split into a plan and a doing side**, the same shape
`adapters/session_launcher/plan.py` uses for a launch, and for the same reason:
the decisions are where the failures live, and decisions can be held still by a
test while subprocesses cannot.

- The **plan** side (`inputs`, `mach_o`, `lock`, `plan`) may read; it never
  writes, never spawns and never reaches the network. It answers: which
  interpreter this host needs, what the lock says may be installed, which files
  under the assembled tree are Mach-O, and **in what order they get signed**.
- The **doing** side (`run`) executes the plan's steps in order and stops at the
  first failure. It decides nothing.

The split is not stylistic. `codesign --verify --deep --strict` is the only
after-the-fact check available, and it does not walk `Contents/Resources` — the
reason the ticket locked an explicit inside-out enumerate-and-sign rather than
`codesign --deep`, which is deprecated for signing anyway. The signable set is
overwhelmingly the *wheels'* contribution — the bare python-build-standalone
tree carries 11 Mach-O files, the engine with its voice extra carries about 85 —
and that set changes shape every time the lock is regenerated. A missed `.so`
under `lib-dynload`, or an app signed before its own contents, produces a bundle
that verifies clean and then fails at the one moment it matters. So the
enumeration and the order are pytest assertions, not a hope.

See `docs/adr/0005-the-engine-lives-inside-the-app-bundle.md` for why the engine
is in the bundle at all, and `docs/app-bundle.md` for how to run this.
"""
