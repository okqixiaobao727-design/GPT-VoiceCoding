# The app bundle

One command, from a clean checkout, to a signed `GPT-VoiceCoding.app`:

```bash
scripts/build-app.sh
```

That is the whole interface. The pipeline itself is [`app_bundle/`](../app_bundle),
in Python, under the project's own test suite; the script is one line that runs
it. Three other things it will do:

```bash
scripts/build-app.sh --debug            # a debug build of the shell
scripts/build-app.sh --without-engine   # the shell alone, for the developer loop
scripts/build-app.sh lock               # regenerate the dependency lock
```

`--without-engine` is the successor to the old `dev-app.sh`. It builds a bundle
the shell's own resolver falls through past, to `GPTVOICECODING_ENGINE_PYTHON` or
`PATH` — the developer path, which is a stated feature rather than a degraded
build.

## What ends up inside

```
GPT-VoiceCoding.app/
└── Contents/
    ├── Info.plist                    the bundle's identity, copied from shell/Resources
    ├── MacOS/GPTVoiceCodingShell     the menu-bar shell, and nothing else
    └── Resources/
        ├── config.example.toml       shipped, never installed for you
        └── engine/                   python-build-standalone, the locked wheels, the engine
            └── bin/
                ├── python3 -> python3.12    what the shell spawns
                └── bridgectl                what `[delegate] cli` names
```

The engine is inside the bundle because that is what earns the microphone grant
— see [ADR 0005](adr/0005-the-engine-lives-inside-the-app-bundle.md). Bundle
containment is the mechanism; the process tree is not.

## Why the pipeline is a plan and a doing side

`codesign --deep` is deprecated for signing, and never discovers
`Contents/Resources` anyway — so the set of things to sign is **enumerated**, not
delegated to a flag. `--deep --strict` is still the right thing to *verify* with,
and the build runs it, but it is not what finds the files.

That makes the enumeration and the signing order the two facts nothing else can
check. They are decided in `app_bundle/plan.py` and `app_bundle/signing.py`,
which read and never write, spawn or download, and they are asserted by
`tests/test_app_bundle.py`. `app_bundle/run.py` executes the result in order and
stops at the first failure.

The size of what is at stake: a bare python-build-standalone tree carries **11**
Mach-O files, and the engine with its voice extra carries **85** — the wheels are
almost all of it, and that set changes shape every time the lock is regenerated.
A missed `.so` under `lib-dynload`, or an app signed before its own contents,
produces a bundle that verifies clean and fails at the one moment it matters.

## The decisions this pipeline holds

| Decision | Why |
| --- | --- |
| python-build-standalone `install_only`, not a framework CPython | A framework CPython re-executes `Python.app/Contents/MacOS/Python` from its original location, so the process that ends up running is *outside* the bundle even though the shell spawned the one inside it. |
| Pinned by release tag **and** SHA256, and a hash-pinned lock for the wheels | A pipeline whose job is to sign a set of Mach-O files must not let an unpinned upstream change that set underneath it. Reproducibility here is a signing-integrity property, not a convenience. |
| One lock per host triple, and a refusal when there is none | A wheel's hash is architecture-specific, so one lock cannot cover two. Falling back to an unpinned install would mean signing binaries nobody reviewed. |
| Single-architecture, read from the build host | python-build-standalone publishes no `universal2` build, so a universal `.app` means lipo-ing two interpreters and two sets of wheels. Adoption-era work, which route (a) accommodates later without architectural change. |
| Ad-hoc signature, no Developer ID, no notarization | Charter decision 9: v0 targets developers. The accepted cost is that the signature changes per build, so a *new build* may re-prompt for the microphone. |
| Hardened runtime **off** | It buys nothing v0 ships — it is what notarization needs — and it is the most likely cause of the CFFI audio-callback crash the `allow-jit` escape hatch was reserved for, whose usual companion fix (`disable-library-validation`) is forbidden. It becomes the notarization-era ticket's decision, where `allow-jit` becomes live again. |
| `com.apple.security.device.audio-input` on the bundled interpreter only | Entitlements go on executables, and `python3.12` is the process that opens the device. Without the sandbox or the hardened runtime it is **inert** — belt and braces, deliberately, so the bundle is already the right shape later. It is not what earns the grant. |
| Bytecode pre-compiled at build time | Nothing may write into the bundle at runtime. The shell already sets `PYTHONDONTWRITEBYTECODE` for the bundled interpreter it spawns; this covers the two cases it cannot see — the engine run headless from a terminal, and the relocated `bridgectl` — and it also means a start does not recompile from source into memory. |
| `bridgectl` is a two-line wrapper, not pip's console script | `pip` writes an **absolute** shebang. The interpreter relocates; its scripts do not. The engine's own check on `[delegate] cli` is "is a runnable file", which a dead shebang passes — so the failure would surface as `bad interpreter` inside a generated instruction. |
| `config.example.toml` is shipped, never installed | The configuration is a file the user owns and the engine only reads, and it names adapters by import reference, so it runs with the privileges of whoever wrote it. An installer that authored it would be claiming something that is not the installer's. |

## Regenerating the lock

```bash
scripts/build-app.sh lock
```

It resolves against the **bundled** interpreter's own version and platform,
because a lock resolved by some other Python is a lock for some other set of
wheels. It writes `app_bundle/locks/<triple>.lock`. Read the diff: it is the list
of binaries the next build will sign.

## Before a release: the microphone

The one part a machine cannot finish. macOS shows the TCC prompt to a person.

```bash
python3 scripts/microphone_grant_proof.py --reset
```

It prints the checklist and what each step must show. The probe that gated
ADR 0005 already established the negative control — an interpreter outside any
`.app` collapses to the bare binary path — so that half is deliberately not
re-run: it is a property of macOS, not of this bundle.

## Cutover: one bot, one engine

Telegram permits exactly **one `getUpdates` consumer per bot**. If the
first-generation bridge is still running on the same machine, do not point the
new engine's Companion Channel at the bot it holds: the two steal each other's
inbound messages, `verify` still passes because `getMe` and `getChat` are
unaffected, and only inbound goes quiet. Stop the old bridge first, or use a
distinct bot until it is retired.
